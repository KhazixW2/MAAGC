import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Set, Tuple

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import logger
from utils.screen import screencap

from .battle_action import BattleActionMixin
from .battle_camera import BattleCameraMixin
from .battle_decision import BattleDecisionMixin
from .battle_grid import COLS, ROWS, BattleGrid, Cell, CellType, GridScanner
from .battle_planner import BattlePlannerMixin
from .battle_threat import BattleThreatMixin
from .battle_world_map import (
    BattleSessionRegistry,
    BattleSessionState,
    session_key,
)


class ActiveRoundStatus(Enum):
    """主动阶段一次建图的结论。"""

    READY = "ready"      # 已获得可行动状态
    WAIT = "wait"        # 本回合无有效目标，结束回合后继续
    FAILED = "failed"    # 无法继续战斗


@dataclass
class _ActiveRoundState:
    """主动阶段建图后可用于行动的状态。"""

    grid: BattleGrid
    reference: Any
    allies: List[Tuple[int, int]] = field(default_factory=list)
    pursuit_direction: Optional[Tuple[int, int]] = None
    hidden_enemy_cell: Optional[Tuple[int, int]] = None
    environment_mode: bool = False


@dataclass
class _ActiveRoundResult:
    """主动阶段建图的结论。"""

    status: ActiveRoundStatus
    state: Optional[_ActiveRoundState] = None
    #: WAIT 时用于结束回合的参考截图。
    wait_reference: Any = None
    #: WAIT 时的阶段名称，用于结束回合与日志。
    wait_phase: str = ""


@dataclass
class _RoundOutcome:
    """一轮角色行动的结论。"""

    #: 战斗终态：None 表示战斗仍在进行，True/False 表示胜利/失败。
    result: Optional[bool] = None
    #: 本回合是否已确认推进到下一回合。
    advanced: bool = False
    #: 本回合是否确认过攻击或移动。
    had_action: bool = False


@AgentServer.custom_action("AutoFightProcessor")
class AutoFightProcessor(
    BattleCameraMixin,
    BattleThreatMixin,
    BattlePlannerMixin,
    BattleDecisionMixin,
    BattleActionMixin,
    CustomAction,
):
    """先被动等待反击，超过指定回合后执行主动战斗。

    策略：前 20 轮全部依赖「结束回合 + 自动反击」，让敌人自己走过来；
    仅当 20 轮后仍未通关时，才进入主动搜索与追击模式。这样可以减少决策
    次数，避免每局都触发 16 视野螺旋搜索的低性价比操作。

    具体能力按职责拆分为 ``BattleCameraMixin``（镜头与建图）、
    ``BattleThreatMixin``（危险覆盖层）、``BattlePlannerMixin``（地图合并）、
    ``BattleDecisionMixin``（单角色决策）与 ``BattleActionMixin``
    （点击与结果校验）五个能力类。
    """

    # A battle can legitimately last well beyond the old per-invocation limit
    # of 40.  The session survives recovery calls; this high ceiling is only a
    # final safety fuse, while the normal stop condition is a verified result.
    MAX_ACTION_CYCLES = 300
    MAX_NO_PROGRESS_CYCLES = 12
    PASSIVE_ROUNDS = 20
    ACTIVE_FROM_ROUND = PASSIVE_ROUNDS + 1
    TEST_ACTIVE_FROM_ROUND_ENV = "MAAGC_TEST_ACTIVE_FROM_ROUND"

    # 大地图关卡下敌我可能长期不在同一视野。超过该回合数仍未观察到
    # 敌人方向记忆时，主动丢弃记忆并切回纯螺旋搜索，避免绕远路。
    ENEMY_DIRECTION_MEMORY_ROUNDS = 6

    def __init__(self) -> None:
        super().__init__()
        self.scanner = GridScanner()
        self._coarse_threat_direction: Optional[Tuple[int, int]] = None
        self._last_confirmed_move_direction: Optional[Tuple[int, int]] = None
        self._threat_overlay_safe = True
        self._session: Optional[BattleSessionState] = None
        # 跨回合敌人方向记忆：上次成功搜索到的敌人相对原点的螺旋方向。
        # 我方按此方向推进若干回合，敌人可能也在接近，自然碰面。
        self._last_known_enemy_direction: Optional[Tuple[int, int]] = None
        # 自上次成功记录方向以来经过的回合数；超过阈值后丢弃记忆。
        self._last_known_enemy_age = 0

    # ====================
    # 主循环
    # ====================

    def run(
        self, context: Context, _argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        """执行整场战斗；恢复调用继续使用同一份地图和回合状态。"""
        self._session = BattleSessionRegistry.begin(session_key(context))
        self._last_confirmed_move_direction = (
            None
            if self._session.last_move_direction is None
            else (
                self._session.last_move_direction[1],
                self._session.last_move_direction[0],
            )
        )
        self._threat_overlay_safe = True
        # 跨回合敌人方向记忆：上次成功搜索到的敌人相对原点的螺旋方向。
        # 我方按此方向推进若干回合，敌人可能也在接近，自然碰面。
        # 记忆只在本次调用内跨回合有效，任务被恢复时重新开始积累。
        self._last_known_enemy_direction = None
        # 自上次成功记录方向以来经过的回合数；超过阈值后丢弃记忆。
        self._last_known_enemy_age = 0

        # 上一次任务可能在全图搜索中途被外部停止，导致红色危险覆盖层
        # 残留。该覆盖层会污染敌方红色状态条识别；进入任何正式决策前
        # 都先恢复为关闭状态，只有敌人搜索窗口才临时开启。
        if not self._set_threat_overlay(context, False):
            logger.warning("战斗入口未能确认危险覆盖层关闭，继续谨慎识别")

        passive_rounds = self._resolve_passive_rounds()

        logger.debug(
            "恢复整场战斗会话: "
            f"confirmed_rounds={self._session.confirmed_rounds}, "
            f"world_origin={self._session.world.camera_origin}, "
            f"explored={len(self._session.world.observed)}"
        )

        while self._session.action_cycles < self.MAX_ACTION_CYCLES:
            if context.tasker.stopping:
                logger.info("任务执行被停止")
                return CustomAction.RunResult(success=False)
            self._session.action_cycles += 1
            self._expire_enemy_direction_memory()

            round_result = self._run_battle_round(context, passive_rounds)
            if round_result is not None:
                return CustomAction.RunResult(success=round_result)

        logger.error(
            f"战斗达到安全动作循环上限 {self.MAX_ACTION_CYCLES}，停止任务"
        )
        return CustomAction.RunResult(success=False)

    def _resolve_passive_rounds(self) -> int:
        """被动坚守回合数；测试环境变量可提前进入主动阶段。"""
        passive_rounds = self.PASSIVE_ROUNDS
        test_override = os.environ.get(self.TEST_ACTIVE_FROM_ROUND_ENV)
        if test_override:
            try:
                active_from_round = max(1, int(test_override))
                passive_rounds = active_from_round - 1
                logger.warning(
                    f"测试模式：第 {active_from_round} 回合直接主动出击"
                )
            except ValueError:
                logger.warning(
                    f"忽略非法测试主动回合覆盖: {test_override!r}"
                )
        return passive_rounds

    def _expire_enemy_direction_memory(self) -> None:
        """每个回合开头累加方向记忆年龄，超期后丢弃。"""
        if self._last_known_enemy_direction is None:
            return
        self._last_known_enemy_age += 1
        if self._last_known_enemy_age > self.ENEMY_DIRECTION_MEMORY_ROUNDS:
            logger.warning(
                f"敌人方向记忆超过 {self.ENEMY_DIRECTION_MEMORY_ROUNDS} "
                "回合未刷新，丢弃记忆并切回纯螺旋搜索"
            )
            self._last_known_enemy_direction = None
            self._last_known_enemy_age = 0

    def _run_battle_round(
        self, context: Context, passive_rounds: int
    ) -> Optional[bool]:
        """执行一个战斗回合。

        返回 None 表示本回合结束、战斗继续；True/False 表示已进入
        胜负终态或已无法继续。
        """
        # 结算页的红蓝装饰可能被范围节点命中。胜负检测必须先于
        # “残留选择清理”，否则任务完成页会被误当成仍有攻击范围，
        # 点击 Esc 后仍残留并导致流程错误退出。
        terminal_img = screencap(context)
        if terminal_img is None:
            logger.error("战斗入口截图失败")
            return False
        terminal_result = self._detect_battle_result(context, terminal_img)
        if terminal_result is not None:
            return terminal_result

        # 外部停止或失败动作可能留下“人物已选中但卡片已收起”的状态。
        # 这时黄色/蓝色行动范围会污染红条扫描，必须先真正点右上 Esc
        # 取消选择，不能把顶部中间的卡片按钮误当成取消按钮。
        if not self._clear_stale_selection(context):
            logger.error("回合建图前无法清除残留选择状态")
            return False

        # 上一名角色也可能只留下了人物卡片。它会遮住红条、黄色攻击
        # 角标和目标点击点，正式建图前仍需验证收起。
        if not self._dismiss_role_card(context):
            logger.error("回合建图前无法收起人物卡片，停止以避免盲目移动")
            return False

        # 上一轮的威胁层若因动画/识别延迟未关闭，红色覆盖会污染本轮
        # 的敌方与环境目标扫描。只有确认普通画面恢复后才允许建图。
        if not self._set_threat_overlay(context, False):
            logger.warning("回合建图前威胁层仍开启，本回合只结束等待恢复")
            recovery_reference = screencap(context)
            if recovery_reference is None:
                return False
            recovery_result = self._end_round(
                context, recovery_reference, "威胁层恢复失败"
            )
            if recovery_result is not None:
                return recovery_result
            self._wait_for_scene_settle(context, timeout=6.0)
            return None

        game_round = self._session.confirmed_rounds + 1
        logger.debug(
            f"战斗回合 {game_round} / 动作循环 "
            f"{self._session.action_cycles}：建立当前战场快照"
        )
        round_reference = screencap(context)
        if round_reference is None:
            logger.error("建立回合快照失败")
            return False

        battle_result = self._detect_battle_result(context, round_reference)
        if battle_result is not None:
            return battle_result

        # 所有任务统一先完整结束 20 个回合。判断依据存放在整场会话中，
        # AutoFightProcessor 被恢复逻辑再次调用时不会重新等待 20 回合。
        if self._session.confirmed_rounds < passive_rounds:
            logger.debug(
                f"第 {game_round} 回合：统一坚守反击 "
                f"({self._session.confirmed_rounds}/{passive_rounds})，结束回合"
            )
            passive_result = self._end_round(
                context, round_reference, "被动阶段"
            )
            if passive_result is not None:
                return passive_result
            self._wait_for_scene_settle(context, timeout=6.0)
            return None

        return self._run_active_round(context, round_reference, passive_rounds)

    # ====================
    # 主动阶段
    # ====================

    def _run_active_round(
        self, context: Context, round_reference: Any, passive_rounds: int
    ) -> Optional[bool]:
        """主动搜索与追击阶段的一次回合。"""
        prepared = self._prepare_active_round(
            context, round_reference, passive_rounds
        )
        if prepared.status is ActiveRoundStatus.FAILED:
            return False
        if prepared.status is ActiveRoundStatus.WAIT:
            wait_result = self._end_round(
                context, prepared.wait_reference, prepared.wait_phase
            )
            if wait_result is not None:
                return wait_result
            self._wait_for_scene_settle(context, timeout=6.0)
            return None

        state = prepared.state
        if state is None:
            logger.error("主动阶段建图未返回可行动状态")
            return False
        outcome = self._act_with_allies(context, state)
        if outcome.result is not None:
            return outcome.result

        if outcome.advanced:
            self._wait_for_scene_settle(context, timeout=6.0)
            return None

        if not outcome.had_action:
            no_progress = self._session.record_no_progress()
            logger.warning(
                "主动回合没有确认任何攻击或移动: "
                f"{no_progress}/{self.MAX_NO_PROGRESS_CYCLES}"
            )
            if no_progress >= self.MAX_NO_PROGRESS_CYCLES:
                logger.error("连续主动回合没有可验证动作，停止进入外层恢复")
                return False

        # 最后一名角色行动后，游戏可能自行结束我方回合。留一个短暂
        # 宽限期，仅当常驻回合数字没有变化时才点击结束回合。
        if self._wait_for_round_change(context, state.reference, timeout=1.5):
            logger.debug("检测到自动换回合，跳过结束回合按钮")
            self._record_round_advance("角色行动后自动换回合")
            self._wait_for_scene_settle(context, timeout=6.0)
            return None

        end_result = self._end_round(context, state.reference, "主动阶段")
        if end_result is not None:
            return end_result
        self._wait_for_scene_settle(context, timeout=6.0)
        return None

    def _prepare_active_round(
        self, context: Context, round_reference: Any, passive_rounds: int
    ) -> _ActiveRoundResult:
        """主动阶段建图：定位我方、敌人与追击方向。"""
        round_grid = BattleGrid()
        self.scanner.scan_grid(round_grid, context, round_reference)
        self._merge_world_view(
            round_grid,
            self._session.confirmed_rounds,
            replace_visible=True,
        )

        if self._session.confirmed_rounds == passive_rounds:
            logger.info(
                f"战斗到达第 {self._session.confirmed_rounds + 1} 回合仍未结束，"
                "切换为主动搜索与追击模式"
            )

        world = self._session.world
        if not world.has_allies() or not self._world_has_fresh_targets():
            logger.debug(
                "全局地图缺少我方或敌人，触发威胁层顺时针探索: "
                f"allies={world.has_allies()}, "
                f"enemies={self._world_has_fresh_targets()}"
            )
            self._explore_world_clockwise(
                context, self._session.confirmed_rounds
            )

        focused = self._focus_known_ally(
            context, self._session.confirmed_rounds
        )
        if focused is None and (
            not world.has_allies() or not self._world_has_fresh_targets()
        ):
            self._explore_world_clockwise(
                context, self._session.confirmed_rounds
            )
            focused = self._focus_known_ally(
                context, self._session.confirmed_rounds
            )

        if focused is None or (
            not self._world_has_fresh_targets()
            and self._coarse_threat_direction is None
        ):
            no_progress = self._session.record_no_progress()
            logger.warning(
                "主动阶段完整建图后仍无法同时定位我方和敌人，"
                f"本回合等待敌人移动后重试 ({no_progress}/"
                f"{self.MAX_NO_PROGRESS_CYCLES})"
            )
            if no_progress >= self.MAX_NO_PROGRESS_CYCLES:
                logger.error("连续多轮全局建图没有新增有效战斗目标")
                return _ActiveRoundResult(ActiveRoundStatus.FAILED)
            latest = screencap(context)
            if latest is None:
                return _ActiveRoundResult(ActiveRoundStatus.FAILED)
            return _ActiveRoundResult(
                ActiveRoundStatus.WAIT,
                wait_reference=latest,
                wait_phase="全局建图等待",
            )

        round_grid, round_reference = focused
        round_grid, recentered, recentered_img = self._recenter_edge_allies(
            context,
            round_grid,
            self._session.confirmed_rounds,
        )
        if recentered and recentered_img is not None:
            round_reference = recentered_img
        if not round_grid.enemy_units:
            refreshed = self._refresh_local_threat_map(
                context, self._session.confirmed_rounds
            )
            if not refreshed or not self._threat_overlay_safe:
                logger.warning(
                    "当前我方视野无法安全刷新威胁层，本回合只结束等待"
                )
                return _ActiveRoundResult(
                    ActiveRoundStatus.WAIT,
                    wait_reference=round_reference,
                    wait_phase="局部威胁刷新失败",
                )
            normal_img = screencap(context)
            if normal_img is None:
                return _ActiveRoundResult(ActiveRoundStatus.FAILED)
            round_grid = self._scan_world_view(
                context,
                normal_img,
                self._session.confirmed_rounds,
                overlay_open=False,
            )
            round_reference = normal_img

        # A precisely identified hollow threat cell can itself sit at a
        # viewport corner even though no normal red bar is visible.  Pull
        # that world point inward with the matching diagonal nudge before
        # selecting a unit, then rebuild both normal and threat views.
        _, known_hidden_target, _ = self._world_pursuit(round_grid)
        if known_hidden_target is not None and not round_grid.enemy_units:
            (
                round_grid,
                hidden_recentered,
                hidden_recentered_img,
            ) = self._recenter_edge_allies(
                context,
                round_grid,
                self._session.confirmed_rounds,
                known_target=known_hidden_target,
            )
            if hidden_recentered and hidden_recentered_img is not None:
                round_reference = hidden_recentered_img
                if not round_grid.enemy_units:
                    if not self._refresh_local_threat_map(
                        context, self._session.confirmed_rounds
                    ):
                        logger.warning("隐藏敌人格重定位后威胁层刷新失败")
                    normal_img = screencap(context)
                    if normal_img is None:
                        return _ActiveRoundResult(ActiveRoundStatus.FAILED)
                    round_grid = self._scan_world_view(
                        context,
                        normal_img,
                        self._session.confirmed_rounds,
                        overlay_open=False,
                    )
                    round_reference = normal_img

        if (
            not self._world_has_fresh_targets()
            and self._coarse_threat_direction is None
        ):
            self._explore_world_clockwise(
                context, self._session.confirmed_rounds
            )
            focused = self._focus_known_ally(
                context, self._session.confirmed_rounds
            )
            if focused is None or (
                not self._world_has_fresh_targets()
                and self._coarse_threat_direction is None
            ):
                no_progress = self._session.record_no_progress()
                logger.warning(
                    "局部威胁刷新后敌人位置失效，等待下一回合再探索 "
                    f"({no_progress}/{self.MAX_NO_PROGRESS_CYCLES})"
                )
                latest = screencap(context)
                if latest is None:
                    return _ActiveRoundResult(ActiveRoundStatus.FAILED)
                return _ActiveRoundResult(
                    ActiveRoundStatus.WAIT,
                    wait_reference=latest,
                    wait_phase="敌人位置失效",
                )
            round_grid, round_reference = focused

        pursuit_direction, hidden_enemy_cell, environment_mode = (
            self._world_pursuit(round_grid)
        )
        # 跨回合方向记忆在此刷新：本次规划出的追击方向就是最新的记忆值，
        # 因此下面判空一律用 pursuit_direction，不必再读记忆字段。
        self._last_known_enemy_direction = pursuit_direction
        self._last_known_enemy_age = 0
        logger.debug(
            "全局地图规划目标: "
            f"direction={pursuit_direction}, "
            f"local_target={hidden_enemy_cell}, "
            f"environment={environment_mode}"
        )

        unsafe_allies = [
            cell
            for cell in round_grid.self_units
            if not self._ally_is_actionable(cell)
        ]
        if unsafe_allies:
            logger.debug(
                f"忽略 {len(unsafe_allies)} 名位于屏幕边缘/系统 UI 区域的我方单位"
            )
        # 全图搜索可能已经平移镜头并替换 round_grid，行动坐标必须从
        # 搜索返回的新快照重新生成，不能沿用搜索前的格子位置。
        allies = [
            (cell.row, cell.col)
            for cell in round_grid.self_units
            if self._ally_is_actionable(cell)
        ]
        logger.debug(
            f"战场识别: {COLS}x{ROWS}, 我方={len(allies)}, "
            f"人物敌方={len(round_grid.enemy_units)}, "
            f"环境目标={len(round_grid.environment_units)}"
        )

        if not allies:
            # 大地图下我方位置可能临时不可见（如相机被推到远端）。
            # 有方向记忆或环境目标模式时跳过本回合，等下一回合重新建图；
            # 否则才判负，避免因为一次扫描失败抹掉整场战斗。
            if pursuit_direction is None and not environment_mode:
                logger.warning("当前稳定画面未发现我方单位，停止主动战斗")
                return _ActiveRoundResult(ActiveRoundStatus.FAILED)
            logger.warning(
                "当前画面无我方但已有追击方向 "
                f"{pursuit_direction}，本回合跳过行动"
            )
        if (
            not round_grid.enemy_units
            and pursuit_direction is None
            and not environment_mode
        ):
            logger.error("当前视野无敌人且全地图搜索没有给出追击方向")
            return _ActiveRoundResult(ActiveRoundStatus.FAILED)

        return _ActiveRoundResult(
            ActiveRoundStatus.READY,
            state=_ActiveRoundState(
                grid=round_grid,
                reference=round_reference,
                allies=allies,
                pursuit_direction=pursuit_direction,
                hidden_enemy_cell=hidden_enemy_cell,
                environment_mode=environment_mode,
            ),
        )

    # ====================
    # 角色行动
    # ====================

    def _act_with_allies(
        self, context: Context, state: _ActiveRoundState
    ) -> _RoundOutcome:
        """依次调度本回合的我方角色，返回本回合的整体结论。"""
        outcome = _RoundOutcome()
        round_grid = state.grid
        round_reference = state.reference
        pursuit_direction = state.pursuit_direction
        hidden_enemy_cell = state.hidden_enemy_cell
        environment_mode = state.environment_mode
        used_cells: Set[Tuple[int, int]] = set()

        for ally_index, (planned_row, planned_col) in enumerate(state.allies):
            if context.tasker.stopping:
                logger.info("任务执行被停止")
                outcome.result = False
                return outcome

            # 前一个角色可能移动并改变场景。当前角色行动前必须重新截图，
            # 不能继续使用回合开始时保存的 Cell / unit_center。
            current_img = screencap(context)
            if current_img is None:
                logger.error("角色行动前截图失败")
                outcome.result = False
                return outcome

            battle_result = self._detect_battle_result(context, current_img)
            if battle_result is not None:
                outcome.result = battle_result
                return outcome

            current_grid = BattleGrid()
            self.scanner.scan_grid(current_grid, context, current_img)
            self._merge_world_view(
                current_grid,
                self._session.confirmed_rounds,
                replace_visible=True,
            )
            action_pursuit = pursuit_direction
            action_hidden_enemy_cell = hidden_enemy_cell
            current_targets = list(current_grid.enemy_units)
            if not current_targets and current_grid.environment_units:
                # 同一个祭坛的红条会随受击/遮挡帧在“扁平人物条”和
                # “数字+长条环境目标”之间切换。只要它仍在当前动作帧，
                # 就统一作为目标，不因分类抖动重新启动全图搜索。
                current_targets = list(current_grid.environment_units)
                environment_mode = True
                logger.debug(
                    "当前动作帧将祭坛/石碑类红色目标纳入候选，"
                    f"数量={len(current_targets)}"
                )
            if not current_targets and environment_mode:
                # 搜索帧刚确认的祭坛可能因人物动画、树木或底栏遮挡，
                # 在选人前的下一帧暂时失去红条。镜头在此期间没有
                # 移动，因此只为当前动作沿用同视野、同回合的目标；
                # 角色一旦移动，外层会立即结束本回合并重新建图。
                current_targets = list(round_grid.environment_units)
                if current_targets:
                    logger.debug(
                        "选人前环境目标短暂被遮挡，沿用本回合搜索帧"
                        f"已确认的 {len(current_targets)} 个目标"
                    )
            if (
                not current_targets
                and action_pursuit is None
                and self._last_known_enemy_direction is not None
            ):
                action_pursuit = self._last_known_enemy_direction
                logger.debug(
                    "当前动作帧仍无可见敌人，沿用本回合隐匿/远端方向 "
                    f"{action_pursuit}"
                )
            if not current_targets and action_pursuit is None:
                if environment_mode:
                    logger.debug(
                        "环境目标在当前角色快照中不可见，"
                        "跳过本角色并在下一回合重新建图"
                    )
                    continue
                (
                    action_pursuit,
                    action_hidden_enemy_cell,
                    environment_mode,
                ) = self._world_pursuit(current_grid)
                if action_pursuit is None:
                    logger.debug(
                        "当前角色帧没有可用全局目标，跳过角色；"
                        "下一回合重新触发探索"
                    )
                    continue

            current_ally = self._nearest_available_ally(
                current_grid,
                planned_row,
                planned_col,
                used_cells,
            )
            if current_ally is None:
                logger.warning(
                    f"角色 {ally_index} 在新快照中找不到，跳过本次行动"
                )
                continue

            used_cells.add((current_ally.row, current_ally.col))
            logger.debug(
                f"选择角色 {ally_index}: grid=({current_ally.row},{current_ally.col})"
            )
            select_x, select_y = current_ally.safe_click_point()
            if not self._click_and_log(
                context, select_x, select_y, f"选择我方角色 {ally_index}"
            ):
                logger.error("选择我方角色的控制器点击失败")
                outcome.result = False
                return outcome

            # 选中后左上资料卡会遮住敌人、黄色攻击角标和点击点。
            # 实机确认顶部收起按钮只隐藏卡片，不会取消选中或范围。
            if not self._dismiss_role_card(context):
                logger.error("选人后无法收起人物卡片，停止以避免盲目移动")
                outcome.result = False
                return outcome

            # 4 倍速下覆盖层可能在 0.6 秒等待期间已经变化。短间隔连续取帧，
            # 并让敌人和范围始终来自同一张截图。
            selected_state = self._capture_selected_state(
                context, current_targets
            )
            if selected_state is None:
                logger.error("选择角色后截图失败")
                outcome.result = False
                return outcome
            selected_img, selected_grid, selected_range_count = selected_state
            selected_ally = current_ally

            self._log_matrix(selected_grid, selected_ally)
            if selected_range_count == 0:
                logger.warning(
                    "选人后连续取帧仍未识别到攻击/移动范围；"
                    "本次选择未确认或该单位当前无可用行动"
                )
                self._cancel_selection(context)
                continue

            acted, moved_to, action_advanced_round = self._decide_and_act(
                context,
                selected_grid,
                selected_ally,
                selected_img,
                round_reference,
                action_pursuit,
                action_hidden_enemy_cell,
            )
            if moved_to is not None:
                # 移动后下一张截图里的同一角色已经换了格子。把新位置也
                # 标记为已处理，避免它被误当成下一名尚未行动的角色。
                used_cells.add(moved_to)
            # 当前角色只消费一次本回合追击方向；跨回合方向记忆仍保留，
            # 下一回合若仍看不到敌人，将直接沿记忆方向继续推进。
            pursuit_direction = None

            if not acted:
                logger.warning("存在动作范围但没有安全合法目标，已取消本次选择")
                self._cancel_selection(context)
            elif not action_advanced_round and not self._dismiss_role_card(
                context
            ):
                logger.error("动作完成后无法收起人物卡片，停止后续角色行动")
                outcome.result = False
                return outcome

            if acted:
                outcome.had_action = True

            if action_advanced_round:
                logger.debug("动作后常驻回合数字已变化，游戏已自动进入下一回合")
                outcome.advanced = True

            if outcome.advanced:
                break
            if moved_to is not None and action_pursuit is not None:
                logger.debug(
                    "远端追击移动已确认成功；停止调度其余角色并结束本回合，"
                    "下一回合重新建图后继续接近或攻击"
                )
                break

        return outcome

    # ====================
    # 单角色决策
    # ====================

    @staticmethod
    def _log_matrix(grid: BattleGrid, ally: Cell) -> None:
        matrix_lines = []
        for row in range(ROWS):
            row_values = []
            for col in range(COLS):
                cell = grid.cells[row][col]
                if row == ally.row and col == ally.col:
                    base = "A"
                elif cell.cell_type == CellType.ENEMY:
                    base = "E"
                elif cell.cell_type == CellType.FRIEND:
                    base = "F"
                elif cell.cell_type == CellType.SELF:
                    base = "S"
                else:
                    base = "0"
                if cell.is_attackable:
                    base += "1"
                if cell.is_moveable:
                    base += "2"
                row_values.append(base)
            matrix_lines.append(" ".join(row_values))

        logger.debug(
            "统一矩阵 (A=当前, E=敌人, F=友军, S=我方, 1=可攻击, 2=可移动):\n"
            + "\n".join(matrix_lines)
        )
