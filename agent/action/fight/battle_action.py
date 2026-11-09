"""行动执行与结果校验能力。

覆盖选中态捕获、点击提交、动作结果确认、结束回合与回合数字跟踪。
结果校验只承认可验证的证据（血条扣血、菱形行动点、回合数字变化），
绝不把“范围消失”当成动作成功。
"""

import time
from typing import Any, Iterator, List, Optional, Set, Tuple

from maa.context import Context

from utils import logger, progress
from utils.screen import screencap

from . import battle_vision
from .battle_geometry import nearest_cell
from .battle_grid import CELL_HEIGHT, CELL_WIDTH, Cell, CellType, BattleGrid
from .battle_support import BattleSupport


class BattleActionMixin(BattleSupport):
    """选中、点击、动作确认与回合推进。"""

    SELECT_CAPTURE_ATTEMPTS = 4
    SELECT_CAPTURE_DELAY = 0.12

    # 右下角“回合：N”中的数字是常驻 HUD，不依赖一闪而过的回合提示。
    # 720x1280 下只比较数字区域，人物移动或镜头滚动不会污染这个信号。
    ROUND_DIGIT_ROI = (670, 1175, 20, 25)
    # 两位数回合只改变个位时，固定的十位会稀释 ROI 平均差异；同一回合
    # 连续稳定帧实测差异为 0，因此使用 3.0 仍能避开静态噪声。
    ROUND_CHANGE_THRESHOLD = 10.0
    ROUND_STABLE_THRESHOLD = 1.5
    ROUND_CONFIRM_FRAMES = 2

    # ====================
    # 选中态
    # ====================

    def _capture_selected_state(
        self, context: Context, known_enemies: List[Cell]
    ) -> Optional[Tuple[Any, BattleGrid, int]]:
        """快速捕获选中后的动作范围，并带入选中前刚确认的真实敌人。"""
        last_state: Optional[Tuple[Any, BattleGrid, int]] = None
        for attempt in range(1, self.SELECT_CAPTURE_ATTEMPTS + 1):
            time.sleep(self.SELECT_CAPTURE_DELAY)
            img = screencap(context)
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
            range_count = battle_vision.count_action_range(grid)
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

        after_img = screencap(context)
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
        img = screencap(context)
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
        return battle_vision.count_action_range(grid)

    def _dismiss_role_card(self, context: Context) -> bool:
        """仅在左上人物卡片确实存在时点击收起，并验证卡片消失。"""
        img = screencap(context)
        if img is None:
            return False
        card = context.run_recognition("Battle_SelectedRoleCard", img)
        if not card or not card.hit:
            return True

        result = context.run_task("Battle_CollapseRoleCard")
        if not self._task_result_has_hit(result, {"Battle_CollapseRoleCard"}):
            logger.warning("人物卡片收起按钮点击失败")
            return False

        after_img = screencap(context)
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

    # ====================
    # 点击
    # ====================

    def _click_cell(self, context: Context, cell: Cell, label: str) -> bool:
        """局部网格点对点操作：移动格与敌人格都点击两次提交。"""
        x, y = cell.action_click_point(self._action_kind(label))
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

    # ====================
    # 结算与进度
    # ====================

    def _detect_battle_result(
        self, context: Context, img: Any
    ) -> Optional[bool]:
        """识别持久结算页；返回 True/False，仍在战斗则返回 None。

        同一场战斗会被多个调用点反复检测（每轮结束前、每次动作后），
        结算提示按 session 只打印一次，避免同一场战斗刷出重复日志。
        """
        if context.run_recognition("FightFail", img).hit:
            self._report_battle_result(False)
            return False
        if context.run_recognition("FightVictory", img).hit:
            self._report_battle_result(True)
            return True
        return None

    def _report_battle_result(self, victory: bool) -> None:
        """打印战斗结算结果；同一场战斗只打印一次。"""
        session = self._session
        if session is not None and session.reported_result is not None:
            return
        if session is not None:
            session.reported_result = victory
        if victory:
            logger.info("识别到战斗胜利结算页，战斗正常结束")
        else:
            logger.error("识别到战斗失败结算页")

    def _refresh_progress(self, detail: str = "") -> None:
        """刷新战斗进度行：回合数与最近一次动作画在同一行。

        终端上原地覆盖，不再逐条刷屏；日志文件里仍逐条保留。
        """
        rounds = self._session.confirmed_rounds if self._session else 0
        message = f"整场战斗 回合 {rounds}"
        if detail:
            message += f" · {detail}"
        # depth=3：日志里的调用位置要落在触发这次刷新的业务代码上，
        # 而不是这层封装里。
        progress.update(message, depth=3)

    def _record_round_advance(self, source: str) -> None:
        if self._session is None:
            return
        self._session.record_round_advance()
        self._refresh_progress(source)

    # ====================
    # 回合推进
    # ====================

    def _end_round(
        self, context: Context, round_reference: Any, phase: str
    ) -> Optional[bool]:
        """
        点击结束回合并验证进入下一回合。

        返回 None 表示已验证进入下一回合；True/False 表示
        已进入胜利/失败终态或操作无法确认。
        """
        for attempt in range(1, 3):
            current_img = screencap(context)
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

            latest_img = screencap(context)
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

    @classmethod
    def _poll_frames(
        cls, context: Context, timeout: float, interval: float
    ) -> Iterator[Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not context.tasker.stopping:
            img = screencap(context)
            if img is not None:
                yield img
            time.sleep(interval)

    @classmethod
    def _track_round_change(
        cls, reference: Any, candidate: Any, frames: int, img: Any
    ) -> Tuple[bool, Any, int, float]:
        difference = battle_vision.round_marker_difference(
            reference, img, cls.ROUND_DIGIT_ROI
        )
        if difference < cls.ROUND_CHANGE_THRESHOLD:
            return False, None, 0, difference
        stable_difference = (
            battle_vision.round_marker_difference(
                candidate, img, cls.ROUND_DIGIT_ROI
            )
            if candidate is not None
            else float("inf")
        )
        frames = frames + 1 if stable_difference <= cls.ROUND_STABLE_THRESHOLD else 1
        return frames >= cls.ROUND_CONFIRM_FRAMES, img, frames, difference

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

    # ====================
    # 动作结果确认
    # ====================

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
        is_move = self._action_kind(label) == self.ACTION_KIND_MOVE
        last_frame_score = 0.0
        target_bar_point = (
            target.target_center
            if target.target_center != (0, 0)
            else target.safe_click_point()
        )
        before_target_red = battle_vision.red_bar_pixels(
            before_img, target_bar_point
        )
        round_candidate = None
        round_confirm_frames = 0
        for after_img in self._poll_frames(context, timeout=3.5, interval=0.25):
            last_frame_score = battle_vision.frame_difference(before_img, after_img)
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
                logger.debug(
                    f"{label}后回合数字连续稳定变化，"
                    f"像素差异={round_score:.2f}，动作已生效"
                )
                self._record_round_advance(f"{label}后自动换回合")
                return True, True

            range_grid = BattleGrid()
            self.scanner.scan_ranges(range_grid, context, after_img)
            remaining_ranges = battle_vision.count_action_range(range_grid)
            if is_move:
                # 蓝色范围会与普通状态条连成大片，但选中角色上方一排独立
                # 菱形行动点仍保持清晰。先用该局部特征确认移动，既不需要
                # Esc（会撤销移动），也不需要二次点击目标格。
                selected_self_cells = battle_vision.selected_self_cells(after_img)
                reached_target = (target.row, target.col) in selected_self_cells
                still_at_origin = (ally.row, ally.col) in selected_self_cells
                if reached_target and not still_at_origin:
                    logger.debug(
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
                        logger.debug(
                            f"移动确认成功: 清层后我方已从 "
                            f"({ally.row}, {ally.col}) 到达 "
                            f"({target.row}, {target.col})"
                        )
                        return True, False
                    if pixel_reached:
                        logger.debug(
                            "移动确认成功: 新我方状态条已到达目标蓝格的"
                            f"局部预测位置 {expected_point}"
                        )
                        return True, False
            else:
                after_target_red = battle_vision.red_bar_pixels(
                    after_img, target_bar_point
                )
                required_drop = max(8, int(before_target_red * 0.08))
                if after_target_red <= before_target_red - required_drop:
                    logger.debug(
                        "攻击确认成功: 目标红色状态条像素 "
                        f"{before_target_red} -> {after_target_red}"
                    )
                    return True, False

        logger.warning(
            f"{label}未得到具体结果确认；拒绝用范围消失或整屏差异"
            f" {last_frame_score:.2f} 作为成功依据"
        )
        return False, False

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
        return nearest_cell(candidates, row, col)
