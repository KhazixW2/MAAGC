"""动作层：所有对游戏的操作。

这一层是唯一允许“动手”的地方——点击、点两下确认、平移镜头、开关
危险覆盖层、结束回合，以及动作之后的结果校验。

结果校验只承认可验证的证据（血条扣血、菱形行动点、回合数字变化），
绝不把“范围消失”当成动作成功。

动作语义用 :class:`ActionKind` 表达，不再靠中文标签里是否含“攻击”
来嗅探；点击方式与结果校验共用同一个枚举。
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any, Iterator, List, Optional, Set, Tuple

from maa.context import Context

from utils import logger, progress
from utils.screen import screencap

from . import battle_vision
from .battle_classify import GridClassifier
from .battle_geometry import ally_is_actionable, nearest_cell
from .battle_map import (
    CELL_HEIGHT,
    CELL_WIDTH,
    BattleGrid,
    BattleSessionState,
    Cell,
    CellKind,
)


class ActionKind(Enum):
    """动作语义。

    点击方式与结果校验共用这一个枚举，不再各自判断动作标签。
    """

    MOVE = "move"
    ATTACK = "attack"


def task_result_has_hit(result: Any, names: Set[str]) -> bool:
    """判断一次任务执行是否命中了指定节点。"""
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


class BattleActions:
    """战斗中的全部游戏操作。"""

    SELECT_CAPTURE_ATTEMPTS = 4
    SELECT_CAPTURE_DELAY = 0.12

    # 右下角“回合：N”是常驻 HUD，用 Pipeline 的 OCR 节点读取，不依赖
    # 一闪而过的回合提示，也不受人物移动或镜头滚动影响。
    ROUND_NUMBER_NODE = "FightRoundNumber"
    # 连续读到同一个新回合数字的帧数，达到后才确认回合推进。
    ROUND_CONFIRM_FRAMES = 2

    def __init__(
        self,
        session: BattleSessionState,
        classifier: GridClassifier,
    ) -> None:
        self.session = session
        self.classifier = classifier

    # ====================
    # 选中态
    # ====================

    def capture_selected_state(
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
            self.classifier.scan_grid(
                grid, context, img, cell_types=(CellKind.ENEMY,)
            )
            # 选中覆盖层会把尸体、技能特效和黄色格边缘染成红色碎片。
            # 这里只允许更新选中前同一动作快照已经确认的敌人格，不能在
            # 选中状态中凭空新增敌人并覆盖远端/红旗追击方向。
            known_positions = {
                (enemy.row, enemy.col) for enemy in known_enemies
            }
            for cell in grid:
                if (
                    cell.kind == CellKind.ENEMY
                    and (cell.row, cell.col) not in known_positions
                ):
                    cell.kind = CellKind.EMPTY
                    cell.unit_center = (0, 0)
                    cell.target_center = (0, 0)
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
                if cell.kind not in (CellKind.EMPTY, CellKind.ENVIRONMENT):
                    continue
                cell.kind = CellKind.ENEMY
                cell.unit_center = known_enemy.unit_center
                cell.target_center = known_enemy.target_center
            self.classifier.scan_ranges(grid, context, img)
            range_count = battle_vision.count_action_range(grid)
            last_state = (img, grid, range_count)
            logger.debug(
                f"选中状态取帧 {attempt}/{self.SELECT_CAPTURE_ATTEMPTS}: "
                f"可信敌方={len(grid.enemy_units)}, 动作范围格={range_count}"
            )
            if range_count:
                return last_state
        return last_state

    def cancel_selection(self, context: Context) -> bool:
        """点击右上 Esc 区域，并验证人物卡片和行动范围同时消失。"""
        result = context.run_task("Battle_Cancel")
        clicked = task_result_has_hit(result, {"Battle_Cancel"})
        if not clicked:
            logger.warning("右上 Esc 点击失败")
            return False

        after_img = screencap(context)
        if after_img is None:
            return False
        range_count = self.action_range_count(context, after_img)
        still_open = context.run_recognition(
            "Battle_SelectedRoleCard", after_img
        )
        cancelled = range_count == 0 and not (still_open and still_open.hit)
        logger.debug(
            f"取消当前选择: Esc点击={clicked}, "
            f"残留行动范围={range_count}, succeeded={cancelled}"
        )
        return cancelled

    def clear_stale_selection(self, context: Context) -> bool:
        """每轮建图前清理由中断或失败动作留下的选中覆盖层。"""
        img = screencap(context)
        if img is None:
            return False
        range_count = self.action_range_count(context, img)
        card = context.run_recognition("Battle_SelectedRoleCard", img)
        if range_count == 0 and not (card and card.hit):
            return True
        logger.warning(
            f"建图前发现残留选择状态: 行动范围={range_count}, "
            f"人物卡片={'有' if card and card.hit else '无'}"
        )
        return self.cancel_selection(context)

    def action_range_count(self, context: Context, img: Any) -> int:
        grid = BattleGrid()
        self.classifier.scan_ranges(grid, context, img)
        return battle_vision.count_action_range(grid)

    def dismiss_role_card(self, context: Context) -> bool:
        """仅在左上人物卡片确实存在时点击收起，并验证卡片消失。"""
        img = screencap(context)
        if img is None:
            return False
        card = context.run_recognition("Battle_SelectedRoleCard", img)
        if not card or not card.hit:
            return True

        result = context.run_task("Battle_CollapseRoleCard")
        if not task_result_has_hit(result, {"Battle_CollapseRoleCard"}):
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

    def click_cell(
        self,
        context: Context,
        cell: Cell,
        kind: ActionKind,
        label: str,
    ) -> bool:
        """局部网格点对点操作：移动格与敌人格都点击两次提交。"""
        x, y = cell.action_click_point(kind.value)
        logger.debug(f"{label}点击: ({cell.row}, {cell.col}) -> ({x}, {y})")
        first = self.click_and_log(context, x, y, f"{label}目标点击")
        if not first:
            return False
        # 第一次点击生成路径/攻击预览，第二次点击同一目标正式提交。
        time.sleep(0.2)
        second = self.click_and_log(context, x, y, f"{label}确认点击")
        # 4 倍速下 0.5 秒足以让镜头、人物和范围层落稳；后续仍通过
        # 画面/血条/回合变化做确认，而不是把等待本身当作成功。
        time.sleep(0.5)
        return second

    @staticmethod
    def click_and_log(context: Context, x: int, y: int, label: str) -> bool:
        job = context.tasker.controller.post_click(x, y).wait()
        succeeded = bool(job.succeeded)
        logger.debug(f"{label}: ({x}, {y}), controller_succeeded={succeeded}")
        return succeeded

    # ====================
    # 镜头与覆盖层
    # ====================

    def pan_camera(
        self,
        context: Context,
        direction: Tuple[int, int],
        fine: bool = False,
    ) -> bool:
        """按方向平移镜头；``fine`` 使用小幅微调节点。"""
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
        return task_result_has_hit(result, {node})

    def set_threat_overlay(self, context: Context, enabled: bool) -> bool:
        """把危险覆盖层切换到目标状态，并确认动画已经落稳。"""
        for attempt in range(1, 3):
            img = screencap(context)
            if img is None:
                continue
            current = context.run_recognition(
                "Battle_ThreatToggleOn", img
            ).hit
            if current == enabled:
                return True
            result = context.run_task("Battle_ToggleThreat")
            clicked = task_result_has_hit(
                result, {"Battle_ToggleThreat"}
            )
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
                settled_img = screencap(context)
                if settled_img is None:
                    continue
                settled = context.run_recognition(
                    "Battle_ThreatToggleOn", settled_img
                ).hit
                if settled == enabled:
                    return True

        final_img = screencap(context)
        verified = bool(
            final_img is not None
            and context.run_recognition(
                "Battle_ThreatToggleOn", final_img
            ).hit
            == enabled
        )
        if not verified:
            logger.warning(
                f"危险覆盖层未能确认切换为 {'开启' if enabled else '关闭'}"
            )
        return verified

    # ====================
    # 结算与进度
    # ====================

    def detect_battle_result(
        self, context: Context, img: Any
    ) -> Optional[bool]:
        """识别持久结算页；返回 True/False，仍在战斗则返回 None。

        同一场战斗会被多个调用点反复检测（每轮结束前、每次动作后），
        结算提示按 session 只打印一次，避免同一场战斗刷出重复日志。
        """
        if context.run_recognition("FightFail", img).hit:
            self.report_battle_result(False)
            return False
        if context.run_recognition("FightVictory", img).hit:
            self.report_battle_result(True)
            return True
        return None

    def report_battle_result(self, victory: bool) -> None:
        """打印战斗结算结果；同一场战斗只打印一次。"""
        session = self.session
        if session is not None and session.reported_result is not None:
            return
        if session is not None:
            session.reported_result = victory
        if victory:
            logger.info("识别到战斗胜利结算页，战斗正常结束")
        else:
            logger.error("识别到战斗失败结算页")

    def refresh_progress(self, detail: str = "") -> None:
        """刷新战斗进度行：回合数与最近一次动作画在同一行。

        终端上原地覆盖，不再逐条刷屏；日志文件里仍逐条保留。
        """
        rounds = self.session.confirmed_rounds if self.session else 0
        message = f"整场战斗 回合 {rounds}"
        if detail:
            message += f" · {detail}"
        # depth=3：日志里的调用位置要落在触发这次刷新的业务代码上，
        # 而不是这层封装里。
        progress.update(message, depth=3)

    def record_round_advance(self, source: str) -> None:
        if self.session is None:
            return
        self.session.record_round_advance()
        self.refresh_progress(source)

    # ====================
    # 回合推进
    # ====================

    def end_round(
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

            battle_result = self.detect_battle_result(context, current_img)
            if battle_result is not None:
                return battle_result
            if not context.run_recognition("FightEndRound", current_img).hit:
                logger.error(f"{phase}未识别到结束回合按钮")
                return False

            logger.debug(f"{phase}点击结束回合 ({attempt}/2)")
            result = context.run_task("FightEndRound")
            if not task_result_has_hit(result, {"FightEndRound"}):
                logger.warning(f"{phase}第 {attempt} 次结束回合节点未命中")
                continue

            timeout = 4.0 if attempt == 1 else 12.0
            if self.wait_for_round_change(
                context, round_reference, timeout=timeout
            ):
                logger.debug(f"{phase}已确认回合变化")
                self.record_round_advance(f"{phase}结束回合")
                return None

            latest_img = screencap(context)
            latest_result = (
                self.detect_battle_result(context, latest_img)
                if latest_img is not None
                else None
            )
            if latest_result is not None:
                return latest_result
            logger.warning(f"{phase}第 {attempt} 次点击后回合未变化")

        logger.error(f"{phase}两次结束回合均未生效")
        return False

    @classmethod
    def poll_frames(
        cls, context: Context, timeout: float, interval: float
    ) -> Iterator[Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not context.tasker.stopping:
            img = screencap(context)
            if img is not None:
                yield img
            time.sleep(interval)

    def read_round_number(self, context: Context, img: Any) -> Optional[int]:
        """读取右下角常驻 HUD 的回合数字；识别不到返回 None。

        HUD 文本可能被拆成“回合”和数字两个文本框，因此标签与数字要分开
        找，缺一不可，避免把结算页的其它数字当成回合。
        """
        detail = context.run_recognition(self.ROUND_NUMBER_NODE, img)
        texts = (
            [getattr(item, "text", "") or "" for item in detail.all_results]
            if detail
            else []
        )
        if not any("回合" in text for text in texts):
            return None
        for text in texts:
            match = re.search(r"\d+", text)
            if match:
                return int(match.group())
        return None

    def watch_round(
        self, context: Context, reference: Any, frames: Iterator[Any]
    ) -> Iterator[Tuple[Any, bool]]:
        """逐帧读取回合数字，产出 ``(截图, 是否已确认回合推进)``。

        基线取自参考帧；只有连续两帧读到同一个新数字才确认推进，
        避免把单帧识别抖动当成回合变化。
        """
        baseline = self.read_round_number(context, reference)
        if baseline is None:
            logger.warning("参考帧未读到回合数字，无法确认回合变化")
        previous: Optional[int] = None
        seen = 0
        for img in frames:
            number = self.read_round_number(context, img)
            if baseline is None or number is None or number == baseline:
                previous, seen = None, 0
            else:
                seen = seen + 1 if number == previous else 1
                previous = number
            yield img, seen >= self.ROUND_CONFIRM_FRAMES

    def wait_for_round_change(
        self, context: Context, reference: Any, timeout: float
    ) -> bool:
        """轮询回合数字，连续两帧读到同一个新数字即确认回合推进。"""
        frames = self.poll_frames(context, timeout, 0.2)
        return any(
            changed
            for _, changed in self.watch_round(context, reference, frames)
        )

    def wait_for_scene_settle(self, context: Context, timeout: float) -> bool:
        started = time.monotonic()
        ready_frames = 0
        for img in self.poll_frames(context, timeout, 0.25):
            battle_result = self.detect_battle_result(context, img)
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

    def verify_action_result(
        self,
        context: Context,
        before_img: Any,
        round_reference: Any,
        kind: ActionKind,
        label: str,
        ally: Cell,
        target: Cell,
    ) -> Tuple[bool, bool]:
        """确认具体动作结果，不能把“范围消失”误当成攻击/移动成功。"""
        is_move = kind == ActionKind.MOVE
        last_frame_score = 0.0
        target_bar_point = (
            target.target_center
            if target.target_center != (0, 0)
            else target.safe_click_point()
        )
        before_target_red = battle_vision.red_bar_pixels(
            before_img, target_bar_point
        )
        frames = self.poll_frames(context, timeout=3.5, interval=0.25)
        for after_img, round_changed in self.watch_round(
            context, round_reference, frames
        ):
            last_frame_score = battle_vision.frame_difference(
                before_img, after_img
            )
            battle_result = self.detect_battle_result(context, after_img)
            if battle_result is not None:
                return battle_result, True
            if round_changed:
                logger.debug(f"{label}后回合数字连续两帧变为新值，动作已生效")
                self.record_round_advance(f"{label}后自动换回合")
                return True, True

            range_grid = BattleGrid()
            self.classifier.scan_ranges(range_grid, context, after_img)
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
                    self.classifier.scan_grid(
                        post_grid,
                        context,
                        after_img,
                        cell_types=(CellKind.SELF,),
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

    # ====================
    # 选人辅助
    # ====================

    @classmethod
    def nearest_available_ally(
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
            and ally_is_actionable(cell)
        ]
        return nearest_cell(candidates, row, col)
