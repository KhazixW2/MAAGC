import time
from typing import Any, Iterator, List, Optional, Set, Tuple

import cv2
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import logger

from .battle_grid import BattleGrid, Cell, CellType, GridScanner, ROWS, COLS


@AgentServer.custom_action("AutoFightProcessor")
class AutoFightProcessor(CustomAction):
    """先被动等待反击，超过指定回合后执行主动战斗。

    策略：前 15 轮全部依赖「结束回合 + 自动反击」，让敌人自己走过来；
    仅当 15 轮后仍未通关时，才进入主动搜索与追击模式。这样可以减少决策
    次数，避免每局都触发 16 视野螺旋搜索的低性价比操作。
    """

    MAX_ROUNDS = 30
    ACTIVE_FROM_ROUND = 15
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
    ACTION_SAFE_X = (30, 690)
    # 战斗区域实际延伸到 y=1160；地图位于下边界时，状态条会停在
    # y≈1150，仍在 HUD 上沿之外，可以安全点击。
    ACTION_SAFE_Y = (100, 1155)
    SELECT_CAPTURE_ATTEMPTS = 4
    SELECT_CAPTURE_DELAY = 0.12

    def __init__(self) -> None:
        super().__init__()
        self.scanner = GridScanner()

    # 大地图关卡下敌我可能长期不在同一视野。超过该回合数仍未观察到
    # 敌人方向记忆时，主动丢弃记忆并切回纯螺旋搜索，避免绕远路。
    ENEMY_DIRECTION_MEMORY_ROUNDS = 6

    def run(
        self, context: Context, _argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        """执行战斗循环；每个角色行动前后都丢弃旧地图并重新扫描。"""
        round_count = 0
        survival_mode = False
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

        while round_count < self.MAX_ROUNDS:
            if context.tasker.stopping:
                logger.info("任务执行被停止")
                return CustomAction.RunResult(success=False)

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

            round_count += 1
            logger.debug(f"战斗循环 {round_count}：建立当前战场快照")
            round_reference = self._screencap(context)
            if round_reference is None:
                logger.error("建立回合快照失败")
                return CustomAction.RunResult(success=False)

            battle_result = self._detect_battle_result(context, round_reference)
            if battle_result is not None:
                return CustomAction.RunResult(success=battle_result)

            if not survival_mode:
                survival_reco = context.run_recognition(
                    "FightSurvivalObjective", round_reference
                )
                if survival_reco and survival_reco.hit:
                    survival_mode = True
                    logger.info(
                        "识别到坚守回合目标，本场强制保持被动反击模式"
                    )

            game_round = round_count
            # 严格按用户策略：前 ACTIVE_FROM_ROUND 轮全部被动，仅当游戏回合
            # 真正达到阈值后切换为主动模式。早期 needs_reconnaissance 不再
            # 触发主动出击（避免每局都做无谓的螺旋搜索）。
            use_active_strategy = not survival_mode and (
                game_round >= self.ACTIVE_FROM_ROUND
            )
            if not use_active_strategy:
                passive_mode = "坚守反击" if survival_mode else "被动反击"
                logger.info(f"第 {game_round} 回合：{passive_mode}，结束回合")
                passive_result = self._end_round(
                    context, round_reference, "被动阶段"
                )
                if passive_result is not None:
                    return CustomAction.RunResult(success=passive_result)
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

            round_grid = BattleGrid()
            self.scanner.scan_grid(round_grid, context, round_reference)
            needs_reconnaissance = (
                not round_grid.self_units or not round_grid.enemy_units
            )

            if game_round == self.ACTIVE_FROM_ROUND:
                logger.info(
                    f"战斗到达第 {game_round} 回合仍未结束，"
                    "切换为主动搜索与追击模式"
                )

            # 流程可能从镜头偏离我方的视野恢复，也可能处于所有单位行动完的
            # 半回合。两者都会表现为“当前画面没有完整蓝条 + 结束回合按钮存在”，
            # 因而不能仅凭结束回合按钮直接判定行动耗尽。先补帧，再全图找回
            # 我方；只有完整搜索仍找不到任何我方蓝条时，才允许结束回合。
            if not round_grid.self_units:
                for retry in range(1, 3):
                    time.sleep(0.2)
                    retry_img = self._screencap(context)
                    if retry_img is None:
                        continue
                    retry_grid = BattleGrid()
                    self.scanner.scan_grid(retry_grid, context, retry_img)
                    if retry_grid.self_units:
                        logger.info(f"第 {retry} 次补充取帧重新识别到我方单位")
                        round_reference = retry_img
                        round_grid = retry_grid
                        break

            if not round_grid.self_units:
                recovered_grid = self._search_for_allies_and_stay(context)
                if recovered_grid is None:
                    latest_img = self._screencap(context)
                    if (
                        latest_img is not None
                        and context.run_recognition(
                            "FightEndRound", latest_img
                        ).hit
                    ):
                        logger.warning(
                            f"已扫描最多 {self.MAX_SEARCH_SWIPES} 个相邻视野，"
                            "仍未重新找到我方单位；按已耗尽半回合恢复"
                        )
                        result = context.run_task("FightEndRound")
                        if not self._task_result_has_hit(
                            result, {"FightEndRound"}
                        ):
                            logger.error("找回我方失败后的结束回合点击失败")
                            return CustomAction.RunResult(success=False)
                        self._wait_for_round_change(
                            context, round_reference, timeout=6.0
                        )
                        self._wait_for_scene_settle(context, timeout=6.0)
                        continue
                    logger.error(
                        f"已扫描最多 {self.MAX_SEARCH_SWIPES} 个相邻视野，"
                        "仍未重新找到我方单位"
                    )
                    return CustomAction.RunResult(success=False)
                round_grid = recovered_grid
                round_reference = self._screencap(context)
                if round_reference is None:
                    logger.error("找回我方视野后建立回合快照失败")
                    return CustomAction.RunResult(success=False)

            round_grid, recentered = self._recenter_edge_allies(
                context, round_grid
            )
            if recentered:
                round_reference = self._screencap(context)
                if round_reference is None:
                    logger.error("居中我方视野后建立回合快照失败")
                    return CustomAction.RunResult(success=False)

            pursuit_direction: Optional[Tuple[int, int]] = None
            environment_mode = False
            if not round_grid.enemy_units and round_grid.self_units:
                local_environment = list(round_grid.environment_units)
                if local_environment:
                    environment_mode = True
                    logger.info(
                        "当前视野已确认祭坛/石碑类环境目标，"
                        f"直接行动，候选={len(local_environment)}"
                    )
                elif last_known_enemy_direction is not None:
                    # 陈旧方向记忆只能在「当前视野还能继续推进」时使用。
                    # bot 已在 col=0 / col=COLS-1 / row=0 / row=ROWS-1
                    # 时如果还按记忆方向走，会因为同列格子之间血条 X 坐标
                    # 噪声而在两三个格子间反复横跳。地图其实还能往该方向
                    # 滑动，必须先 pan camera 再决定方向。
                    if self._ally_at_grid_edge(
                        allies, last_known_enemy_direction
                    ):
                        logger.info(
                            f"陈旧方向记忆 {last_known_enemy_direction} "
                            f"（age={last_known_enemy_age}）"
                            "已撞当前视野边界，丢弃并触发螺旋搜索刷新方向"
                        )
                        last_known_enemy_direction = None
                        last_known_enemy_age = 0
                    else:
                        pursuit_direction = last_known_enemy_direction
                        logger.info(
                            f"沿用敌人方向记忆 {pursuit_direction} "
                            f"（age={last_known_enemy_age}），"
                            "本回合直接推进"
                        )
                if (
                    last_known_enemy_direction is None
                    and pursuit_direction is None
                    and not environment_mode
                    and not round_grid.enemy_units
                ):
                    search_result = self._search_for_enemies(context)
                    if search_result is None:
                        logger.error(
                            f"已扫描当前视野及最多 {self.MAX_SEARCH_SWIPES} "
                            "个相邻视野，仍未找到人物敌人或环境目标；"
                            "且无跨回合方向记忆，本场无法继续推进"
                        )
                        return CustomAction.RunResult(success=False)
                    (
                        pursuit_direction,
                        round_grid,
                        environment_mode,
                    ) = search_result
                    # 成功搜索到敌人方向就刷新记忆，后续回合直接按方向推进。
                    last_known_enemy_direction = pursuit_direction
                    last_known_enemy_age = 0
                    logger.info(
                        f"刷新敌人方向记忆: {last_known_enemy_direction}"
                    )

            unsafe_allies = [
                cell
                for cell in round_grid.self_units
                if not self._ally_is_actionable(cell)
            ]
            if unsafe_allies:
                logger.info(
                    f"忽略 {len(unsafe_allies)} 名位于屏幕边缘/系统 UI 区域的我方单位"
                )
            allies = [
                (cell.row, cell.col)
                for cell in round_grid.self_units
                if self._ally_is_actionable(cell)
            ]
            logger.info(
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
                action_pursuit = pursuit_direction
                current_targets = list(current_grid.enemy_units)
                if not current_targets and current_grid.environment_units:
                    # 同一个祭坛的红条会随受击/遮挡帧在“扁平人物条”和
                    # “数字+长条环境目标”之间切换。只要它仍在当前动作帧，
                    # 就统一作为目标，不因分类抖动重新启动全图搜索。
                    current_targets = list(current_grid.environment_units)
                    environment_mode = True
                    logger.info(
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
                        logger.info(
                            "选人前环境目标短暂被遮挡，沿用本回合搜索帧"
                            f"已确认的 {len(current_targets)} 个目标"
                        )
                if not current_targets and action_pursuit is None:
                    if environment_mode:
                        logger.info(
                            "环境目标在当前角色快照中不可见，"
                            "跳过本角色并在下一回合重新建图"
                        )
                        continue
                    search_result = self._search_for_enemies(context)
                    if search_result is None:
                        logger.error(
                            "角色行动前完成全地图搜索，仍未找到敌人；"
                            "停止以避免无目标移动"
                        )
                        return CustomAction.RunResult(success=False)
                    (
                        action_pursuit,
                        current_grid,
                        environment_mode,
                    ) = search_result
                    current_targets = list(
                        current_grid.environment_units
                        if environment_mode
                        else current_grid.enemy_units
                    )

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
                logger.info(
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

            # 最后一名角色行动后，游戏可能自行结束我方回合。留一个短暂
            # 宽限期，仅当常驻回合数字没有变化时才点击结束回合。
            if self._wait_for_round_change(context, round_reference, timeout=1.5):
                logger.info("检测到自动换回合，跳过结束回合按钮")
                self._wait_for_scene_settle(context, timeout=6.0)
                continue

            end_result = self._end_round(
                context, round_reference, "主动阶段"
            )
            if end_result is not None:
                return CustomAction.RunResult(success=end_result)
            self._wait_for_scene_settle(context, timeout=6.0)

        logger.error(f"战斗达到最大回合数 {self.MAX_ROUNDS}，停止任务")
        return CustomAction.RunResult(success=False)

    def _decide_and_act(
        self,
        context: Context,
        grid: BattleGrid,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        pursuit_direction: Optional[Tuple[int, int]],
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """返回 ``(是否生效, 移动后的格子, 是否已经换回合)``。"""
        attack_targets = [
            cell
            for cell in grid.enemy_units
            if cell.is_attackable and not cell.is_moveable
        ]
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

        logger.info(
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

        move_targets = [
            cell
            for row in grid.cells
            for cell in row
            if cell.is_moveable
            and not cell.is_attackable
            and cell.cell_type == CellType.NONE
        ]
        logger.info(
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

        if grid.enemy_units:
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
            logger.info(
                "当前视野有敌人但没有可信攻击目标，"
                f"向敌人 ({nearest_enemy.row}, {nearest_enemy.col}) "
                "选择最远移动格"
            )
        elif pursuit_direction is not None:
            direction_x, direction_y = pursuit_direction
            target = self._farthest_move_target(
                move_targets,
                ally,
                direction_x,
                direction_y,
            )
            ally_x, ally_y = ally.safe_click_point()
            target_x, target_y = target.action_click_point("move")
            projection = (
                (target_x - ally_x) * direction_x
                + (target_y - ally_y) * direction_y
            )
            if projection <= 0:
                logger.warning(
                    f"移动范围内没有朝远端敌人方向 {pursuit_direction} 的格子"
                )
                return False, None, False
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
        return accepted, destination, advanced

    @staticmethod
    def _screen_distance(first: Cell, second: Cell, action: str) -> int:
        first_x, first_y = first.action_click_point(action)
        second_x, second_y = second.safe_click_point()
        return (first_x - second_x) ** 2 + (first_y - second_y) ** 2

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

            def approach_score(cell: Cell) -> Tuple[int, int, int]:
                cell_x, cell_y = cell.action_click_point("move")
                delta_x = cell_x - ally_x
                delta_y = cell_y - ally_y
                enemy_distance = (
                    (cell_x - enemy_point[0]) ** 2
                    + (cell_y - enemy_point[1]) ** 2
                )
                projection = delta_x * direction_x + delta_y * direction_y
                displacement = delta_x * delta_x + delta_y * delta_y
                return enemy_distance, -projection, -displacement

            closer_targets = [
                cell
                for cell in move_targets
                if approach_score(cell)[0] < current_distance
            ]
            candidates = closer_targets or move_targets
            return min(candidates, key=approach_score)

        def score(cell: Cell) -> Tuple[int, int, int]:
            cell_x, cell_y = cell.action_click_point("move")
            delta_x = cell_x - ally_x
            delta_y = cell_y - ally_y
            projection = delta_x * direction_x + delta_y * direction_y
            displacement = delta_x * delta_x + delta_y * delta_y
            return projection, displacement, 0

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
            logger.info(
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
        logger.info(
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
        logger.info("左上人物卡片已收起")
        return True

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

        # 危险覆盖区是敌人的攻击范围，不是敌人坐标。远程/固定敌人的
        # 覆盖区质心经常位于敌人反方向；同时红色半透明地块会污染红色
        # 状态条识别。因此搜索期间保持覆盖层关闭，只认真实红色血条。
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
            logger.info(
                f"地图搜索视野 {views_scanned}: offset=({offset_x},{offset_y}), "
                f"真实红色状态条={len(search_grid.enemy_units)}, "
                f"环境目标={len(search_grid.environment_units)}, "
                f"我方={len(search_grid.self_units)}"
            )
            if search_grid.enemy_units or search_grid.environment_units:
                found_direction = (offset_x, offset_y)
                found_enemy_count = len(search_grid.enemy_units)
                found_environment_count = len(search_grid.environment_units)
                found_environment_only = (
                    not search_grid.enemy_units
                    and bool(search_grid.environment_units)
                )
                found_view = views_scanned
                found_grid = search_grid
                if found_environment_only:
                    logger.info(
                        "搜索视野已确认祭坛/石碑类环境目标："
                        f"数量={found_environment_count}"
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
            logger.info(
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
        self, context: Context, grid: BattleGrid
    ) -> Tuple[BattleGrid, bool]:
        """把边缘敌人/我方平移到可识别、可点击的安全区域。"""
        if not grid.self_units:
            return grid, False

        current_grid = grid
        moved = False
        screen_center = (360, 580)

        # 固定目标可能长期停在第 0 行/最外列。虽然红条还能被识别，黄色
        # 攻击角标却会被屏幕裁掉，角色便只会在附近移动而无法攻击。
        # 先用小步把真实敌人拉回画面，再重新建立完整网格。
        for _ in range(2):
            edge_enemy = next(
                (
                    cell
                    for cell in current_grid.enemy_units
                    if (
                        cell.safe_click_point()[1] < 160
                        or cell.safe_click_point()[1] > 1000
                        or cell.safe_click_point()[0] < 90
                        or cell.safe_click_point()[0] > 630
                    )
                ),
                None,
            )
            if edge_enemy is None:
                break
            enemy_x, enemy_y = edge_enemy.safe_click_point()
            if enemy_y < 160:
                direction = (0, -1)
            elif enemy_y > 1000:
                direction = (0, 1)
            elif enemy_x < 90:
                direction = (-1, 0)
            else:
                direction = (1, 0)
            logger.info(
                f"敌人状态条位于边缘 ({enemy_x},{enemy_y})，"
                f"向 {direction} 平移镜头以恢复完整攻击框"
            )
            if not self._pan_camera(context, direction, fine=True):
                logger.warning(f"居中敌人视野时地图滑动 {direction} 失败")
                break
            img = self._screencap(context)
            if img is None:
                break
            candidate_grid = BattleGrid()
            self.scanner.scan_grid(candidate_grid, context, img)
            if not candidate_grid.self_units or not candidate_grid.enemy_units:
                logger.warning("居中敌人后丢失我方或敌方，撤销本次滑动")
                self._pan_camera(
                    context, (-direction[0], -direction[1]), fine=True
                )
                break
            current_grid = candidate_grid
            moved = True

        if current_grid.self_units and all(
            self._ally_is_actionable(cell)
            for cell in current_grid.self_units
        ):
            return current_grid, moved

        for _ in range(4):
            unsafe_allies = [
                cell
                for cell in current_grid.self_units
                if not self._ally_is_actionable(cell)
            ]
            if not unsafe_allies:
                break
            target = min(
                unsafe_allies,
                key=lambda cell: (
                    cell.safe_click_point()[0] - screen_center[0]
                )
                ** 2
                + (
                    cell.safe_click_point()[1] - screen_center[1]
                )
                ** 2,
            )
            target_x, target_y = target.safe_click_point()
            # 每次只走一个小步并重新建图；横向调整可能同时改变纵向投影。
            if target_y > self.ACTION_SAFE_Y[1]:
                direction = (0, 1)
            elif target_y < self.ACTION_SAFE_Y[0]:
                direction = (0, -1)
            elif target_x > self.ACTION_SAFE_X[1]:
                direction = (1, 0)
            elif target_x < self.ACTION_SAFE_X[0]:
                direction = (-1, 0)
            else:
                break
            logger.info(
                f"我方状态条位于边缘 ({target_x},{target_y})，"
                f"向 {direction} 平移镜头以获得可操作视野"
            )
            if not self._pan_camera(context, direction, fine=True):
                logger.warning(f"居中我方视野时地图滑动 {direction} 失败")
                continue
            img = self._screencap(context)
            if img is None:
                continue
            candidate_grid = BattleGrid()
            self.scanner.scan_grid(candidate_grid, context, img)
            if not candidate_grid.self_units:
                logger.warning("居中滑动后暂时没有识别到我方，撤销本次滑动")
                self._pan_camera(
                    context, (-direction[0], -direction[1]), fine=True
                )
                continue
            candidate_target = min(
                candidate_grid.self_units,
                key=lambda cell: (
                    cell.safe_click_point()[0] - screen_center[0]
                )
                ** 2
                + (
                    cell.safe_click_point()[1] - screen_center[1]
                )
                ** 2,
            )
            candidate_x, candidate_y = candidate_target.safe_click_point()
            if abs(candidate_x - target_x) + abs(candidate_y - target_y) < 15:
                logger.info("细调镜头后我方坐标基本不变，判定已到地图边界")
                current_grid = candidate_grid
                break
            current_grid = candidate_grid
            moved = True

        return current_grid, moved

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
        """点击目标并再次点击确认；该游戏第一次点击只生成动作预览。"""
        action = "attack" if label == "攻击" else "move"
        x, y = cell.action_click_point(action)
        logger.info(f"{label}点击: ({cell.row}, {cell.col}) -> ({x}, {y})")
        previewed = self._click_and_log(context, x, y, f"{label}预览点击")
        if not previewed:
            return False
        time.sleep(0.35)
        confirmed = self._click_and_log(context, x, y, f"{label}确认点击")
        time.sleep(0.8)
        return confirmed

    @staticmethod
    def _click_and_log(context: Context, x: int, y: int, label: str) -> bool:
        job = context.tasker.controller.post_click(x, y).wait()
        succeeded = bool(job.succeeded)
        logger.info(f"{label}: ({x}, {y}), controller_succeeded={succeeded}")
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
                return True, True

            range_grid = BattleGrid()
            self.scanner.scan_ranges(range_grid, context, after_img)
            remaining_ranges = sum(
                1
                for row in range_grid.cells
                for cell in row
                if cell.is_moveable or cell.is_attackable
            )
            if remaining_ranges == 0:
                if label == "移动":
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
                    if reached_target and not still_at_origin:
                        logger.info(
                            f"移动确认成功: 我方已从 ({ally.row}, {ally.col}) "
                            f"到达 ({target.row}, {target.col})"
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
            logger.info(
                f"危险覆盖层切换为 {'开启' if enabled else '关闭'} "
                f"({attempt}/2): click={clicked}"
            )

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

        logger.info(
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
