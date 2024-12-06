from collections.abc import AsyncGenerator

from nova.core.exceptions import PlanTrajectoryFailed
from nova.gateway import ApiGateway
from nova.types.state import MotionState
from nova.types.action import Action, CombinedActions
from nova.types.pose import Pose
from nova.types.collision_scene import CollisionScene
from loguru import logger
import wandelbots_api_client as wb

MAX_JOINT_VELOCITY_PREPARE_MOVE = 0.2
START_LOCATION_OF_MOTION = 0.0


class MotionGroup:
    def __init__(self, api_gateway: ApiGateway, cell: str, motion_group_id: str):
        self._api_gateway = api_gateway
        self._motion_api_client = api_gateway.motion_api
        self._cell = cell
        self._motion_group_id = motion_group_id
        self._current_motion: str | None = None

    @property
    def motion_group_id(self) -> str:
        return self._motion_group_id

    @property
    def current_motion(self) -> str:
        # if not self._current_motion:
        #    raise ValueError("No MotionId attached. There is no planned motion available.")
        return self._current_motion

    async def stream_run(
        self,
        actions: list[Action] | Action,
        tcp: str,
        # collision_scene: dts.CollisionScene | None,
        response_rate_in_ms: int = 200,
    ) -> AsyncGenerator[MotionState, None]:
        """An asynchronous generator that tracks and yields motion states along a given path.

        This method iterates over the motion states of a given path while handling motion events
        associated with specific path parameters. It tracks the motion states in the `_motion_recording`
        attribute and yields each state as it progresses through the path. Additionally, if there are any
        motion events defined in the path tags, they will be executed when the corresponding path parameter
        is reached.

        Args:
            actions: the motion defining the path
            tcp (Optional[str]): The tool center point (TCP) to be used for the motion.
            collision_scene (Optional[CollisionScene]): The collision scene to be used for collision detection.
            response_rate_in_ms (int): The sample time in milliseconds to be used for the motion.

        Returns:
            Context manager returns an iterator
                Yields:
                    MotionState: The current motion state along the path.
                Raises:
                    RobotMotionError: if the robot motion stop without reaching the target and stops motion then on exit
                    StopAsyncIteration: If the motion path iteration is completed.

        """
        if not isinstance(actions, list):
            actions = [actions]

        # TODO get default tcp if tcp is not set
        motion_iter = self._planned_motion_iter(
            actions=actions, tcp=tcp, collision_scene=None, response_rate_in_ms=response_rate_in_ms
        )
        async for motion_state in motion_iter:
            yield motion_state

    async def run(self, actions: list[Action] | Action, tcp: str):
        async for _ in self.stream_run(actions=actions, tcp=tcp):
            pass

    async def get_state(self, tcp: str) -> wb.models.MotionGroupStateResponse:
        response = await self._api_gateway.motion_group_infos_api.get_current_motion_group_state(
            cell=self._cell, motion_group=self.motion_group_id, tcp=tcp
        )
        return response

    async def joints(self, tcp: str) -> wb.models.Joints:
        state = await self.get_state(tcp=tcp)
        return state.state.joint_position

    async def tcp_pose(self, tcp: str) -> Pose:
        state = await self.get_state(tcp=tcp)
        return Pose(state.state.tcp_pose)

    async def _get_number_of_joints(self) -> int:
        spec = await self._api_gateway.motion_group_infos_api.get_motion_group_specification(
            cell=self._cell, motion_group=self.motion_group_id
        )
        return len(spec.mechanical_joint_limits)

    async def _get_optimizer_setup(self, tcp: str) -> wb.models.OptimizerSetup:
        return await self._api_gateway.motion_group_infos_api.get_optimizer_configuration(
            cell=self._cell, motion_group=self._motion_group_id, tcp=tcp
        )

    async def plan(
        self, actions: list[Action] | Action, tcp: str
    ) -> wb.models.PlanTrajectoryResponse:
        if not isinstance(actions, list):
            actions = [actions]

        if len(actions) == 0:
            raise ValueError("Actions are empty")

        current_joints = await self.joints(tcp=tcp)
        robot_setup = await self._get_optimizer_setup(tcp=tcp)

        # TODO: paths = [wb.models.MotionCommandPath(**path.model_dump()) for path in path.motions]
        combined_actions = CombinedActions(items=actions)
        motions = [
            wb.models.MotionCommandPath.from_dict(motion.model_dump())
            for motion in combined_actions.motions
        ]
        print(motions)
        motion_commands = [wb.models.MotionCommand(path=motion) for motion in motions]

        request = wb.models.PlanTrajectoryRequest(
            robot_setup=robot_setup,
            motion_group=self.motion_group_id,
            start_joint_position=current_joints.joints,
            motion_commands=motion_commands,
            tcp=tcp,
        )

        motion_api_client = self._api_gateway.motion_api
        plan_response = await motion_api_client.plan_trajectory(
            cell=self._cell, plan_trajectory_request=request
        )

        if isinstance(
            plan_response.response.actual_instance, wb.models.PlanTrajectoryFailedResponse
        ):
            failed_response = plan_response.response.actual_instance
            raise PlanTrajectoryFailed(failed_response)

        return plan_response

    async def _get_trajectory_sample(
        self, location: float
    ) -> wb.models.GetTrajectorySampleResponse:
        """Call the RAE to get single sample of trajectory from a previously planned path

        Args:
            location: The path parameter along the trajectory to sample

        Returns:
            The trajectory sample at the specified location
        """
        if location < 0:
            raise ValueError("location cannot be negative")

        return await self._motion_api_client.get_motion_trajectory_sample(
            cell=self._cell, motion=self.current_motion, location_on_trajectory=location
        )

    async def _planned_motion_iter(
        self,
        actions: list[Action],
        tcp: str,
        collision_scene: CollisionScene | None,
        response_rate_in_ms: int,
    ) -> AsyncGenerator[MotionState]:
        number_of_joints = await self._get_number_of_joints()

        async def move_along_path(
            motion_id: str,
            joint_velocities: list[float] | None = None,
            joint_trajectory: wb.models.JointTrajectory | None = None,
        ) -> AsyncGenerator[MotionState]:
            motion_api = self._api_gateway.motion_api
            load_plan_response = await motion_api.load_planned_motion(
                cell=self._cell,
                planned_motion=wb.models.PlannedMotion(
                    motion_group=self.motion_group_id,
                    times=joint_trajectory.times,
                    joint_positions=joint_trajectory.joint_positions,
                    locations=joint_trajectory.locations,
                    tcp="Flange",
                ),
            )

            load_plan_response = load_plan_response.plan_successful_response

            limit_override = wb.models.LimitsOverride()
            if joint_velocities is not None:
                limit_override.joint_velocity_limits = wb.models.Joints(joints=joint_velocities)

            # Iterator that moves the robot to start of motion
            move_to_trajectory_stream = motion_api.stream_move_to_trajectory_via_joint_ptp(
                cell=self._cell, motion=load_plan_response.motion, location_on_trajectory=0
            )
            async for motion_state_move_to_trajectory in move_to_trajectory_stream:
                yield motion_state_move_to_trajectory

            responses = []

            async def movement_controller(
                response_stream: AsyncGenerator,
            ) -> (AsyncGenerator)[
                wb.models.ExecuteTrajectoryRequest, wb.models.ExecuteTrajectoryResponse
            ]:
                yield wb.models.InitializeMovementRequest(
                    trajectory=load_plan_response.motion, initial_location=0
                )

                combined_actions = CombinedActions(items=actions)
                initialize_movement_response = await anext(response_stream)
                print(f"initial move response {initialize_movement_response}")
                set_io_list = [
                    wb.models.SetIO(io=action.model_dump(), location=action.path_parameter)
                    for action in combined_actions.actions
                ]

                yield wb.models.StartMovementRequest(
                    set_ios=set_io_list, start_on_io=None, pause_on_io=None
                )

                async for execute_trajectory_response in response_stream:
                    response = execute_trajectory_response.actual_instance
                    print(f"execute_trajectory_response {response}")
                    responses.append(response)

                    # Terminate the generator
                    if isinstance(response, wb.models.Standstill):
                        if (
                            response.standstill.reason
                            == wb.models.StandstillReason.REASON_MOTION_ENDED
                        ):
                            return

            await motion_api.execute_trajectory(self._cell, movement_controller)

        """
        if any(isinstance(motion, dts.UnresolvedMotion) for motion in path.motions) or collision_scene is not None:
            current_joints = await self._get_current_joints()
            optimizer_setup = await self.get_optimizer_setup(tcp)
            path = await resolve_motions_and_check_collisions(
                initial_joints=current_joints,
                motion_trajectory=path,
                collision_scene=collision_scene,
                optimizer_setup=optimizer_setup,
                moving_robot_identifier=self.identifier,
            )
        """

        plan_response = await self.plan(actions, tcp)

        # if not rae_pb_parser.plan_response.is_executable(plan_response):
        #     raise MotionException(plan_response, [], [])

        # await self._get_trajectory_sample(response_rate_in_ms)
        # TODO: take velocity override into account.
        # if len(trajectory.sample) > 0 and not math.isnan(trajectory[-1].time):
        #    self._execution_duration += trajectory[-1].time

        # self._current_motion = plan_response.response.actual_instance
        logger.debug(f"Planned move session: {self.current_motion}")

        # TODO refactor RAE commands vs. multiple motion chains
        joints_velocities = [MAX_JOINT_VELOCITY_PREPARE_MOVE] * number_of_joints
        move_iter = move_along_path(
            self.current_motion,
            joint_velocities=joints_velocities,
            joint_trajectory=plan_response.response.actual_instance,
        )
        async for motion_state in move_iter:
            yield motion_state

        logger.debug(f"Move session '{self.current_motion}' was successfully executed.")
        self._current_motion = None

    async def stop(self):
        logger.debug(f"Stopping motion of {self}...")
        try:
            await self._motion_api_client.stop_execution(
                cell=self._cell, motion=self.current_motion
            )
            logger.debug(f"Motion {self.current_motion} stopped.")
        except ValueError as e:
            logger.debug(f"No motion to stop for {self}: {e}")
