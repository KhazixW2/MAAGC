import os
import time
from typing import Any, Iterator, List, Optional, Set, Tuple

import cv2
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import logger

from .battle_grid import (
    BattleGrid,
    Cell,
    CellType,
    GridScanner,
    ROWS,
    COLS,
    CELL_HEIGHT,
    CELL_WIDTH,
)
from .battle_world_map import (
    ENEMY as WORLD_ENEMY,
    ENVIRONMENT as WORLD_ENVIRONMENT,
    SELF as WORLD_SELF,
    BattleSessionRegistry,
    BattleSessionState,
    choose_move_candidate,
    session_key,
)


@AgentServer.custom_action("AutoFightProcessor")
class AutoFightProcessor(CustomAction):
    """先被动等待反击，超过指定回合后执行主动战斗。

    策略：前 20 轮全部依赖「结束回合 + 自动反击」，让敌人自己走过来；
    仅当 20 轮后仍未通关时，才进入主动搜索与追击模式。这样可以减少决策
    次数，避免每局都触发 16 视野螺旋搜索的低性价比操作。
    """

    # A battle can legitimately last well beyond the old per-invocation limit
    # of 40.  The session survives recovery calls; this high ceiling is only a
    # final safety fuse, while the normal stop condition is a verified result.
    MAX_ACTION_CYCLES = 300
    MAX_NO_PROGRESS_CYCLES = 12
    PASSIVE_ROUNDS = 20
    ACTIVE_FROM_ROUND = PASSIVE_ROUNDS + 1
    TEST_ACTIVE_FROM_ROUND_ENV = "MAAGC_TEST_ACTIVE_FROM_ROUND"
    MAX_SEARCH_SWIPES = 16
    MAX_STRAIGHT_SEARCH_SWIPES = 3
    # 右下角“回合：N”中的数字是常驻 HUD，不依赖一闪而过的回合提示。
    # 720x1280 下只比较数字区域，人物移动或镜头滚动不会污染这个信号。
    ROUND_DIGIT_ROI = (670, 1175, 20, 25)
    # 两位数回合只改变个位时，固定的十位会稀释 ROI 平均差异；同一回合
    # 连续稳定帧实测差异为 0，因此使用 3.0 仍能避开静态噪声。
    ROUND_CHANGE_THRESHOLD = 10.0
    ROUND_STABLE_THRESHOLD = 1.5
    ROUND_CONFIRM_FRAMES = 2
    # 最外侧约 90px 虽然还能点到状态条，但单位会挡住视野外的敌人，
    # 黄色攻击框也容易被裁切；先平移镜头回到中部再做局部建图。
    ACTION_SAFE_X = (90, 630)
    # 战斗区域实际延伸到 y=1160；地图位于下边界时，状态条会停在
    # y≈1150，仍在 HUD 上沿之外，可以安全点击。
    ACTION_SAFE_Y = (100, 1155)
    SELECT_CAPTURE_ATTEMPTS = 4
    SELECT_CAPTURE_DELAY = 0.12
    MOVE_TARGET_MIN_ALLY_DISTANCE = 105
    # 隐匿单位没有红色生命条，但底部“危险”开关仍会绘制其攻击覆盖区。
    # 只接受至少接近一个 120x120 战斗格的大连通框，排除红旗、服装和
    # 场景中的红色碎片；覆盖区只用于给出粗粒度追击方向，不冒充敌人坐标。
    THREAT_REGION_MIN_BOX_AREA = 10000
    THREAT_REGION_MIN_WIDTH = 90
    THREAT_REGION_MIN_HEIGHT = 60
    THREAT_DIRECTION_DEAD_ZONE = 60
    # 威胁色块描在敌人可攻击的格子上，敌人的立绘会把自己所在格的
    # 红色遮罩遮住。因此不能把整片红区的包围盒中心当作敌人；要找的是
    # 被红色威胁格从四周包住的“锯齿/空洞”中心。
    THREAT_ORIGIN_CORE_RADIUS = 20
    THREAT_ORIGIN_RING_RADIUS = 105
    THREAT_ORIGIN_SAMPLE_STEP = 12
    THREAT_ORIGIN_MIN_DISTANCE_FROM_ALLY = 55
    THREAT_ORIGIN_MIN_SCORE = 0.38
    # 威胁覆盖层与战斗网格严格按 120px 对齐。敌人的立绘会遮住自身格的
    # 中心，但仍会露出一部分红色；以此识别“锯齿中间”的具体敌人格。
    THREAT_CELL_MIN_COVERAGE = 0.08
    THREAT_CELL_MAX_COVERAGE = 0.70
    THREAT_CELL_CENTER_MAX_COVERAGE = 0.10
    THREAT_CELL_NEIGHBOR_MIN_COVERAGE = 0.20
    THREAT_CELL_MIN_RED_NEIGHBORS = 2
    ENEMY_OBSERVATION_MAX_AGE = 0
    ENVIRONMENT_OBSERVATION_MAX_AGE = 2

    def __init__(self) -> None:
        super().__init__()
        self.scanner = GridScanner()
        self._hidden_enemy_cell: Optional[Tuple[int, int]] = None
        self._coarse_threat_direction: Optional[Tuple[int, int]] = None
        self._last_confirmed_move_direction: Optional[Tuple[int, int]] = None
        self._threat_overlay_safe = True
        self._session: Optional[BattleSessionState] = None
        self._session_key = ""

    # 大地图关卡下敌我可能长期不在同一视野。超过该回合数仍未观察到
    # 敌人方向记忆时，主动丢弃记忆并切回纯螺旋搜索，避免绕远路。
    ENEMY_DIRECTION_MEMORY_ROUNDS = 6

    def run(
        self, context: Context, _argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        """执行整场战斗；恢复调用继续使用同一份地图和回合状态。"""
        self._session_key = session_key(context)
        self._session = BattleSessionRegistry.begin(self._session_key)
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
        last_known_enemy_direction: Optional[Tuple[int, int]] = None
        # 自上次成功记录方向以来经过的回合数；超过阈值后丢弃记忆。
        last_known_enemy_age: int = 0

        # 上一次任务可能在全图搜索中途被外部停止，导致红色危险覆盖层
        # 残留。该覆盖层会污染敌方红色状态条识别；进入任何正式决策前
        # 都先恢复为关闭状态，只有敌人搜索窗口才临时开启。
        if not self._set_threat_overlay(context, False):
            logger.warning("战斗入口未能确认危险覆盖层关闭，继续谨慎识别")

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

            # 跨回合敌人方向记忆：每个回合开头累加年龄，超期丢弃，
            # 避免方向记忆过期后让角色朝着错误方向走好几回合。
            if last_known_enemy_direction is not None:
                last_known_enemy_age += 1
                if last_known_enemy_age > self.ENEMY_DIRECTION_MEMORY_ROUNDS:
                    logger.warning(
                        f"敌人方向记忆超过 {self.ENEMY_DIRECTION_MEMORY_ROUNDS} "
                        "回合未刷新，丢弃记忆并切回纯螺旋搜索"
                    )
                    last_known_enemy_direction = None
                    last_known_enemy_age = 0

            # 结算页的红蓝装饰可能被范围节点命中。胜负检测必须先于
            # “残留选择清理”，否则任务完成页会被误当成仍有攻击范围，
            # 点击 Esc 后仍残留并导致流程错误退出。
            terminal_img = self._screencap(context)
            if terminal_img is None:
                logger.error("战斗入口截图失败")
                return CustomAction.RunResult(success=False)
            terminal_result = self._detect_battle_result(
                context, terminal_img
            )
            if terminal_result is not None:
                return CustomAction.RunResult(success=terminal_result)

            # 外部停止或失败动作可能留下“人物已选中但卡片已收起”的状态。
            # 这时黄色/蓝色行动范围会污染红条扫描，必须先真正点右上 Esc
            # 取消选择，不能把顶部中间的卡片按钮误当成取消按钮。
            if not self._clear_stale_selection(context):
                logger.error("回合建图前无法清除残留选择状态")
                return CustomAction.RunResult(success=False)

            # 上一名角色也可能只留下了人物卡片。它会遮住红条、黄色攻击
            # 角标和目标点击点，正式建图前仍需验证收起。
            if not self._dismiss_role_card(context):
                logger.error("回合建图前无法收起人物卡片，停止以避免盲目移动")
                return CustomAction.RunResult(success=False)

            # 上一轮的威胁层若因动画/识别延迟未关闭，红色覆盖会污染本轮
            # 的敌方与环境目标扫描。只有确认普通画面恢复后才允许建图。
            if not self._set_threat_overlay(context, False):
                logger.warning("回合建图前威胁层仍开启，本回合只结束等待恢复")
                recovery_reference = self._screencap(context)
                if recovery_reference is None:
                    return CustomAction.RunResult(success=False)
                recovery_result = self._end_round(
                    context, recovery_reference, "威胁层恢复失败"
                )
                if recovery_result is not None:
                    return CustomAction.RunResult(success=recovery_result)
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

            game_round = self._session.confirmed_rounds + 1
            logger.debug(
                f"战斗回合 {game_round} / 动作循环 "
                f"{self._session.action_cycles}：建立当前战场快照"
            )
            round_reference = self._screencap(context)
            if round_reference is None:
                logger.error("建立回合快照失败")
                return CustomAction.RunResult(success=False)

            battle_result = self._detect_battle_result(context, round_reference)
            if battle_result is not None:
                return CustomAction.RunResult(success=battle_result)

            # 所有任务统一先完整结束 20 个回合。判断依据存放在整场会话中，
            # AutoFightProcessor 被恢复逻辑再次调用时不会重新等待 20 回合。
            use_active_strategy = self._session.confirmed_rounds >= passive_rounds
            if not use_active_strategy:
                logger.debug(
                    f"第 {game_round} 回合：统一坚守反击 "
                    f"({self._session.confirmed_rounds}/{passive_rounds})，结束回合"
                )
                passive_result = self._end_round(
                    context, round_reference, "被动阶段"
                )
                if passive_result is not None:
                    return CustomAction.RunResult(success=passive_result)
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

            round_grid = BattleGrid()
            self.scanner.scan_grid(round_grid, context, round_reference)
            self._merge_world_view(
                round_grid,
                self._session.confirmed_rounds,
                replace_visible=True,
            )

            if self._session.confirmed_rounds == passive_rounds:
                logger.info(
                    f"战斗到达第 {game_round} 回合仍未结束，"
                    "切换为主动搜索与追击模式"
                )

            world = self._session.world
            if not world.has_allies() or not self._world_has_fresh_targets():
                logger.info(
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
                    return CustomAction.RunResult(success=False)
                latest = self._screencap(context)
                if latest is None:
                    return CustomAction.RunResult(success=False)
                wait_result = self._end_round(context, latest, "全局建图等待")
                if wait_result is not None:
                    return CustomAction.RunResult(success=wait_result)
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

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
                    wait_result = self._end_round(
                        context, round_reference, "局部威胁刷新失败"
                    )
                    if wait_result is not None:
                        return CustomAction.RunResult(success=wait_result)
                    self._wait_for_scene_settle(context, timeout=6.0)
                    continue
                normal_img = self._screencap(context)
                if normal_img is None:
                    return CustomAction.RunResult(success=False)
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
                        normal_img = self._screencap(context)
                        if normal_img is None:
                            return CustomAction.RunResult(success=False)
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
                    latest = self._screencap(context)
                    if latest is None:
                        return CustomAction.RunResult(success=False)
                    wait_result = self._end_round(
                        context, latest, "敌人位置失效"
                    )
                    if wait_result is not None:
                        return CustomAction.RunResult(success=wait_result)
                    self._wait_for_scene_settle(context, timeout=6.0)
                    continue
                round_grid, round_reference = focused

            pursuit_direction, hidden_enemy_cell, environment_mode = (
                self._world_pursuit(round_grid)
            )
            last_known_enemy_direction = pursuit_direction
            last_known_enemy_age = 0
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
                if (
                    pursuit_direction is None
                    and not environment_mode
                    and last_known_enemy_direction is None
                ):
                    logger.warning("当前稳定画面未发现我方单位，停止主动战斗")
                    return CustomAction.RunResult(success=False)
                logger.warning(
                    "当前画面无我方但已有追击方向 "
                    f"{pursuit_direction or last_known_enemy_direction}，"
                    "本回合跳过行动"
                )
            if (
                not round_grid.enemy_units
                and pursuit_direction is None
                and not environment_mode
                and last_known_enemy_direction is None
            ):
                logger.error("当前视野无敌人且全地图搜索没有给出追击方向")
                return CustomAction.RunResult(success=False)

            used_cells: Set[Tuple[int, int]] = set()
            round_advanced = False
            round_had_action = False
            for ally_index, (planned_row, planned_col) in enumerate(allies):
                if context.tasker.stopping:
                    logger.info("任务执行被停止")
                    return CustomAction.RunResult(success=False)

                # 前一个角色可能移动并改变场景。当前角色行动前必须重新截图，
                # 不能继续使用回合开始时保存的 Cell / unit_center。
                current_img = self._screencap(context)
                if current_img is None:
                    logger.error("角色行动前截图失败")
                    return CustomAction.RunResult(success=False)

                battle_result = self._detect_battle_result(context, current_img)
                if battle_result is not None:
                    return CustomAction.RunResult(success=battle_result)

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
                    and last_known_enemy_direction is not None
                ):
                    action_pursuit = last_known_enemy_direction
                    logger.debug(
                        "当前动作帧仍无可见敌人，沿用本回合隐匿/远端方向 "
                        f"{action_pursuit}"
                    )
                if not current_targets and action_pursuit is None:
                    if environment_mode:
                        logger.info(
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
                        logger.info(
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
                    return CustomAction.RunResult(success=False)

                # 选中后左上资料卡会遮住敌人、黄色攻击角标和点击点。
                # 实机确认顶部收起按钮只隐藏卡片，不会取消选中或范围。
                if not self._dismiss_role_card(context):
                    logger.error("选人后无法收起人物卡片，停止以避免盲目移动")
                    return CustomAction.RunResult(success=False)

                # 4 倍速下覆盖层可能在 0.6 秒等待期间已经变化。短间隔连续取帧，
                # 并让敌人和范围始终来自同一张截图。
                selected_state = self._capture_selected_state(
                    context, current_targets
                )
                if selected_state is None:
                    logger.error("选择角色后截图失败")
                    return CustomAction.RunResult(success=False)
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
                    return CustomAction.RunResult(success=False)

                if acted:
                    round_had_action = True

                if action_advanced_round:
                    logger.info("动作后常驻回合数字已变化，游戏已自动进入下一回合")
                    round_advanced = True

                if round_advanced:
                    break
                if moved_to is not None and action_pursuit is not None:
                    logger.info(
                        "远端追击移动已确认成功；停止调度其余角色并结束本回合，"
                        "下一回合重新建图后继续接近或攻击"
                    )
                    break

            if round_advanced:
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

            if not round_had_action:
                no_progress = self._session.record_no_progress()
                logger.warning(
                    "主动回合没有确认任何攻击或移动: "
                    f"{no_progress}/{self.MAX_NO_PROGRESS_CYCLES}"
                )
                if no_progress >= self.MAX_NO_PROGRESS_CYCLES:
                    logger.error("连续主动回合没有可验证动作，停止进入外层恢复")
                    return CustomAction.RunResult(success=False)

            # 最后一名角色行动后，游戏可能自行结束我方回合。留一个短暂
            # 宽限期，仅当常驻回合数字没有变化时才点击结束回合。
            if self._wait_for_round_change(context, round_reference, timeout=1.5):
                logger.info("检测到自动换回合，跳过结束回合按钮")
                self._record_round_advance("角色行动后自动换回合")
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

            end_result = self._end_round(
                context, round_reference, "主动阶段"
            )
            if end_result is not None:
                return CustomAction.RunResult(success=end_result)
            self._wait_for_scene_settle(context, timeout=6.0)

        logger.error(
            f"战斗达到安全动作循环上限 {self.MAX_ACTION_CYCLES}，停止任务"
        )
        return CustomAction.RunResult(success=False)

    def _decide_and_act(
        self,
        context: Context,
        grid: BattleGrid,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        pursuit_direction: Optional[Tuple[int, int]],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """返回 ``(是否生效, 移动后的格子, 是否已经换回合)``。"""
        attack_targets = [
            cell
            for cell in grid.enemy_units
            if cell.is_attackable and not cell.is_moveable
        ]
        if not attack_targets:
            attack_targets = self._promote_occluded_adjacent_targets(grid, ally)
        conflicted_targets = [
            cell
            for cell in grid.enemy_units
            if cell.is_attackable and cell.is_moveable
        ]
        if conflicted_targets:
            logger.warning(
                "拒绝 E12 冲突格: "
                + str([(cell.row, cell.col) for cell in conflicted_targets])
            )

        logger.debug(
            "可攻击敌人位置(E1): "
            + str([(cell.row, cell.col) for cell in attack_targets])
        )
        if attack_targets:
            target = min(
                attack_targets,
                key=lambda cell: self._screen_distance(cell, ally, "attack"),
            )
            logger.info(f"攻击目标格子: ({target.row}, {target.col})")
            if not self._click_cell(context, target, "攻击"):
                return False, None, False
            accepted, advanced = self._verify_action_result(
                context,
                before_img,
                round_reference,
                "攻击",
                ally,
                target,
            )
            if accepted or advanced:
                if accepted and self._session is not None:
                    self._session.record_progress()
                return accepted, None, advanced

            logger.warning(
                "黄色综合范围内的敌人未能实际攻击，"
                "重新选择我方并回退为向敌人移动"
            )
            if not self._cancel_selection(context):
                return False, None, False
            ally_x, ally_y = ally.safe_click_point()
            if not self._click_and_log(
                context, ally_x, ally_y, "攻击失败后重新选择我方"
            ):
                return False, None, False
            fallback_state = self._capture_selected_state(
                context, list(grid.enemy_units)
            )
            if fallback_state is None:
                return False, None, False
            before_img, grid, _ = fallback_state

        hidden_target = (
            grid.get_cell(*hidden_enemy_cell)
            if hidden_enemy_cell is not None
            else None
        )
        if (
            not attack_targets
            and hidden_target is not None
            and self._is_adjacent_cell(ally, hidden_target)
        ):
            # 隐匿敌人没有红色血条，人物立绘还可能遮住黄色攻击角标。
            # 这时 A* 会正确判断我方已经站在攻击环，却无法从 E1 得到
            # 攻击目标。锯齿中心来自当前威胁层、且与所选我方八方向相邻
            # 时，按游戏的点对点规则直接双击该格；远端地图记忆不走此
            # 分支，避免把过期坐标当成可点击敌人。
            logger.info(
                "相邻锯齿威胁中心虽无黄色角标，直接双击攻击格子: "
                f"({hidden_target.row}, {hidden_target.col})"
            )
            if not self._click_cell(context, hidden_target, "相邻威胁中心攻击"):
                return False, None, False
            accepted, advanced = self._verify_action_result(
                context,
                before_img,
                round_reference,
                "相邻威胁中心攻击",
                ally,
                hidden_target,
            )
            if accepted or advanced:
                if accepted and self._session is not None:
                    self._session.record_progress()
                return accepted, None, advanced
            logger.warning(
                "相邻威胁中心双击未得到胜利、回合变化或血条变化确认，"
                "取消选择后下回合重建威胁图"
            )
            self._cancel_selection(context)
            return False, None, False

        move_targets = [
            cell
            for row in grid.cells
            for cell in row
            if cell.is_moveable
            and not cell.is_attackable
            and cell.cell_type == CellType.NONE
        ]
        move_targets = self._without_ally_occlusion(move_targets, ally)
        logger.debug(
            "可移动空白位置(2): "
            + str([(cell.row, cell.col) for cell in move_targets])
        )
        if not move_targets:
            if grid.enemy_units:
                # 点击未被黄色范围确认的敌人只会打开资料面板，并不会攻击。
                # 把当前角色视为本轮不可用，交给外层循环改选下一名我方；
                # 不能用一次假点击消耗动作预算并污染后续截图。
                logger.info(
                    "当前角色没有已确认的攻击格或移动格，跳过并改选下一名我方"
                )
            return False, None, False

        target: Optional[Cell] = None
        used_world_plan = False
        if self._session is not None:
            world = self._session.world
            ally_world = world.local_to_world(ally.row, ally.col)
            current_local_targets = [
                (cell.row, cell.col) for cell in grid.enemy_units
            ]
            if current_local_targets:
                world_targets = [
                    world.local_to_world(row, col)
                    for row, col in current_local_targets
                ]
            elif hidden_enemy_cell is not None:
                world_targets = [world.local_to_world(*hidden_enemy_cell)]
            else:
                fresh_enemies, fresh_environment = self._fresh_world_targets()
                world_targets = fresh_enemies or fresh_environment

            # One action follows one current target.  Feeding every remembered
            # threat point into the attack-ring planner made the goal switch
            # between stale points and produced short orbits around the enemy.
            if world_targets:
                nearest_target = min(
                    world_targets,
                    key=lambda point: (
                        max(
                            abs(point[0] - ally_world[0]),
                            abs(point[1] - ally_world[1]),
                        ),
                        point,
                    ),
                )
                world_targets = [nearest_target]
            candidate_cells = {
                world.local_to_world(cell.row, cell.col): cell
                for cell in move_targets
            }
            if world_targets:
                used_world_plan = True
                selected_world = choose_move_candidate(
                    ally_world,
                    list(candidate_cells),
                    world_targets,
                    blocked=world.blocked,
                    explored=world.observed,
                    last_direction=self._session.last_move_direction,
                    recent_positions=self._session.recent_move_points,
                )
                if selected_world is not None:
                    target = candidate_cells[selected_world]
                    logger.debug(
                        "八方向 A* 选择本回合落点: "
                        f"ally={ally_world}, target={selected_world}, "
                        f"enemy={world_targets[0]}"
                    )

        if target is not None:
            pass
        elif used_world_plan:
            logger.info(
                "我方已在目标攻击环或没有严格缩短路径的落点，"
                "本回合不绕行，等待攻击框/敌方位置刷新"
            )
            return False, None, False
        elif grid.enemy_units:
            nearest_enemy = min(
                grid.enemy_units,
                key=lambda enemy: self._screen_distance(enemy, ally, "attack"),
            )
            ally_x, ally_y = ally.safe_click_point()
            enemy_x, enemy_y = nearest_enemy.safe_click_point()
            direction_x = enemy_x - ally_x
            direction_y = enemy_y - ally_y
            target = self._farthest_move_target(
                move_targets,
                ally,
                direction_x,
                direction_y,
                nearest_enemy,
            )
            if not self._move_reduces_enemy_distance(
                target, ally, nearest_enemy
            ):
                logger.info(
                    "局部图没有直接缩短敌我距离的蓝格，"
                    "选择代价最小的八方向绕障格"
                )
            logger.info(
                "当前视野有敌人但没有可信攻击目标，"
                f"向敌人 ({nearest_enemy.row}, {nearest_enemy.col}) "
                "选择最远移动格"
            )
        elif pursuit_direction is not None:
            direction_x, direction_y = pursuit_direction
            hidden_target = (
                grid.get_cell(*hidden_enemy_cell)
                if hidden_enemy_cell is not None
                else None
            )
            if hidden_target is not None and hidden_target.is_attackable:
                logger.info(
                    "锯齿威胁中心已进入攻击范围，直接攻击格子: "
                    f"({hidden_target.row}, {hidden_target.col})"
                )
                if not self._click_cell(context, hidden_target, "威胁中心攻击"):
                    return False, None, False
                accepted, advanced = self._verify_action_result(
                    context,
                    before_img,
                    round_reference,
                    "威胁中心攻击",
                    ally,
                    hidden_target,
                )
                if accepted or advanced:
                    if accepted and self._session is not None:
                        self._session.record_progress()
                    return accepted, None, advanced
                logger.warning("威胁中心攻击未被游戏接受，取消选择后下回合重试")
                self._cancel_selection(context)
                return False, None, False
            coarse_move_targets = self._coarse_move_candidates(
                move_targets, ally
            )
            if not coarse_move_targets:
                return False, None, False
            target = self._farthest_move_target(
                coarse_move_targets,
                ally,
                direction_x,
                direction_y,
                hidden_target,
            )
            projection = (
                (target.col - ally.col) * direction_x
                + (target.row - ally.row) * direction_y
            )
            if projection < 0:
                logger.info(
                    f"直达方向 {pursuit_direction} 被障碍阻挡，"
                    "按局部图选择代价最小的八方向绕障格"
                )
            if projection == 0:
                logger.info(
                    f"直达方向 {pursuit_direction} 被障碍阻挡，"
                    "选择八方向中的垂直绕行格"
                )
            logger.info(
                f"当前视野无敌人，按远端方向 {pursuit_direction} "
                f"选择投影={projection} 的移动格"
            )
        else:
            return False, None, False
        logger.info(f"移动目标格子: ({target.row}, {target.col})")
        if not self._click_cell(context, target, "移动"):
            return False, None, False
        accepted, advanced = self._verify_action_result(
            context,
            before_img,
            round_reference,
            "移动",
            ally,
            target,
        )
        destination = (target.row, target.col) if accepted else None
        if accepted:
            self._last_confirmed_move_direction = self._grid_direction(
                ally, target
            )
            if self._session is not None:
                origin_world = self._session.world.local_to_world(
                    ally.row, ally.col
                )
                target_world = self._session.world.local_to_world(
                    target.row, target.col
                )
                row_direction = (
                    0
                    if target_world[0] == origin_world[0]
                    else (1 if target_world[0] > origin_world[0] else -1)
                )
                col_direction = (
                    0
                    if target_world[1] == origin_world[1]
                    else (1 if target_world[1] > origin_world[1] else -1)
                )
                self._session.last_move_direction = (
                    row_direction,
                    col_direction,
                )
                self._session.world.record_predicted_move(
                    origin_world,
                    target_world,
                    self._session.confirmed_rounds,
                )
                self._session.record_move_point(target_world)
                self._session.record_progress()
            logger.debug(
                "记录局部路径移动方向: "
                f"{self._last_confirmed_move_direction}"
            )
        return accepted, destination, advanced

    @staticmethod
    def _is_adjacent_cell(first: Cell, second: Cell) -> bool:
        """八方向相邻，不把同格或远端世界地图记忆当作可攻击目标。"""
        row_distance = abs(first.row - second.row)
        col_distance = abs(first.col - second.col)
        return max(row_distance, col_distance) == 1

    def _without_immediate_backtrack(
        self, move_targets: List[Cell], ally: Cell
    ) -> List[Cell]:
        """排除上一移动方向后方的半平面，避免下一步大角度掉头。"""
        last_direction = self._last_confirmed_move_direction
        if last_direction is None:
            return move_targets
        candidates = [
            cell
            for cell in move_targets
            if (
                self._grid_direction(ally, cell)[0] * last_direction[0]
                + self._grid_direction(ally, cell)[1] * last_direction[1]
                >= 0
            )
        ]
        removed = len(move_targets) - len(candidates)
        if removed:
            logger.debug(
                f"局部路径排除 {removed} 个位于上一方向 "
                f"{last_direction} 反向半平面的蓝格"
            )
        if candidates:
            return candidates
        logger.info("局部图只剩大角度回退格，本回合不移动并等待地图变化")
        return []

    def _coarse_move_candidates(
        self, move_targets: List[Cell], ally: Cell
    ) -> List[Cell]:
        """Constrain direction-only pursuit without blocking real A* paths.

        A coarse threat direction can flip after every enemy turn.  Applying
        it blindly produced a three-cell orbit because the next move was often
        the exact reverse of the previous one.  Reject that immediate reversal
        for one turn and prefer cells not visited in the latest short history.
        Precise world-target A* planning intentionally bypasses this helper so
        it can still backtrack around a real obstacle when required.
        """

        candidates = self._without_immediate_backtrack(move_targets, ally)
        if not candidates:
            # Wait one turn, then permit a reversal if the refreshed threat
            # direction still requires it.  This breaks ping-pong movement
            # without permanently forbidding a necessary retreat.
            self._last_confirmed_move_direction = None
            if self._session is not None:
                self._session.last_move_direction = None
            return []

        if self._session is None or not self._session.recent_move_points:
            return candidates
        recent = set(self._session.recent_move_points[-4:])
        fresh = [
            cell
            for cell in candidates
            if self._session.world.local_to_world(cell.row, cell.col)
            not in recent
        ]
        if fresh:
            logger.debug(
                f"粗方向追击优先 {len(fresh)} 个近期未访问移动格"
            )
            return fresh
        return candidates

    def _without_ally_occlusion(
        self, move_targets: List[Cell], ally: Cell
    ) -> List[Cell]:
        """排除落在当前大体型立绘上的蓝格中心，避免点击被角色截获。"""
        ally_x, ally_y = ally.safe_click_point()
        min_distance_sq = self.MOVE_TARGET_MIN_ALLY_DISTANCE ** 2
        candidates = []
        for cell in move_targets:
            target_x, target_y = cell.action_click_point("move")
            if (target_x - ally_x) ** 2 + (target_y - ally_y) ** 2 >= min_distance_sq:
                candidates.append(cell)
        removed = len(move_targets) - len(candidates)
        if removed:
            logger.debug(f"局部路径排除 {removed} 个被我方立绘覆盖的近邻蓝格")
        return candidates or move_targets

    @staticmethod
    def _grid_direction(first: Cell, second: Cell) -> Tuple[int, int]:
        delta_col = second.col - first.col
        delta_row = second.row - first.row
        return (
            1 if delta_col > 0 else -1 if delta_col < 0 else 0,
            1 if delta_row > 0 else -1 if delta_row < 0 else 0,
        )

    @staticmethod
    def _screen_distance(first: Cell, second: Cell, action: str) -> int:
        first_x, first_y = first.action_click_point(action)
        second_x, second_y = second.safe_click_point()
        return (first_x - second_x) ** 2 + (first_y - second_y) ** 2

    def _promote_occluded_adjacent_targets(
        self, grid: BattleGrid, ally: Cell
    ) -> List[Cell]:
        """Recover an adjacent enemy whose yellow target frame is occluded.

        Large altars and two overlapping units can leave only a few yellow
        corner fragments on the enemy cell.  A selected frame that still has
        other attack-range cells proves the yellow layer is active; combined
        with a current real red status bar in an adjacent cell this is enough
        to attempt the user's normal double-click attack.  Result verification
        still rejects a click that the game does not accept.
        """

        has_attack_layer = any(cell.is_attackable for cell in grid)
        if not has_attack_layer:
            return []
        promoted: List[Cell] = []
        for enemy in grid.enemy_units:
            if enemy.is_attackable or enemy.is_moveable:
                continue
            if max(abs(enemy.row - ally.row), abs(enemy.col - ally.col)) > 1:
                continue
            center_x, bar_y = (
                enemy.target_center
                if enemy.target_center != (0, 0)
                else enemy.safe_click_point()
            )
            enemy.is_attackable = True
            enemy.attack_center = (
                center_x,
                max(1, bar_y - self.scanner.ATTACK_MARKER_Y_OFFSET),
            )
            promoted.append(enemy)
        if promoted:
            logger.info(
                "相邻敌人攻击框被本体遮挡，按红条与攻击层恢复目标: "
                + str([(cell.row, cell.col) for cell in promoted])
            )
        return promoted

    @staticmethod
    def _move_reduces_enemy_distance(
        target: Cell, ally: Cell, enemy: Cell
    ) -> bool:
        """只接受真正接近敌人的落点，屏蔽垂直/水平噪声造成的两格往返。"""
        ally_x, ally_y = ally.safe_click_point()
        target_x, target_y = target.action_click_point("move")
        enemy_x, enemy_y = enemy.safe_click_point()
        before = (ally_x - enemy_x) ** 2 + (ally_y - enemy_y) ** 2
        after = (target_x - enemy_x) ** 2 + (target_y - enemy_y) ** 2
        return after < before

    @classmethod
    def _edge_recenter_direction(
        cls,
        cells: Any,
        *,
        x_bounds: Optional[Tuple[int, int]] = None,
        y_bounds: Optional[Tuple[int, int]] = None,
    ) -> Optional[Tuple[int, int]]:
        """Return a cardinal or diagonal pan that pulls edge units inward."""

        if isinstance(cells, Cell):
            cells = [cells]
        cells = list(cells)
        if not cells:
            return None
        x_min, x_max = x_bounds or cls.ACTION_SAFE_X
        y_min, y_max = y_bounds or cls.ACTION_SAFE_Y

        left_overflow = max(
            (x_min - cell.safe_click_point()[0] for cell in cells), default=0
        )
        right_overflow = max(
            (cell.safe_click_point()[0] - x_max for cell in cells), default=0
        )
        top_overflow = max(
            (y_min - cell.safe_click_point()[1] for cell in cells), default=0
        )
        bottom_overflow = max(
            (cell.safe_click_point()[1] - y_max for cell in cells), default=0
        )
        direction_x = (
            -1
            if left_overflow > max(0, right_overflow)
            else (1 if right_overflow > 0 else 0)
        )
        direction_y = (
            -1
            if top_overflow > max(0, bottom_overflow)
            else (1 if bottom_overflow > 0 else 0)
        )
        return (direction_x, direction_y) if direction_x or direction_y else None

    @staticmethod
    def _farthest_move_target(
        move_targets: List[Cell],
        ally: Cell,
        direction_x: int,
        direction_y: int,
        enemy: Optional[Cell] = None,
    ) -> Cell:
        """选择追击落点。

        已知屏内敌人时，优先选择真正缩短到该敌人距离的格子。不能只取
        方向投影最大值：当一次移动能够越过敌人时，角色会在敌人两侧来回
        横跳，永远无法进入攻击距离。仅有远端搜索方向时才继续按最大投影
        走满移动范围。
        """
        ally_x, ally_y = ally.safe_click_point()
        enemy_point = enemy.safe_click_point() if enemy is not None else None

        if enemy_point is not None:
            current_distance = (
                (ally_x - enemy_point[0]) ** 2
                + (ally_y - enemy_point[1]) ** 2
            )

            def approach_score(cell: Cell) -> Tuple[int, int, int, int]:
                cell_x, cell_y = cell.action_click_point("move")
                delta_x = cell_x - ally_x
                delta_y = cell_y - ally_y
                enemy_distance = (
                    (cell_x - enemy_point[0]) ** 2
                    + (cell_y - enemy_point[1]) ** 2
                )
                projection = delta_x * direction_x + delta_y * direction_y
                displacement = delta_x * delta_x + delta_y * delta_y
                # 同样接近敌人时，选择仍位于敌人这一侧的格子，不能因追击
                # 投影更大而越过锯齿中心，导致在敌人两侧反复横跳。
                overshot = int(
                    (cell_x - enemy_point[0]) * direction_x
                    + (cell_y - enemy_point[1]) * direction_y
                    > 0
                )
                return enemy_distance, overshot, -projection, -displacement

            closer_targets = [
                cell
                for cell in move_targets
                if approach_score(cell)[0] < current_distance
            ]
            candidates = closer_targets or move_targets
            return min(candidates, key=approach_score)

        desired_direction = (
            1 if direction_x > 0 else -1 if direction_x < 0 else 0,
            1 if direction_y > 0 else -1 if direction_y < 0 else 0,
        )

        def score(cell: Cell) -> Tuple[int, int, int, int]:
            cell_x, cell_y = cell.action_click_point("move")
            delta_x = cell_x - ally_x
            delta_y = cell_y - ally_y
            step_direction = AutoFightProcessor._grid_direction(ally, cell)
            # 先比较八方向的一致程度，再比较步数。远端方向只是局部导航
            # 向量，不应驱使角色一次跨越多行；相同方向必须优先点击最近
            # 的蓝格，下一回合重新建图后再走下一步。
            alignment = (
                step_direction[0] * desired_direction[0]
                + step_direction[1] * desired_direction[1]
            )
            angular_error = abs(
                step_direction[0] * desired_direction[1]
                - step_direction[1] * desired_direction[0]
            )
            local_steps = max(
                abs(cell.col - ally.col), abs(cell.row - ally.row)
            )
            displacement = delta_x * delta_x + delta_y * delta_y
            return alignment, -angular_error, -local_steps, -displacement

        return max(move_targets, key=score)

    def _capture_selected_state(
        self, context: Context, known_enemies: List[Cell]
    ) -> Optional[Tuple[Any, BattleGrid, int]]:
        """快速捕获选中后的动作范围，并带入选中前刚确认的真实敌人。"""
        last_state: Optional[Tuple[Any, BattleGrid, int]] = None
        for attempt in range(1, self.SELECT_CAPTURE_ATTEMPTS + 1):
            time.sleep(self.SELECT_CAPTURE_DELAY)
            img = self._screencap(context)
            if img is None:
                continue

            grid = BattleGrid()
            # 蓝色移动覆盖层会命中 Battle_UnitScan_Blue。选中状态只需要重扫
            # 敌方状态条；当前我方坐标沿用点击前的同一帧定位。
            self.scanner.scan_grid(
                grid, context, img, cell_types=(CellType.ENEMY,)
            )
            # 选中覆盖层会把尸体、技能特效和黄色格边缘染成红色碎片。
            # 这里只允许更新选中前同一动作快照已经确认的敌人格，不能在
            # 选中状态中凭空新增敌人并覆盖远端/红旗追击方向。
            known_positions = {
                (enemy.row, enemy.col) for enemy in known_enemies
            }
            for cell in grid:
                if (
                    cell.cell_type == CellType.ENEMY
                    and (cell.row, cell.col) not in known_positions
                ):
                    cell.cell_type = CellType.NONE
                    cell.unit_center = (0, 0)
                    cell.target_center = (0, 0)
                    cell.is_environment_object = False
            # 选中覆盖层会遮住红色血条，导致同帧敌人扫描变成 0。这里仅带入
            # 点击前约 0.1 秒的当前动作快照；每名角色行动前都会重新扫描，
            # 不会把上一动作/上一回合的坐标作为幽灵状态保留下来。
            for known_enemy in known_enemies:
                cell = grid.get_cell(known_enemy.row, known_enemy.col)
                if cell is None:
                    continue
                # 普通敌人若已由当前选中帧重新识别，保留这帧的新坐标。
                # 环境物件则需要临时提升为攻击候选，否则 enemy_units 的全局
                # 过滤会让范围判定永远看不到它。这个提升只存在于当前角色的
                # selected_grid，不会把祭坛带回普通敌人的全图搜索。
                if cell.cell_type != CellType.NONE and not cell.is_environment_object:
                    continue
                cell.cell_type = CellType.ENEMY
                cell.is_environment_object = False
                cell.unit_center = known_enemy.unit_center
                cell.target_center = known_enemy.target_center
            self.scanner.scan_ranges(grid, context, img)
            range_count = sum(
                1
                for row in grid.cells
                for cell in row
                if cell.is_moveable or cell.is_attackable
            )
            last_state = (img, grid, range_count)
            logger.debug(
                f"选中状态取帧 {attempt}/{self.SELECT_CAPTURE_ATTEMPTS}: "
                f"可信敌方={len(grid.enemy_units)}, 动作范围格={range_count}"
            )
            if range_count:
                return last_state
        return last_state

    def _cancel_selection(self, context: Context) -> bool:
        """点击右上 Esc 区域，并验证人物卡片和行动范围同时消失。"""
        result = context.run_task("Battle_Cancel")
        clicked = self._task_result_has_hit(
            result, {"Battle_Cancel"}
        )
        if not clicked:
            logger.warning("右上 Esc 点击失败")
            return False

        after_img = self._screencap(context)
        if after_img is None:
            return False
        range_count = self._action_range_count(context, after_img)
        still_open = context.run_recognition(
            "Battle_SelectedRoleCard", after_img
        )
        cancelled = range_count == 0 and not (still_open and still_open.hit)
        logger.debug(
            f"取消当前选择: Esc点击={clicked}, "
            f"残留行动范围={range_count}, succeeded={cancelled}"
        )
        return cancelled

    def _clear_stale_selection(self, context: Context) -> bool:
        """每轮建图前清理由中断或失败动作留下的选中覆盖层。"""
        img = self._screencap(context)
        if img is None:
            return False
        range_count = self._action_range_count(context, img)
        card = context.run_recognition("Battle_SelectedRoleCard", img)
        if range_count == 0 and not (card and card.hit):
            return True
        logger.warning(
            f"建图前发现残留选择状态: 行动范围={range_count}, "
            f"人物卡片={'有' if card and card.hit else '无'}"
        )
        return self._cancel_selection(context)

    def _action_range_count(self, context: Context, img: Any) -> int:
        grid = BattleGrid()
        self.scanner.scan_ranges(grid, context, img)
        return sum(
            1
            for cell in grid
            if cell.is_moveable or cell.is_attackable
        )

    def _dismiss_role_card(self, context: Context) -> bool:
        """仅在左上人物卡片确实存在时点击收起，并验证卡片消失。"""
        img = self._screencap(context)
        if img is None:
            return False
        card = context.run_recognition("Battle_SelectedRoleCard", img)
        if not card or not card.hit:
            return True

        result = context.run_task("Battle_CollapseRoleCard")
        if not self._task_result_has_hit(result, {"Battle_CollapseRoleCard"}):
            logger.warning("人物卡片收起按钮点击失败")
            return False

        after_img = self._screencap(context)
        if after_img is None:
            return False
        still_open = context.run_recognition(
            "Battle_SelectedRoleCard", after_img
        )
        if still_open and still_open.hit:
            logger.warning("点击顶部按钮后人物卡片仍存在")
            return False
        logger.debug("左上人物卡片已收起")
        return True

    def _merge_world_view(
        self,
        grid: BattleGrid,
        round_seen: int,
        *,
        replace_visible: bool,
    ) -> None:
        """Merge one local ``BattleGrid`` into the persistent battle map."""
        if self._session is None:
            return
        self._session.world.merge_view(
            round_seen,
            allies=((cell.row, cell.col) for cell in grid.self_units),
            enemies=((cell.row, cell.col) for cell in grid.enemy_units),
            friends=((cell.row, cell.col) for cell in grid.friend_units),
            environment=(
                (cell.row, cell.col) for cell in grid.environment_units
            ),
            replace_visible=replace_visible,
        )

    def _fresh_world_targets(
        self,
    ) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
        """Return only target observations that are safe for this decision.

        Enemies can move after every submitted round, so an enemy coordinate
        from an older round is a direction hint at best and must not remain an
        A* goal.  Environment targets are static and may survive two briefly
        occluded frames.
        """

        if self._session is None:
            return [], []
        round_seen = self._session.confirmed_rounds
        world = self._session.world
        enemies = world.unit_points(
            WORLD_ENEMY,
            current_round=round_seen,
            max_age=self.ENEMY_OBSERVATION_MAX_AGE,
        )
        environment = world.unit_points(
            WORLD_ENVIRONMENT,
            current_round=round_seen,
            max_age=self.ENVIRONMENT_OBSERVATION_MAX_AGE,
        )
        return enemies, environment

    def _world_has_fresh_targets(self) -> bool:
        enemies, environment = self._fresh_world_targets()
        return bool(enemies or environment)

    @classmethod
    def _direction_from_screen_target(
        cls, allies: List[Cell], target_x: int, target_y: int
    ) -> Optional[Tuple[int, int]]:
        if not allies:
            return None
        ally_x = sum(cell.safe_click_point()[0] for cell in allies) // len(allies)
        ally_y = sum(cell.safe_click_point()[1] for cell in allies) // len(allies)
        delta_x = target_x - ally_x
        delta_y = target_y - ally_y
        direction_x = (
            0
            if abs(delta_x) < cls.THREAT_DIRECTION_DEAD_ZONE
            else (1 if delta_x > 0 else -1)
        )
        direction_y = (
            0
            if abs(delta_y) < cls.THREAT_DIRECTION_DEAD_ZONE
            else (1 if delta_y > 0 else -1)
        )
        return (direction_x, direction_y) if direction_x or direction_y else None

    def _merge_open_threat_overlay(
        self, context: Context, img: Any, grid: BattleGrid, round_seen: int
    ) -> List[Tuple[int, int]]:
        """Map hidden enemies while the danger overlay is already enabled."""
        if self._session is None or img is None:
            return []
        allies = [
            cell for cell in grid.self_units if self._ally_is_actionable(cell)
        ]
        local_points: List[Tuple[int, int]] = []
        confidence = 0.95
        self._coarse_threat_direction = None

        hidden_cell = self._threat_enemy_cell_from_mask(img, allies)
        if hidden_cell is not None:
            row, col, _ = hidden_cell
            local_points.append((row, col))
            self._coarse_threat_direction = self._direction_from_screen_target(
                allies,
                col * CELL_WIDTH + CELL_WIDTH // 2,
                row * CELL_HEIGHT + CELL_HEIGHT // 2,
            )
        else:
            # A continuous hollow or the overall ColorMatch box may still give
            # a useful eight-direction hint, but neither is an enemy cell.  In
            # particular, never persist the bounding-box centre as an A* goal.
            origin = self._threat_origin_from_mask(img, allies)
            if origin is not None:
                origin_x, origin_y, _ = origin
                self._coarse_threat_direction = self._direction_from_screen_target(
                    allies, origin_x, origin_y
                )
            else:
                recognition = context.run_recognition(
                    "Battle_ThreatRegion", img
                )
                results = (
                    recognition.filtered_results
                    if recognition
                    and recognition.hit
                    and recognition.filtered_results
                    else []
                )
                credible = []
                for result in results:
                    x, y, width, height = (
                        int(value) for value in result.box
                    )
                    if (
                        width >= self.THREAT_REGION_MIN_WIDTH
                        and height >= self.THREAT_REGION_MIN_HEIGHT
                        and width * height >= self.THREAT_REGION_MIN_BOX_AREA
                    ):
                        credible.append((width * height, x, y, width, height))
                if credible:
                    _, x, y, width, height = max(credible)
                    self._coarse_threat_direction = self._direction_from_screen_target(
                        allies,
                        x + width // 2,
                        y + height // 2,
                    )

        # Calling this with an empty list deliberately removes older threat
        # cells from the current viewport.  Coarse direction hints never enter
        # ``world.units`` and therefore cannot accumulate into phantom targets.
        world_points = self._session.world.merge_threat_cells(
            local_points,
            round_seen,
            confidence=confidence,
        )
        if local_points:
            logger.info(
                "确认锯齿空洞敌人格: "
                f"local={local_points}, world={world_points}"
            )
        elif self._coarse_threat_direction is not None:
            logger.debug(
                "威胁层仅产生方向提示，不写入敌人坐标: "
                f"direction={self._coarse_threat_direction}"
            )
        return local_points

    def _refresh_local_threat_map(
        self, context: Context, round_seen: int
    ) -> bool:
        """Refresh hidden-enemy markers in the current viewport only."""
        self._coarse_threat_direction = None
        if not self._set_threat_overlay(context, True):
            self._threat_overlay_safe = False
            return False
        try:
            img = self._screencap(context)
            if img is None:
                return False
            self._scan_world_view(
                context,
                img,
                round_seen,
                overlay_open=True,
            )
            return True
        finally:
            restored = self._set_threat_overlay(context, False)
            self._threat_overlay_safe = restored

    def _scan_world_view(
        self,
        context: Context,
        img: Any,
        round_seen: int,
        *,
        overlay_open: bool,
    ) -> BattleGrid:
        grid = BattleGrid()
        # 开启威胁层后，整片红色危险格会被普通红条 ColorMatch 切成大量
        # 水平碎片。它们不是敌人血条，不能写入持久地图；覆盖层画面只
        # 保留蓝/绿单位，敌人位置统一由锯齿中心/威胁区专用识别写入。
        scan_types = (
            (CellType.SELF, CellType.FRIEND) if overlay_open else None
        )
        self.scanner.scan_grid(
            grid,
            context,
            img,
            cell_types=scan_types,
        )
        self._merge_world_view(
            grid,
            round_seen,
            # The red overlay can temporarily hide a health bar.  Never erase
            # normal observations from an overlay frame.
            replace_visible=not overlay_open,
        )
        if overlay_open:
            self._merge_open_threat_overlay(context, img, grid, round_seen)
        return grid

    def _pan_world_once(
        self,
        context: Context,
        direction: Tuple[int, int],
        round_seen: int,
        *,
        overlay_open: bool,
        phase: str,
        fine: bool = False,
    ) -> Optional[Tuple[bool, Optional[BattleGrid], Any]]:
        """Pan once, register measured camera motion, then update the map.

        ``direction`` is expressed as ``(world_row, world_col)``.  The return
        value is ``(moved, grid, image)``; ``None`` means the swipe action itself
        failed and therefore must not be mistaken for a map boundary.
        """
        if self._session is None:
            return None
        before_img = self._screencap(context)
        if before_img is None:
            return None
        before_observed = len(self._session.world.observed)
        direction_xy = direction[1], direction[0]
        if not self._pan_camera(context, direction_xy, fine=fine):
            logger.warning(f"{phase}地图滑动动作失败: direction={direction}")
            return None
        after_img = self._screencap(context)
        if after_img is None:
            return None

        shift_x, shift_y, response = self._camera_motion(before_img, after_img)
        if not self._camera_shift_is_real(shift_x, shift_y, response):
            self._session.world.mark_boundary(direction)
            logger.info(
                f"{phase}到达地图边界: direction={direction}, "
                f"motion=({shift_x:.1f},{shift_y:.1f}), response={response:.3f}"
            )
            return False, None, after_img

        origin_delta = self._session.world.apply_camera_motion(shift_x, shift_y)
        grid = self._scan_world_view(
            context,
            after_img,
            round_seen,
            overlay_open=overlay_open,
        )
        # Camera exploration alone is not combat progress.  In particular,
        # threat cells can appear/disappear as overlapping viewports clip the
        # jagged hole; resetting the watchdog for that visual churn made the
        # 12-cycle no-progress fuse stay forever at 1/12.  Newly observed map
        # cells are useful progress, while confirmed attacks/moves reset the
        # counter at their action sites.
        if len(self._session.world.observed) > before_observed:
            self._session.record_progress()
        logger.debug(
            f"{phase}地图更新: direction={direction}, "
            f"motion=({shift_x:.1f},{shift_y:.1f}), "
            f"origin_delta={origin_delta}, "
            f"origin={self._session.world.camera_origin}, "
            f"explored={len(self._session.world.observed)}"
        )
        return True, grid, after_img

    def _explore_world_clockwise(
        self, context: Context, round_seen: int
    ) -> bool:
        """Explore right/down/left/up with threat mapping kept enabled."""
        if self._session is None:
            return False
        explorer = self._session.new_exploration_pass()
        world = self._session.world
        self._coarse_threat_direction = None
        logger.info(
            f"开始第 {self._session.exploration_passes} 次顺时针全局建图："
            "右 -> 下 -> 左 -> 上"
        )
        if not self._set_threat_overlay(context, True):
            self._threat_overlay_safe = False
            return False

        action_failures = 0
        try:
            initial_img = self._screencap(context)
            if initial_img is None:
                return False
            self._scan_world_view(
                context,
                initial_img,
                round_seen,
                overlay_open=True,
            )
            while not explorer.completed and not context.tasker.stopping:
                if world.has_allies() and (
                    self._world_has_fresh_targets()
                    or self._coarse_threat_direction is not None
                ):
                    logger.info("全局地图已同时具备我方和敌人，提前结束探索")
                    break
                direction = explorer.direction
                pan_result = self._pan_world_once(
                    context,
                    direction,
                    round_seen,
                    overlay_open=True,
                    phase="顺时针探索",
                )
                if pan_result is None:
                    action_failures += 1
                    if action_failures >= 2:
                        logger.error("顺时针探索连续两次滑动动作失败")
                        return False
                    continue
                action_failures = 0
                moved, _, _ = pan_result
                explorer.record_pan(moved)
        finally:
            restored = self._set_threat_overlay(context, False)
            self._threat_overlay_safe = restored

        if not self._threat_overlay_safe:
            logger.error("顺时针探索结束后无法关闭威胁层")
            return False
        logger.info(
            "顺时针建图结束: "
            f"complete={explorer.completed}, swipes={explorer.successful_swipes}, "
            f"allies={len(world.unit_points(WORLD_SELF))}, "
            f"enemies={len(world.unit_points(WORLD_ENEMY))}, "
            f"environment={len(world.unit_points(WORLD_ENVIRONMENT))}"
        )
        return world.has_allies() and (
            self._world_has_fresh_targets()
            or self._coarse_threat_direction is not None
        )

    def _focus_known_ally(
        self, context: Context, round_seen: int
    ) -> Optional[Tuple[BattleGrid, Any]]:
        """Move the camera to the newest known ally and leave it actionable."""
        if self._session is None:
            return None
        if not self._set_threat_overlay(context, False):
            return None
        world = self._session.world
        failed_pans = 0

        for _ in range(14):
            img = self._screencap(context)
            if img is None:
                return None
            grid = self._scan_world_view(
                context,
                img,
                round_seen,
                overlay_open=False,
            )
            actionable = [
                cell for cell in grid.self_units if self._ally_is_actionable(cell)
            ]
            centered = [
                cell
                for cell in actionable
                if 1 <= cell.row <= ROWS - 2 and 1 <= cell.col <= COLS - 2
            ]
            if centered:
                return grid, img
            if actionable:
                target_world = world.local_to_world(
                    actionable[0].row, actionable[0].col
                )
            else:
                ally_points = world.unit_points(WORLD_SELF)
                if not ally_points:
                    return None
                target_world = ally_points[0]

            local = world.world_to_local(target_world)
            if local is None:
                origin_row, origin_col = world.camera_origin
                centre = origin_row + ROWS // 2, origin_col + COLS // 2
                row_direction = (
                    0
                    if target_world[0] == centre[0]
                    else (1 if target_world[0] > centre[0] else -1)
                )
                col_direction = (
                    0
                    if target_world[1] == centre[1]
                    else (1 if target_world[1] > centre[1] else -1)
                )
            else:
                row_direction = -1 if local[0] <= 0 else (1 if local[0] >= ROWS - 1 else 0)
                col_direction = -1 if local[1] <= 0 else (1 if local[1] >= COLS - 1 else 0)
            direction = row_direction, col_direction
            if direction == (0, 0):
                # The remembered ally was inside this view but a fresh normal
                # scan invalidated it.  Retry with another observation.
                continue

            pan_result = self._pan_world_once(
                context,
                direction,
                round_seen,
                overlay_open=False,
                phase="定位我方",
                fine=True,
            )
            if pan_result is None:
                failed_pans += 1
                if failed_pans >= 2:
                    return None
                continue
            moved, _, _ = pan_result
            if not moved:
                # Reaching a camera boundary does not invalidate a unit that
                # is already inside the conservative click-safe rectangle.
                # Returning that edge view lets the local planner select the
                # ally and move toward the retained threat target instead of
                # re-entering global exploration forever.
                if actionable:
                    logger.info(
                        "镜头已到边界，沿用当前可操作我方视野进入点对点行动"
                    )
                    return grid, img
                return None
        return None

    def _world_pursuit(
        self, grid: BattleGrid, ally: Optional[Cell] = None
    ) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]], bool]:
        """Return screen-order direction, local target and environment mode."""
        if self._session is None:
            return None, None, False
        world = self._session.world
        if ally is None:
            actionable = [
                cell for cell in grid.self_units if self._ally_is_actionable(cell)
            ]
            if not actionable:
                return None, None, False
            ally = actionable[0]
        ally_world = world.local_to_world(ally.row, ally.col)
        enemy_points, environment_points = self._fresh_world_targets()
        environment_mode = not enemy_points and bool(environment_points)
        targets = enemy_points or environment_points
        if not targets:
            return self._coarse_threat_direction, None, environment_mode
        target = min(
            targets,
            key=lambda point: (
                max(abs(point[0] - ally_world[0]), abs(point[1] - ally_world[1])),
                point,
            ),
        )
        row_delta = target[0] - ally_world[0]
        col_delta = target[1] - ally_world[1]
        direction = (
            0 if col_delta == 0 else (1 if col_delta > 0 else -1),
            0 if row_delta == 0 else (1 if row_delta > 0 else -1),
        )
        return direction, world.world_to_local(target), environment_mode

    def _search_for_enemies(
        self, context: Context
    ) -> Optional[Tuple[Tuple[int, int], BattleGrid, bool]]:
        """顺时针螺旋搜索人物/环境目标；若敌我同屏则停留。"""
        executed_steps: List[Tuple[int, int]] = []
        offset_x = 0
        offset_y = 0
        views_scanned = 1
        found_direction: Optional[Tuple[int, int]] = None
        found_enemy_count = 0
        found_environment_count = 0
        found_environment_only = False
        found_view = 0
        found_grid: Optional[BattleGrid] = None
        blocked_direction: Optional[Tuple[int, int]] = None

        logger.info(
            "当前视野没有敌人，开始顺时针螺旋搜索，"
            f"最多滑动 {self.MAX_SEARCH_SWIPES} 次"
        )

        # 搜索阶段既检查真实红色血条，也检查危险覆盖区。草丛会遮住
        # 血条，而覆盖区的锯齿中心仍标识敌人；找到覆盖区时，当前镜头
        # 相对起点的偏移就是返回我方后应推进的粗粒度方向。
        self._set_threat_overlay(context, False)

        for direction in self._spiral_directions(self.MAX_SEARCH_SWIPES):
            if context.tasker.stopping:
                break
            if direction == blocked_direction:
                continue
            blocked_direction = None
            scan_result = self._pan_and_scan(
                context, direction, "敌人搜索"
            )
            if scan_result is None:
                # 当前方向已经撞边，跳过本螺旋段剩余的同方向手势；
                # 换向后再允许该方向出现在后续螺旋段中。
                blocked_direction = direction
                continue
            search_grid, _ = scan_result
            executed_steps.append(direction)
            offset_x += direction[0]
            offset_y += direction[1]
            views_scanned += 1
            threat_regions = []
            if not search_grid.enemy_units:
                threat_regions = self._visible_threat_regions(context)
            logger.debug(
                f"地图搜索视野 {views_scanned}: offset=({offset_x},{offset_y}), "
                f"真实红色状态条={len(search_grid.enemy_units)}, "
                f"威胁区域={len(threat_regions)}, "
                f"环境目标={len(search_grid.environment_units)}, "
                f"我方={len(search_grid.self_units)}"
            )
            if (
                search_grid.enemy_units
                or search_grid.environment_units
                or threat_regions
            ):
                found_direction = (offset_x, offset_y)
                found_enemy_count = len(search_grid.enemy_units) or len(threat_regions)
                found_environment_count = len(search_grid.environment_units)
                found_environment_only = (
                    not search_grid.enemy_units
                    and not threat_regions
                    and bool(search_grid.environment_units)
                )
                found_view = views_scanned
                found_grid = search_grid
                if found_environment_only:
                    logger.info(
                        "搜索视野已确认祭坛/石碑类环境目标："
                        f"数量={found_environment_count}"
                    )
                elif threat_regions:
                    logger.info(
                        "搜索视野已确认隐藏敌人的威胁区域（锯齿中心方向有效）："
                        f"区域数={len(threat_regions)}"
                    )
                else:
                    logger.info(
                        "搜索视野已确认带生命数字的敌人状态条："
                        f"数量={found_enemy_count}"
                    )
                break

        if (
            found_direction is not None
            and found_grid is not None
            and found_grid.self_units
        ):
            self._set_threat_overlay(context, False)
            logger.info(
                f"搜索视野同时保留 {len(found_grid.self_units)} 名我方，"
                "停留当前视野直接行动，不再逆向返回"
            )
            return found_direction, found_grid, found_environment_only

        # B 路径：敌人已确认但当前相机我方不可见。
        # 大地图下「敌我同屏」几乎不可能发生，不能让 reverse-pan 这一个
        # 不可靠的反向滑动决定整场成败。改用更小的顺时针螺旋专门找回
        # 我方位置；找到后保留敌人方向（让 run() 跨回合推进），即便 grid
        # 里不再有敌人也能继续推进。
        if found_direction is not None:
            logger.warning(
                "搜索到远端敌人/环境目标但当前视野我方不可见，"
                "改用顺时针小螺旋找回我方位置（不回滚到原点）"
            )
            ally_anchor_grid = self._find_allies(context, max_swipes=6)
            if ally_anchor_grid is not None:
                target_label = (
                    f"{found_environment_count} 个环境目标"
                    if found_environment_only
                    else f"{found_enemy_count} 名敌人"
                )
                logger.info(
                    f"远端发现 {target_label}，相对方向={found_direction}，"
                    f"扫描视野={found_view}；已回到我方视野，按方向推进"
                )
                self._set_threat_overlay(context, False)
                return (
                    found_direction,
                    ally_anchor_grid,
                    found_environment_only,
                )
            logger.warning("小螺旋找回我方失败，回退到 restore_pans 路径")

        # C 路径：未找到敌人 或 B 路径的 _find_allies 也失败。
        # 按实际成功移动的反向序列回退原点，边缘处未产生画面变化的手势
        # 不在 executed_steps 中。
        self._restore_pans(context, executed_steps)

        self._set_threat_overlay(context, False)
        self._wait_for_scene_settle(context, timeout=3.0)
        restored_grid = BattleGrid()
        restored_img = self._screencap(context)
        if restored_img is not None:
            self.scanner.scan_grid(restored_grid, context, restored_img)
        else:
            self.scanner.scan_grid(restored_grid, context)
        restore_succeeded = bool(restored_grid.self_units) or (
            restored_img is not None
            and self._has_safe_blue_evidence(context, restored_img)
        )
        for retry in range(1, 3):
            if restore_succeeded:
                break
            if context.tasker.stopping:
                return None
            logger.warning(
                f"逆向返回第 {retry} 次补充扫描仍未确认我方，"
                "等待 HUD 收敛后重试"
            )
            time.sleep(0.4)
            self._wait_for_scene_settle(context, timeout=2.0)
            retry_img = self._screencap(context)
            if retry_img is None:
                continue
            restored_grid = BattleGrid()
            self.scanner.scan_grid(restored_grid, context, retry_img)
            restore_succeeded = bool(restored_grid.self_units) or (
                self._has_safe_blue_evidence(context, retry_img)
            )

        if not restore_succeeded:
            logger.error("逆向返回后没有重新找到我方单位，地图搜索结果作废")
            return None

        if found_direction is not None:
            target_label = (
                f"{found_environment_count} 个环境目标"
                if found_environment_only
                else f"{found_enemy_count} 名敌人"
            )
            logger.info(
                f"远端发现 {target_label}，相对方向={found_direction}，"
                f"扫描视野={found_view}"
            )
            return found_direction, restored_grid, found_environment_only
        return None

    def _visible_threat_regions(
        self, context: Context
    ) -> List[Tuple[int, int, int, int]]:
        """Return credible danger-overlay regions for the current camera view.

        This is deliberately independent of ally detection: while the search
        camera is panned away from the party, the threat region alone is enough
        to prove that the viewport contains a hidden enemy.  The caller keeps
        the camera offset and later returns to the party before moving.
        """
        if not self._set_threat_overlay(context, True):
            self._set_threat_overlay(context, False)
            return []

        try:
            img = self._screencap(context)
            if img is None:
                return []
            recognition = context.run_recognition("Battle_ThreatRegion", img)
            results = (
                recognition.filtered_results
                if recognition
                and recognition.hit
                and recognition.filtered_results
                else []
            )
            credible: List[Tuple[int, int, int, int]] = []
            for result in results:
                x, y, width, height = result.box
                if (
                    width >= self.THREAT_REGION_MIN_WIDTH
                    and height >= self.THREAT_REGION_MIN_HEIGHT
                    and width * height >= self.THREAT_REGION_MIN_BOX_AREA
                ):
                    credible.append((x, y, width, height))
            return credible
        finally:
            # Red cover masks health bars and can be mistaken for enemy pixels
            # by the normal scanner, so always restore the ordinary view.
            self._set_threat_overlay(context, False)

    def _hidden_enemy_direction(
        self, context: Context, grid: BattleGrid
    ) -> Optional[Tuple[int, int]]:
        """用危险覆盖区推断当前视野中隐匿敌人的粗粒度方向。

        草丛会隐藏敌人的红色生命条，不能据此构造一个可点击敌人格；这里只
        临时打开危险覆盖层，取最大的可信红色连通框相对我方的方向。随后无论
        识别成功与否都关闭覆盖层，避免半透明红地块污染正常红条扫描。
        """
        self._hidden_enemy_cell = None
        self._threat_overlay_safe = True
        allies = [
            cell for cell in grid.self_units if self._ally_is_actionable(cell)
        ]
        if not allies:
            return None

        if not self._set_threat_overlay(context, True):
            self._set_threat_overlay(context, False)
            return None

        direction: Optional[Tuple[int, int]] = None
        accepted_box: Optional[Tuple[int, int, int, int]] = None
        origin_score: Optional[float] = None
        restored = False
        try:
            img = self._screencap(context)
            if img is not None:
                hidden_cell = self._threat_enemy_cell_from_mask(img, allies)
                origin = self._threat_origin_from_mask(img, allies)
                recognition = context.run_recognition("Battle_ThreatRegion", img)
                results = (
                    recognition.filtered_results
                    if recognition
                    and recognition.hit
                    and recognition.filtered_results
                    else []
                )
                candidates = []
                for result in results:
                    x, y, width, height = (
                        int(value) for value in result.box
                    )
                    box_area = width * height
                    if (
                        width >= self.THREAT_REGION_MIN_WIDTH
                        and height >= self.THREAT_REGION_MIN_HEIGHT
                        and box_area >= self.THREAT_REGION_MIN_BOX_AREA
                    ):
                        candidates.append((box_area, x, y, width, height))

                if hidden_cell is not None:
                    row, col, origin_score = hidden_cell
                    self._hidden_enemy_cell = (row, col)
                    target_x = col * 120 + 60
                    target_y = row * 120 + 60
                    accepted_box = (target_x, target_y, 0, 0)
                elif origin is not None:
                    target_x, target_y, origin_score = origin
                    accepted_box = (target_x, target_y, 0, 0)
                elif candidates:
                    _, x, y, width, height = max(candidates)
                    accepted_box = (x, y, width, height)
                    target_x = x + width // 2
                    target_y = y + height // 2

                if accepted_box is not None:
                    ally_x = sum(cell.safe_click_point()[0] for cell in allies) // len(
                        allies
                    )
                    ally_y = sum(cell.safe_click_point()[1] for cell in allies) // len(
                        allies
                    )
                    delta_x = target_x - ally_x
                    delta_y = target_y - ally_y
                    direction_x = (
                        0
                        if abs(delta_x) < self.THREAT_DIRECTION_DEAD_ZONE
                        else (1 if delta_x > 0 else -1)
                    )
                    direction_y = (
                        0
                        if abs(delta_y) < self.THREAT_DIRECTION_DEAD_ZONE
                        else (1 if delta_y > 0 else -1)
                    )
                    if direction_x or direction_y:
                        direction = (direction_x, direction_y)
        finally:
            restored = self._set_threat_overlay(context, False)

        if not restored:
            self._threat_overlay_safe = False
            logger.warning("隐匿敌人探测后未能关闭危险覆盖层，放弃本次方向")
            return None
        if direction is None:
            logger.info("危险覆盖层未发现足够大的可信连通区")
            return None

        logger.info(
            f"危险覆盖区 {accepted_box} 相对我方给出隐匿敌人方向 {direction}"
            + (
                f"，锯齿中心格={self._hidden_enemy_cell}"
                if self._hidden_enemy_cell is not None
                else ""
            )
        )
        return direction

    def _threat_enemy_cell_from_mask(
        self, img: Any, allies: List[Cell]
    ) -> Optional[Tuple[int, int, float]]:
        """Locate the enemy's grid cell from the jagged hole in danger tiles.

        A hidden enemy does not remove the red mask completely: its sprite
        covers the centre of its own 120px cell while the ground still shows
        around the edges.  Normal danger cells retain a red centre, and empty
        terrain has no red coverage at all.  Requiring two adjacent danger
        cells keeps grass and tree holes from becoming targets.
        """
        if img is None or len(img.shape) < 2:
            return None

        height = min(int(img.shape[0]), ROWS * 120)
        width = min(int(img.shape[1]), COLS * 120)
        if height < ROWS * 120 or width < COLS * 120:
            return None
        hsv = cv2.cvtColor(img[:height, :width], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (2, 130, 75), (20, 200, 165)) > 0
        coverage = {
            (row, col): float(
                mask[row * 120 : (row + 1) * 120,
                     col * 120 : (col + 1) * 120].mean()
            )
            for row in range(ROWS)
            for col in range(COLS)
        }
        ally_cells = {(cell.row, cell.col) for cell in allies}
        best: Optional[Tuple[float, int, int]] = None
        for row in range(ROWS):
            for col in range(COLS):
                if (row, col) in ally_cells:
                    continue
                cell_coverage = coverage[(row, col)]
                if not (
                    self.THREAT_CELL_MIN_COVERAGE
                    <= cell_coverage
                    <= self.THREAT_CELL_MAX_COVERAGE
                ):
                    continue
                centre = mask[
                    row * 120 + 40 : row * 120 + 80,
                    col * 120 + 40 : col * 120 + 80,
                ]
                centre_coverage = float(centre.mean())
                if centre_coverage > self.THREAT_CELL_CENTER_MAX_COVERAGE:
                    continue
                neighbours = [
                    coverage.get((row + row_delta, col + col_delta), 0.0)
                    for row_delta, col_delta in ((-1, 0), (1, 0), (0, -1), (0, 1))
                ]
                red_neighbours = sum(
                    value >= self.THREAT_CELL_NEIGHBOR_MIN_COVERAGE
                    for value in neighbours
                )
                if red_neighbours < self.THREAT_CELL_MIN_RED_NEIGHBORS:
                    continue
                # Prefer a conspicuously occluded centre, then the candidate
                # closest to the surrounding red threat pattern.
                score = (
                    2.5 * (self.THREAT_CELL_MAX_COVERAGE - cell_coverage)
                    + sum(neighbours)
                    + (self.THREAT_CELL_CENTER_MAX_COVERAGE - centre_coverage)
                )
                if best is None or score > best[0]:
                    best = (score, row, col)
        if best is None:
            return None
        score, row, col = best
        return row, col, score

    def _threat_origin_from_mask(
        self, img: Any, allies: List[Cell]
    ) -> Optional[Tuple[int, int, float]]:
        """Find a hidden enemy from the jagged centre of its threat overlay.

        The overlay is drawn on threatened cells, while an enemy sprite hides the
        overlay on its own cell.  Thus the enemy becomes a non-red hole surrounded
        by red cells.  Friendly-unit holes are rejected from their known positions.
        The result is used only as a pursuit direction, never as a click target.
        """
        if img is None or len(img.shape) < 2:
            return None

        height = min(int(img.shape[0]), 1160)
        width = min(int(img.shape[1]), 720)
        ring = self.THREAT_ORIGIN_RING_RADIUS
        core = self.THREAT_ORIGIN_CORE_RADIUS
        if height <= ring * 2 or width <= ring * 2:
            return None

        hsv = cv2.cvtColor(img[:height, :width], cv2.COLOR_BGR2HSV)
        red_mask = cv2.inRange(hsv, (2, 130, 75), (20, 200, 165))
        mask = (red_mask > 0).astype("uint8")
        if int(mask.sum()) < self.THREAT_REGION_MIN_BOX_AREA // 8:
            return None

        ally_points = [cell.safe_click_point() for cell in allies]
        best: Optional[Tuple[float, int, int]] = None
        for y in range(ring, height - ring, self.THREAT_ORIGIN_SAMPLE_STEP):
            for x in range(ring, width - ring, self.THREAT_ORIGIN_SAMPLE_STEP):
                if mask[y, x]:
                    continue
                if any(
                    (x - ally_x) ** 2 + (y - ally_y) ** 2
                    < self.THREAT_ORIGIN_MIN_DISTANCE_FROM_ALLY ** 2
                    for ally_x, ally_y in ally_points
                ):
                    continue

                core_coverage = float(
                    mask[y - core : y + core, x - core : x + core].mean()
                )
                if core_coverage > 0.30:
                    continue
                coverage = (
                    float(mask[y - ring : y - core, x - core : x + core].mean()),
                    float(mask[y + core : y + ring, x - core : x + core].mean()),
                    float(mask[y - core : y + core, x - ring : x - core].mean()),
                    float(mask[y - core : y + core, x + core : x + ring].mean()),
                )
                weakest = min(coverage)
                score = weakest + sum(coverage) / 8 - core_coverage * 0.25
                if weakest < 0.20 or score < self.THREAT_ORIGIN_MIN_SCORE:
                    continue
                if best is None or score > best[0]:
                    best = (score, x, y)

        if best is None:
            return None
        score, x, y = best
        return x, y, score

    def _search_for_allies_and_stay(
        self, context: Context
    ) -> Optional[BattleGrid]:
        """镜头丢失我方时只做一轮螺旋搜索；找到后停留。"""
        return self._find_allies(context, max_swipes=self.MAX_SEARCH_SWIPES)

    def _find_allies(
        self, context: Context, max_swipes: int
    ) -> Optional[BattleGrid]:
        """从当前相机出发顺时针螺旋找回我方；找到后停留。

        作为 `_search_for_allies_and_stay`（大螺旋）与
        `_search_for_enemies` 的小步螺旋找回我方子流程的公共实现。
        大地图下 `_search_for_enemies` 找到远端敌人但当前视野无我方时，
        调用本函数以更可靠的方式把相机带回我方视野。
        """
        self._set_threat_overlay(context, False)
        executed_steps: List[Tuple[int, int]] = []
        offset_x = 0
        offset_y = 0
        blue_evidence_seen = False
        attempted_steps = 0
        blocked_direction: Optional[Tuple[int, int]] = None

        logger.info(
            "开始顺时针螺旋找回我方位置，"
            f"最多滑动 {max_swipes} 次"
        )

        for view_index, direction in enumerate(
            self._spiral_directions(max_swipes), start=2
        ):
            if context.tasker.stopping:
                break
            if direction == blocked_direction:
                continue
            blocked_direction = None
            attempted_steps += 1
            scan_result = self._pan_and_scan(
                context, direction, "我方搜索"
            )
            if scan_result is None:
                blocked_direction = direction
                continue
            search_grid, current_img = scan_result
            executed_steps.append(direction)
            offset_x += direction[0]
            offset_y += direction[1]
            logger.debug(
                f"我方搜索视野 {view_index}: offset=({offset_x},{offset_y}), "
                f"我方={len(search_grid.self_units)}, "
                f"敌人={len(search_grid.enemy_units)}"
            )
            if search_grid.self_units:
                logger.info(
                    f"重新找到 {len(search_grid.self_units)} 名我方单位，"
                    f"停留在 offset=({offset_x},{offset_y})"
                )
                return search_grid
            if self._has_safe_blue_evidence(context, current_img):
                if not blue_evidence_seen:
                    logger.info(
                        f"我方搜索视野 {view_index} 发现蓝色碎片，"
                        "但没有完整我方状态条；继续既定螺旋，"
                        "不追加四方向邻域探查"
                    )
                blue_evidence_seen = True

        logger.info(
            f"顺时针探索完成（max={max_swipes}, 实际手势={attempted_steps}, "
            f"有效视野移动={len(executed_steps)}）；"
            "未找到完整我方状态条，restore 回原视野"
        )
        self._restore_pans(context, executed_steps)
        self._wait_for_scene_settle(context, timeout=3.0)
        return None

    def _pan_and_scan(
        self,
        context: Context,
        direction: Tuple[int, int],
        phase: str,
    ) -> Optional[Tuple[BattleGrid, Any]]:
        """滑动一个视野；只有背景真实移动时才返回新建战场快照。"""
        before_img = self._screencap(context)
        if before_img is None or not self._pan_camera(context, direction):
            logger.warning(f"{phase}地图滑动 {direction} 失败")
            return None
        after_img = self._screencap(context)
        if after_img is None:
            logger.warning(f"{phase}滑动后截图失败，撤销该方向")
            self._pan_camera(context, (-direction[0], -direction[1]))
            return None

        shift_x, shift_y, response = self._camera_motion(
            before_img, after_img
        )
        if not self._camera_shift_is_real(shift_x, shift_y, response):
            logger.info(
                f"{phase}向 {direction} 滑动后背景位移="
                f"({shift_x:.1f},{shift_y:.1f}), response={response:.3f}，"
                "判定已到边界"
            )
            return None

        grid = BattleGrid()
        self.scanner.scan_grid(grid, context, after_img)
        return grid, after_img

    def _restore_pans(
        self, context: Context, executed_steps: List[Tuple[int, int]]
    ) -> None:
        """逆序撤销已确认产生背景位移的镜头滑动。"""
        for direction in reversed(executed_steps):
            if context.tasker.stopping:
                return
            self._pan_camera(context, (-direction[0], -direction[1]))

    def _recenter_edge_allies(
        self,
        context: Context,
        grid: BattleGrid,
        round_seen: int,
        *,
        known_target: Optional[Tuple[int, int]] = None,
    ) -> Tuple[BattleGrid, bool, Any]:
        """Pan edge units inward and keep the persistent map synchronized."""
        if not grid.self_units:
            return grid, False, None

        current_grid = grid
        moved = False
        latest_img = None
        known_world_target = (
            self._session.world.local_to_world(*known_target)
            if known_target is not None and self._session is not None
            else None
        )

        # A corner unit needs one diagonal nudge, not two alternating cardinal
        # nudges.  The candidate frame must still contain the party and, when a
        # real enemy was visible, that enemy as well.  Failed nudges are rolled
        # back through the same map-aware path so camera_origin stays correct.
        for _ in range(4):
            edge_enemies = [
                cell
                for cell in current_grid.enemy_units
                if self._edge_recenter_direction(
                    cell,
                    x_bounds=self.ACTION_SAFE_X,
                    y_bounds=(160, 1000),
                )
                is not None
            ]
            unsafe_allies = [
                cell
                for cell in current_grid.self_units
                if not self._ally_is_actionable(cell)
            ]
            known_edge_cells: List[Cell] = []
            if known_world_target is not None and self._session is not None:
                current_local_target = self._session.world.world_to_local(
                    known_world_target
                )
                if current_local_target is not None:
                    synthetic_target = Cell(*current_local_target)
                    if self._edge_recenter_direction(
                        synthetic_target,
                        x_bounds=self.ACTION_SAFE_X,
                        y_bounds=(160, 1000),
                    ) is not None:
                        known_edge_cells.append(synthetic_target)
            if not edge_enemies and not unsafe_allies and not known_edge_cells:
                break
            focus_cells = edge_enemies + known_edge_cells + unsafe_allies
            direction = self._edge_recenter_direction(
                focus_cells,
                x_bounds=self.ACTION_SAFE_X,
                y_bounds=(160, 1000)
                if edge_enemies or known_edge_cells
                else self.ACTION_SAFE_Y,
            )
            if direction is None:
                break
            logger.info(
                "边缘单位重定位: "
                f"allies={len(unsafe_allies)}, enemies={len(edge_enemies)}, "
                f"hidden_targets={len(known_edge_cells)}, "
                f"direction={direction}"
            )
            pan_result = self._pan_world_once(
                context,
                (direction[1], direction[0]),
                round_seen,
                overlay_open=False,
                phase="边缘重定位",
                fine=True,
            )
            if pan_result is None:
                logger.warning(f"边缘重定位滑动 {direction} 失败")
                break
            did_pan, candidate_grid, candidate_img = pan_result
            if not did_pan or candidate_grid is None:
                break
            keeps_enemy = not edge_enemies or bool(candidate_grid.enemy_units)
            if not candidate_grid.self_units or not keeps_enemy:
                logger.warning("边缘重定位后无法同时保留敌我，撤销本次滑动")
                self._pan_world_once(
                    context,
                    (-direction[1], -direction[0]),
                    round_seen,
                    overlay_open=False,
                    phase="边缘重定位回滚",
                    fine=True,
                )
                break
            current_grid = candidate_grid
            latest_img = candidate_img
            moved = True

        return current_grid, moved, latest_img

    @classmethod
    def _ally_is_actionable(cls, cell: Cell) -> bool:
        x, y = cell.safe_click_point()
        return (
            cls.ACTION_SAFE_X[0] <= x <= cls.ACTION_SAFE_X[1]
            and cls.ACTION_SAFE_Y[0] <= y <= cls.ACTION_SAFE_Y[1]
        )

    @classmethod
    def _ally_at_grid_edge(
        cls,
        allies: List[Tuple[int, int]],
        direction: Tuple[int, int],
    ) -> bool:
        """判断任意我方单位是否已处于当前视野网格在指定方向的边界。

        col=0 是当前视野的最左列，row=0 是最上行。bot 已在边界时，
        沿 ``direction`` 推进只能在当前视野内抖动（投影 > 0 但只是
        血条识别噪声），必须 pan camera 才能看到更远的目标。
        ``direction`` 与 ``Battle_MapPan`` 一致：(col方向, row方向)，
        -1 表示向左 / 向上。
        """
        direction_x, direction_y = direction
        for row, col in allies:
            if direction_x < 0 and col == 0:
                return True
            if direction_x > 0 and col == COLS - 1:
                return True
            if direction_y < 0 and row == 0:
                return True
            if direction_y > 0 and row == ROWS - 1:
                return True
        return False

    @classmethod
    def _has_safe_blue_evidence(cls, context: Context, img: Any) -> bool:
        """判断安全战场区域是否仍有被遮挡/截断的我方蓝色碎片。"""
        recognition = context.run_recognition("Battle_UnitScan_Blue", img)
        if not recognition.hit or not recognition.filtered_results:
            return False
        for result in recognition.filtered_results:
            x, y, width, height = (int(value) for value in result.box)
            if width < 2 or height < 3:
                continue
            center_x = x + width // 2
            center_y = y + height // 2
            if (
                cls.ACTION_SAFE_X[0]
                <= center_x
                <= cls.ACTION_SAFE_X[1]
                and cls.ACTION_SAFE_Y[0]
                <= center_y
                <= cls.ACTION_SAFE_Y[1]
            ):
                return True
        return False

    def _pan_camera(
        self,
        context: Context,
        direction: Tuple[int, int],
        fine: bool = False,
    ) -> bool:
        prefix = "Battle_MapNudge" if fine else "Battle_MapPan"
        node_by_direction = {
            (1, 0): f"{prefix}Right",
            (-1, 0): f"{prefix}Left",
            (0, 1): f"{prefix}Down",
            (0, -1): f"{prefix}Up",
            (1, 1): f"{prefix}DownRight",
            (-1, 1): f"{prefix}DownLeft",
            (-1, -1): f"{prefix}UpLeft",
            (1, -1): f"{prefix}UpRight",
        }
        node = node_by_direction.get(direction)
        if node is None:
            return False
        result = context.run_task(node)
        return self._task_result_has_hit(result, {node})

    @classmethod
    def _spiral_directions(cls, max_steps: int) -> List[Tuple[int, int]]:
        """生成右起步的顺时针搜索步；任一直线段最多移动三格。"""
        directions = [(1, 0), (0, 1), (-1, 0), (0, -1)]
        result: List[Tuple[int, int]] = []
        segment_length = 1
        direction_index = 0
        while len(result) < max_steps:
            for _ in range(2):
                direction = directions[direction_index % 4]
                direction_index += 1
                for _ in range(
                    min(segment_length, cls.MAX_STRAIGHT_SEARCH_SWIPES)
                ):
                    result.append(direction)
                    if len(result) >= max_steps:
                        return result
            segment_length += 1
        return result

    def _click_cell(self, context: Context, cell: Cell, label: str) -> bool:
        """局部网格点对点操作：移动格与敌人格都点击两次提交。"""
        action = "attack" if "攻击" in label else "move"
        x, y = cell.action_click_point(action)
        logger.debug(f"{label}点击: ({cell.row}, {cell.col}) -> ({x}, {y})")
        first = self._click_and_log(context, x, y, f"{label}目标点击")
        if not first:
            return False
        # 第一次点击生成路径/攻击预览，第二次点击同一目标正式提交。
        time.sleep(0.2)
        second = self._click_and_log(context, x, y, f"{label}确认点击")
        # 4 倍速下 0.5 秒足以让镜头、人物和范围层落稳；后续仍通过
        # 画面/血条/回合变化做确认，而不是把等待本身当作成功。
        time.sleep(0.5)
        return second

    @staticmethod
    def _click_and_log(context: Context, x: int, y: int, label: str) -> bool:
        job = context.tasker.controller.post_click(x, y).wait()
        succeeded = bool(job.succeeded)
        logger.debug(f"{label}: ({x}, {y}), controller_succeeded={succeeded}")
        return succeeded

    @staticmethod
    def _screencap(context: Context) -> Optional[Any]:
        job = context.tasker.controller.post_screencap().wait()
        if not job.succeeded:
            return None
        return job.get()

    @staticmethod
    def _detect_battle_result(
        context: Context, img: Any
    ) -> Optional[bool]:
        """识别持久结算页；返回 True/False，仍在战斗则返回 None。"""
        if context.run_recognition("FightFail", img).hit:
            logger.error("识别到战斗失败结算页")
            return False
        if context.run_recognition("FightVictory", img).hit:
            logger.info("识别到战斗胜利结算页，战斗正常结束")
            return True
        return None

    def _record_round_advance(self, source: str) -> None:
        if self._session is None:
            return
        self._session.record_round_advance()
        logger.info(
            f"整场战斗回合进度 +1: {self._session.confirmed_rounds} "
            f"(source={source})"
        )

    def _end_round(
        self, context: Context, round_reference: Any, phase: str
    ) -> Optional[bool]:
        """
        点击结束回合并验证进入下一回合。

        返回 None 表示已验证进入下一回合；True/False 表示
        已进入胜利/失败终态或操作无法确认。
        """
        for attempt in range(1, 3):
            current_img = self._screencap(context)
            if current_img is None:
                logger.error(f"{phase}结束回合前截图失败")
                return False

            battle_result = self._detect_battle_result(context, current_img)
            if battle_result is not None:
                return battle_result
            if not context.run_recognition("FightEndRound", current_img).hit:
                logger.error(f"{phase}未识别到结束回合按钮")
                return False

            logger.debug(f"{phase}点击结束回合 ({attempt}/2)")
            result = context.run_task("FightEndRound")
            if not self._task_result_has_hit(result, {"FightEndRound"}):
                logger.warning(f"{phase}第 {attempt} 次结束回合节点未命中")
                continue

            timeout = 4.0 if attempt == 1 else 12.0
            if self._wait_for_round_change(
                context, round_reference, timeout=timeout
            ):
                logger.debug(f"{phase}已确认回合变化")
                self._record_round_advance(f"{phase}结束回合")
                return None

            latest_img = self._screencap(context)
            latest_result = (
                self._detect_battle_result(context, latest_img)
                if latest_img is not None
                else None
            )
            if latest_result is not None:
                return latest_result
            logger.warning(f"{phase}第 {attempt} 次点击后回合未变化")

        logger.error(f"{phase}两次结束回合均未生效")
        return False

    def _verify_action_result(
        self,
        context: Context,
        before_img: Any,
        round_reference: Any,
        label: str,
        ally: Cell,
        target: Cell,
    ) -> Tuple[bool, bool]:
        """确认具体动作结果，不能把“范围消失”误当成攻击/移动成功。"""
        is_move = "移动" in label
        last_frame_score = 0.0
        target_bar_point = (
            target.target_center
            if target.target_center != (0, 0)
            else target.safe_click_point()
        )
        before_target_red = self._red_bar_pixels(
            before_img, target_bar_point
        )
        round_candidate = None
        round_confirm_frames = 0
        for after_img in self._poll_frames(context, timeout=3.5, interval=0.25):
            last_frame_score = self._frame_difference(before_img, after_img)
            battle_result = self._detect_battle_result(context, after_img)
            if battle_result is not None:
                return battle_result, True
            changed, round_candidate, round_confirm_frames, round_score = (
                self._track_round_change(
                    round_reference,
                    round_candidate,
                    round_confirm_frames,
                    after_img,
                )
            )
            if changed:
                logger.info(
                    f"{label}后回合数字连续稳定变化，"
                    f"像素差异={round_score:.2f}，动作已生效"
                )
                self._record_round_advance(f"{label}后自动换回合")
                return True, True

            range_grid = BattleGrid()
            self.scanner.scan_ranges(range_grid, context, after_img)
            remaining_ranges = sum(
                1
                for row in range_grid.cells
                for cell in row
                if cell.is_moveable or cell.is_attackable
            )
            if is_move:
                # 蓝色范围会与普通状态条连成大片，但选中角色上方一排独立
                # 菱形行动点仍保持清晰。先用该局部特征确认移动，既不需要
                # Esc（会撤销移动），也不需要二次点击目标格。
                selected_self_cells = self._selected_self_cells(after_img)
                reached_target = (target.row, target.col) in selected_self_cells
                still_at_origin = (ally.row, ally.col) in selected_self_cells
                if reached_target and not still_at_origin:
                    logger.info(
                        f"移动确认成功: 我方已从 ({ally.row}, {ally.col}) "
                        f"到达 ({target.row}, {target.col})，"
                        f"菱形行动点={sorted(selected_self_cells)}，"
                        f"剩余行动范围={remaining_ranges}"
                    )
                    return True, False
                if remaining_ranges == 0:
                    post_grid = BattleGrid()
                    self.scanner.scan_grid(
                        post_grid,
                        context,
                        after_img,
                        cell_types=(CellType.SELF,),
                    )
                    reached_target = any(
                        cell.row == target.row and cell.col == target.col
                        for cell in post_grid.self_units
                    )
                    still_at_origin = any(
                        cell.row == ally.row and cell.col == ally.col
                        for cell in post_grid.self_units
                    )
                    post_points = [
                        cell.safe_click_point() for cell in post_grid.self_units
                    ]
                    # 镜头平移会让视觉网格在 120px 周期内产生相位偏移，
                    # 因此 move_center 对应的实际 row/col 不一定能和旧快照
                    # 直接相等。用目标蓝格与旧角色状态条的局部像素偏移，
                    # 预测移动后状态条位置，再在新截图中做邻近验证。
                    move_x, move_y = target.action_click_point("move")
                    ally_x, ally_y = ally.safe_click_point()
                    ally_grid_x = move_x + (ally.col - target.col) * CELL_WIDTH
                    ally_grid_y = move_y + (ally.row - target.row) * CELL_HEIGHT
                    expected_point = (
                        move_x + ally_x - ally_grid_x,
                        move_y + ally_y - ally_grid_y,
                    )
                    pixel_reached = any(
                        (point[0] - expected_point[0]) ** 2
                        + (point[1] - expected_point[1]) ** 2
                        <= 75**2
                        and (point[0] - ally_x) ** 2
                        + (point[1] - ally_y) ** 2
                        >= 75**2
                        for point in post_points
                    )
                    logger.debug(
                        "移动清层像素验证: "
                        f"原位置=({ally_x},{ally_y}), "
                        f"预测位置={expected_point}, 实际我方={post_points}"
                    )
                    if reached_target and not still_at_origin:
                        logger.info(
                            f"移动确认成功: 清层后我方已从 "
                            f"({ally.row}, {ally.col}) 到达 "
                            f"({target.row}, {target.col})"
                        )
                        return True, False
                    if pixel_reached:
                        logger.info(
                            "移动确认成功: 新我方状态条已到达目标蓝格的"
                            f"局部预测位置 {expected_point}"
                        )
                        return True, False
            else:
                after_target_red = self._red_bar_pixels(
                    after_img, target_bar_point
                )
                required_drop = max(8, int(before_target_red * 0.08))
                if after_target_red <= before_target_red - required_drop:
                    logger.info(
                        "攻击确认成功: 目标红色状态条像素 "
                        f"{before_target_red} -> {after_target_red}"
                    )
                    return True, False

        logger.warning(
            f"{label}未得到具体结果确认；拒绝用范围消失或整屏差异"
            f" {last_frame_score:.2f} 作为成功依据"
        )
        return False, False

    @staticmethod
    def _selected_self_cells(img: Any) -> set[Tuple[int, int]]:
        """从选中画面的一排蓝色菱形行动点定位我方所在格。"""
        if img is None or len(img.shape) < 2:
            return set()
        height = min(int(img.shape[0]), 1160)
        width = min(int(img.shape[1]), 720)
        hsv = cv2.cvtColor(img[:height, :width], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (95, 80, 130), (125, 255, 255))
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        fragments = []
        for x, y, box_width, box_height, area in stats[1:]:
            if (
                6 <= box_width <= 13
                and 5 <= box_height <= 9
                and 18 <= area <= 110
            ):
                fragments.append(
                    (
                        int(x),
                        int(y),
                        int(box_width),
                        int(box_height),
                    )
                )

        y_groups: List[List[Tuple[int, int, int, int]]] = []
        for fragment in sorted(
            fragments, key=lambda item: item[1] + item[3] // 2
        ):
            center_y = fragment[1] + fragment[3] // 2
            group = next(
                (
                    candidate
                    for candidate in y_groups
                    if abs(
                        center_y
                        - sum(
                            item[1] + item[3] // 2 for item in candidate
                        )
                        / len(candidate)
                    )
                    <= 3
                ),
                None,
            )
            if group is None:
                y_groups.append([fragment])
            else:
                group.append(fragment)

        cells: set[Tuple[int, int]] = set()
        for group in y_groups:
            sequence: List[Tuple[int, int, int, int]] = []
            for fragment in sorted(group, key=lambda item: item[0]):
                if sequence:
                    previous = sequence[-1]
                    center_gap = (
                        fragment[0]
                        + fragment[2] // 2
                        - previous[0]
                        - previous[2] // 2
                    )
                    if not 10 <= center_gap <= 22:
                        if len(sequence) >= 3:
                            AutoFightProcessor._append_selected_cell(
                                cells, sequence
                            )
                        sequence = []
                sequence.append(fragment)
            if len(sequence) >= 3:
                AutoFightProcessor._append_selected_cell(cells, sequence)
        return cells

    @staticmethod
    def _append_selected_cell(
        cells: set[Tuple[int, int]],
        sequence: List[Tuple[int, int, int, int]],
    ) -> None:
        """校验一排菱形的尺寸，并把其中心换算成局部网格。"""
        left = sequence[0][0]
        right = sequence[-1][0] + sequence[-1][2]
        if not 30 <= right - left <= 90:
            return
        center_x = (left + right) // 2
        center_y = int(
            sum(item[1] + item[3] // 2 for item in sequence)
            / len(sequence)
        )
        row = center_y // CELL_HEIGHT
        col = center_x // CELL_WIDTH
        if 0 <= row < ROWS and 0 <= col < COLS:
            cells.add((row, col))

    @staticmethod
    def _red_bar_pixels(img: Any, point: Tuple[int, int]) -> int:
        """统计目标状态条附近的红色像素，用于确认攻击造成的变化。"""
        if img is None:
            return 0
        center_x, center_y = point
        height, width = img.shape[:2]
        left = max(0, center_x - 70)
        right = min(width, center_x + 71)
        # 只统计血条本身的窄横带。旧的 ±16px 会把攻击预览的红色角标
        # 一并算入，确认点击后角标消失就会被误判为“目标扣血”。
        top = max(0, center_y - 4)
        bottom = min(height, center_y + 5)
        if left >= right or top >= bottom:
            return 0
        hsv = cv2.cvtColor(img[top:bottom, left:right], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 80, 130), (22, 255, 255))
        return int(cv2.countNonZero(mask))

    @staticmethod
    def _frame_difference(first: Any, second: Any) -> float:
        first_roi = cv2.resize(first[:1160], (180, 290))
        second_roi = cv2.resize(second[:1160], (180, 290))
        return float(cv2.absdiff(first_roi, second_roi).mean())

    @staticmethod
    def _camera_motion(first: Any, second: Any) -> Tuple[float, float, float]:
        """用背景相位相关估算真实镜头平移，忽略人物待机动画。"""
        if first is None or second is None:
            return 0.0, 0.0, 0.0
        first_gray = cv2.cvtColor(first[100:1080], cv2.COLOR_BGR2GRAY)
        second_gray = cv2.cvtColor(second[100:1080], cv2.COLOR_BGR2GRAY)
        first_small = cv2.resize(first_gray, (360, 490))
        second_small = cv2.resize(second_gray, (360, 490))
        first_edges = cv2.Sobel(first_small, cv2.CV_32F, 1, 1)
        second_edges = cv2.Sobel(second_small, cv2.CV_32F, 1, 1)
        (shift_x, shift_y), response = cv2.phaseCorrelate(
            first_edges, second_edges
        )
        return shift_x * 2.0, shift_y * 2.0, float(response)

    @staticmethod
    def _camera_shift_is_real(
        shift_x: float, shift_y: float, response: float
    ) -> bool:
        distance_squared = shift_x * shift_x + shift_y * shift_y
        # 整屏滑动实测约 240~480px。尸体、人物动画会降低相位相关
        # response，但不可能凭空产生这种量级的背景位移；大位移直接接纳。
        # 小幅居中仍要求较高置信度，避免把待机动画误当成镜头移动。
        return distance_squared >= 10000 or (
            response >= 0.08 and distance_squared >= 625
        )

    def _set_threat_overlay(self, context: Context, enabled: bool) -> bool:
        for attempt in range(1, 3):
            img = self._screencap(context)
            if img is None:
                continue
            current = context.run_recognition("Battle_ThreatToggleOn", img).hit
            if current == enabled:
                return True
            result = context.run_task("Battle_ToggleThreat")
            clicked = self._task_result_has_hit(result, {"Battle_ToggleThreat"})
            logger.debug(
                f"危险覆盖层切换为 {'开启' if enabled else '关闭'} "
                f"({attempt}/2): click={clicked}"
            )
            if not clicked:
                continue

            # 开关有淡入淡出动画。点击后轮询到目标状态再决定是否重试，
            # 避免 300ms 内识别到旧画面后再次点击，把刚关闭的红层重新打开。
            deadline = time.monotonic() + 1.4
            while time.monotonic() < deadline:
                time.sleep(0.15)
                settled_img = self._screencap(context)
                if settled_img is None:
                    continue
                settled = context.run_recognition(
                    "Battle_ThreatToggleOn", settled_img
                ).hit
                if settled == enabled:
                    return True

        final_img = self._screencap(context)
        verified = bool(
            final_img is not None
            and context.run_recognition("Battle_ThreatToggleOn", final_img).hit
            == enabled
        )
        if not verified:
            logger.warning(
                f"危险覆盖层未能确认切换为 {'开启' if enabled else '关闭'}"
            )
        return verified

    @classmethod
    def _round_marker_difference(cls, first: Any, second: Any) -> float:
        x, y, width, height = cls.ROUND_DIGIT_ROI
        if (
            first is None
            or second is None
            or first.shape[0] < y + height
            or second.shape[0] < y + height
            or first.shape[1] < x + width
            or second.shape[1] < x + width
        ):
            return 0.0
        first_roi = first[y : y + height, x : x + width]
        second_roi = second[y : y + height, x : x + width]
        return float(cv2.absdiff(first_roi, second_roi).mean())

    @classmethod
    def _track_round_change(
        cls, reference: Any, candidate: Any, frames: int, img: Any
    ) -> Tuple[bool, Any, int, float]:
        difference = cls._round_marker_difference(reference, img)
        if difference < cls.ROUND_CHANGE_THRESHOLD:
            return False, None, 0, difference
        stable_difference = (
            cls._round_marker_difference(candidate, img)
            if candidate is not None
            else float("inf")
        )
        frames = frames + 1 if stable_difference <= cls.ROUND_STABLE_THRESHOLD else 1
        return frames >= cls.ROUND_CONFIRM_FRAMES, img, frames, difference

    @classmethod
    def _poll_frames(
        cls, context: Context, timeout: float, interval: float
    ) -> Iterator[Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not context.tasker.stopping:
            img = cls._screencap(context)
            if img is not None:
                yield img
            time.sleep(interval)

    def _wait_for_round_change(
        self, context: Context, reference: Any, timeout: float
    ) -> bool:
        candidate = None
        confirm_frames = 0
        for img in self._poll_frames(context, timeout, 0.2):
            changed, candidate, confirm_frames, difference = self._track_round_change(
                reference, candidate, confirm_frames, img
            )
            if changed:
                logger.debug(
                    "常驻回合数字区域连续稳定变化，"
                    f"像素差异={difference:.2f}"
                )
                return True
        return False

    def _wait_for_scene_settle(self, context: Context, timeout: float) -> bool:
        started = time.monotonic()
        ready_frames = 0
        for img in self._poll_frames(context, timeout, 0.25):
            battle_result = self._detect_battle_result(context, img)
            if battle_result is not None:
                return True

            # 人物待机动画让整屏永远无法达到像素稳定。结束回合按钮是我方
            # 可操作状态下的持久 UI，连续两帧命中后即可开始下一次建图。
            ready = context.run_recognition("FightEndRound", img).hit
            ready_frames = ready_frames + 1 if ready else 0
            if ready_frames >= 2 and time.monotonic() - started >= 0.6:
                logger.debug("结束回合按钮连续命中，我方可操作状态已恢复")
                return True

        logger.warning("等待我方可操作状态超时，将重新扫描当前画面")
        return False

    @classmethod
    def _nearest_available_ally(
        cls,
        grid: BattleGrid,
        row: int,
        col: int,
        used_cells: Set[Tuple[int, int]],
    ) -> Optional[Cell]:
        candidates = [
            cell
            for cell in grid.self_units
            if (cell.row, cell.col) not in used_cells
            and cls._ally_is_actionable(cell)
        ]
        return cls._nearest_cell(candidates, row, col)

    @staticmethod
    def _nearest_cell(cells: List[Cell], row: int, col: int) -> Optional[Cell]:
        if not cells:
            return None
        return min(
            cells,
            key=lambda cell: abs(cell.row - row) + abs(cell.col - col),
        )

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

    @staticmethod
    def _task_result_has_hit(result: Any, names: Set[str]) -> bool:
        if not result or not result.nodes:
            return False
        for node in result.nodes:
            if getattr(node, "name", None) not in names:
                continue
            if getattr(node, "completed", False):
                return True
            recognition = getattr(node, "recognition", None)
            if recognition and getattr(recognition, "hit", False):
                return True
        return False
