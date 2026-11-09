"""单角色动作决策。

把“这一名我方角色这一回合做什么”拆成独立的小步骤：先找可攻击敌人，
再尝试攻击；攻击不成立时回退为向敌人移动或沿远端方向推进。
所有判断只承认可验证的证据，失败时交回外层调度下一名角色。
"""

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from maa.context import Context

from utils import logger

from .battle_geometry import (
    grid_direction,
    is_adjacent_cell,
    move_reduces_enemy_distance,
    screen_distance,
    world_direction,
)
from .battle_grid import Cell, CellType, BattleGrid
from .battle_support import BattleSupport
from .battle_world_map import choose_move_candidate


@dataclass
class _ActionPlan:
    """一次待执行的动作。"""

    target: Cell
    #: 动作标签，决定点击方式与结果校验语义（见 `_action_kind`）。
    label: str = "移动"


class BattleDecisionMixin(BattleSupport):
    """单角色的攻击 / 移动决策。"""

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
        attack_targets = self._collect_attack_targets(grid, ally)
        logger.debug(
            "可攻击敌人位置(E1): "
            + str([(cell.row, cell.col) for cell in attack_targets])
        )
        if attack_targets:
            attack_result = self._attempt_attack(
                context,
                grid,
                ally,
                before_img,
                round_reference,
                attack_targets,
            )
            if attack_result is not None:
                return attack_result
            # 黄色范围内的攻击未被游戏接受：重选我方并回退为向敌人移动。
            reselected = self._reselect_ally(context, grid, ally)
            if reselected is None:
                return False, None, False
            before_img, grid = reselected

        if not attack_targets:
            hidden_result = self._attempt_adjacent_hidden_attack(
                context,
                grid,
                ally,
                before_img,
                round_reference,
                hidden_enemy_cell,
            )
            if hidden_result is not None:
                return hidden_result

        plan = self._plan_action(
            grid, ally, pursuit_direction, hidden_enemy_cell
        )
        if plan is None:
            return False, None, False
        return self._execute_action(
            context, ally, before_img, round_reference, plan
        )

    # ====================
    # 攻击
    # ====================

    def _collect_attack_targets(
        self, grid: BattleGrid, ally: Cell
    ) -> List[Cell]:
        """当前可安全点击的敌人格；黄框被遮挡时尝试恢复相邻目标。"""
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
        return attack_targets

    def _attempt_attack(
        self,
        context: Context,
        grid: BattleGrid,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        attack_targets: List[Cell],
    ) -> Optional[Tuple[bool, Optional[Tuple[int, int]], bool]]:
        """攻击最近的可攻击敌人。

        返回 None 表示攻击未被游戏接受，调用方应回退为移动。
        """
        target = min(
            attack_targets,
            key=lambda cell: screen_distance(cell, ally, "attack"),
        )
        # 目标格子每回合都在变，属于连续刷新的进度信息：
        # 终端上原地覆盖同一行，日志文件里仍逐条保留。
        self._refresh_progress(f"攻击目标 ({target.row}, {target.col})")
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
        return None

    def _attempt_adjacent_hidden_attack(
        self,
        context: Context,
        grid: BattleGrid,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Optional[Tuple[bool, Optional[Tuple[int, int]], bool]]:
        """相邻锯齿威胁中心的直接双击攻击。

        隐匿敌人没有红色血条，人物立绘还可能遮住黄色攻击角标。
        这时 A* 会正确判断我方已经站在攻击环，却无法从 E1 得到
        攻击目标。锯齿中心来自当前威胁层、且与所选我方八方向相邻
        时，按游戏的点对点规则直接双击该格；远端地图记忆不走此
        分支，避免把过期坐标当成可点击敌人。
        """
        hidden_target = (
            grid.get_cell(*hidden_enemy_cell)
            if hidden_enemy_cell is not None
            else None
        )
        if hidden_target is None or not is_adjacent_cell(ally, hidden_target):
            return None

        logger.debug(
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

    def _reselect_ally(
        self, context: Context, grid: BattleGrid, ally: Cell
    ) -> Optional[Tuple[Any, BattleGrid]]:
        """攻击失败后重新选中我方，并重新捕获动作范围。"""
        if not self._cancel_selection(context):
            return None
        ally_x, ally_y = ally.safe_click_point()
        if not self._click_and_log(
            context, ally_x, ally_y, "攻击失败后重新选择我方"
        ):
            return None
        fallback_state = self._capture_selected_state(
            context, list(grid.enemy_units)
        )
        if fallback_state is None:
            return None
        before_img, fallback_grid, _ = fallback_state
        return before_img, fallback_grid

    # ====================
    # 落点规划
    # ====================

    def _plan_action(
        self,
        grid: BattleGrid,
        ally: Cell,
        pursuit_direction: Optional[Tuple[int, int]],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Optional[_ActionPlan]:
        """规划本角色这一回合的动作。"""
        move_targets = self._collect_move_targets(grid, ally)
        logger.debug(
            "可移动空白位置(2): "
            + str([(cell.row, cell.col) for cell in move_targets])
        )
        if not move_targets:
            if grid.enemy_units:
                # 点击未被黄色范围确认的敌人只会打开资料面板，并不会攻击。
                # 把当前角色视为本轮不可用，交给外层循环改选下一名我方；
                # 不能用一次假点击消耗动作预算并污染后续截图。
                logger.debug(
                    "当前角色没有已确认的攻击格或移动格，跳过并改选下一名我方"
                )
            return None

        target, used_world_plan = self._world_plan_target(
            grid, ally, move_targets, hidden_enemy_cell
        )
        if target is not None:
            return _ActionPlan(target=target)
        if used_world_plan:
            logger.debug(
                "我方已在目标攻击环或没有严格缩短路径的落点，"
                "本回合不绕行，等待攻击框/敌方位置刷新"
            )
            return None
        if grid.enemy_units:
            target = self._approach_visible_enemy(
                move_targets, ally, grid.enemy_units
            )
            return _ActionPlan(target=target) if target is not None else None
        if pursuit_direction is not None:
            return self._pursue_remote_direction(
                grid, ally, move_targets, pursuit_direction, hidden_enemy_cell
            )
        return None

    def _collect_move_targets(
        self, grid: BattleGrid, ally: Cell
    ) -> List[Cell]:
        """可点击的空白移动格，并排除被本方立绘遮挡的中心。"""
        move_targets = [
            cell
            for row in grid.cells
            for cell in row
            if cell.is_moveable
            and not cell.is_attackable
            and cell.cell_type == CellType.NONE
        ]
        return self._without_ally_occlusion(move_targets, ally)

    def _world_plan_target(
        self,
        grid: BattleGrid,
        ally: Cell,
        move_targets: List[Cell],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Tuple[Optional[Cell], bool]:
        """用世界地图 A* 规划落点，返回 ``(落点, 是否采用了世界计划)``。"""
        if self._session is None:
            return None, False
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
        if not world_targets:
            return None, False

        selected_world = choose_move_candidate(
            ally_world,
            list(candidate_cells),
            world_targets,
            blocked=world.blocked,
            explored=world.observed,
            last_direction=self._session.last_move_direction,
            recent_positions=self._session.recent_move_points,
        )
        if selected_world is None:
            return None, True
        logger.debug(
            "八方向 A* 选择本回合落点: "
            f"ally={ally_world}, target={selected_world}, "
            f"enemy={world_targets[0]}"
        )
        return candidate_cells[selected_world], True

    def _approach_visible_enemy(
        self,
        move_targets: List[Cell],
        ally: Cell,
        enemies: List[Cell],
    ) -> Optional[Cell]:
        """视野内有敌人但没有可信攻击目标时，向最近的敌人推进。"""
        nearest_enemy = min(
            enemies,
            key=lambda enemy: screen_distance(enemy, ally, "attack"),
        )
        ally_x, ally_y = ally.safe_click_point()
        enemy_x, enemy_y = nearest_enemy.safe_click_point()
        target = self._farthest_move_target(
            move_targets,
            ally,
            enemy_x - ally_x,
            enemy_y - ally_y,
            nearest_enemy,
        )
        if not move_reduces_enemy_distance(target, ally, nearest_enemy):
            logger.debug(
                "局部图没有直接缩短敌我距离的蓝格，"
                "选择代价最小的八方向绕障格"
            )
        logger.debug(
            "当前视野有敌人但没有可信攻击目标，"
            f"向敌人 ({nearest_enemy.row}, {nearest_enemy.col}) "
            "选择最远移动格"
        )
        return target

    def _pursue_remote_direction(
        self,
        grid: BattleGrid,
        ally: Cell,
        move_targets: List[Cell],
        pursuit_direction: Tuple[int, int],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Optional[_ActionPlan]:
        """按远端威胁方向推进；威胁中心已进入攻击范围时直接攻击。"""
        direction_x, direction_y = pursuit_direction
        hidden_target = (
            grid.get_cell(*hidden_enemy_cell)
            if hidden_enemy_cell is not None
            else None
        )
        if hidden_target is not None and hidden_target.is_attackable:
            logger.debug(
                "锯齿威胁中心已进入攻击范围，直接攻击格子: "
                f"({hidden_target.row}, {hidden_target.col})"
            )
            return _ActionPlan(target=hidden_target, label="威胁中心攻击")

        coarse_move_targets = self._coarse_move_candidates(move_targets, ally)
        if not coarse_move_targets:
            return None
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
            logger.debug(
                f"直达方向 {pursuit_direction} 被障碍阻挡，"
                "按局部图选择代价最小的八方向绕障格"
            )
        if projection == 0:
            logger.debug(
                f"直达方向 {pursuit_direction} 被障碍阻挡，"
                "选择八方向中的垂直绕行格"
            )
        logger.debug(
            f"当前视野无敌人，按远端方向 {pursuit_direction} "
            f"选择投影={projection} 的移动格"
        )
        return _ActionPlan(target=target)

    # ====================
    # 动作执行
    # ====================

    def _execute_action(
        self,
        context: Context,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        plan: _ActionPlan,
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """执行计划中的动作并校验结果。"""
        if self._action_kind(plan.label) == self.ACTION_KIND_ATTACK:
            return self._execute_attack(
                context, ally, before_img, round_reference, plan
            )
        self._refresh_progress(
            f"移动目标 ({plan.target.row}, {plan.target.col})"
        )
        if not self._click_cell(context, plan.target, plan.label):
            return False, None, False
        accepted, advanced = self._verify_action_result(
            context,
            before_img,
            round_reference,
            plan.label,
            ally,
            plan.target,
        )
        destination = (plan.target.row, plan.target.col) if accepted else None
        if accepted:
            self._record_move(ally, plan.target)
        return accepted, destination, advanced

    def _execute_attack(
        self,
        context: Context,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        plan: _ActionPlan,
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """执行一次攻击；未被接受时取消选择并交回外层。"""
        if not self._click_cell(context, plan.target, plan.label):
            return False, None, False
        accepted, advanced = self._verify_action_result(
            context,
            before_img,
            round_reference,
            plan.label,
            ally,
            plan.target,
        )
        if accepted or advanced:
            if accepted and self._session is not None:
                self._session.record_progress()
            return accepted, None, advanced
        logger.warning(f"{plan.label}未被游戏接受，取消选择后下回合重试")
        self._cancel_selection(context)
        return False, None, False

    def _record_move(self, ally: Cell, target: Cell) -> None:
        """记录本回合的移动方向，并同步更新持久地图的移动预测。"""
        self._last_confirmed_move_direction = grid_direction(ally, target)
        if self._session is not None:
            origin_world = self._session.world.local_to_world(
                ally.row, ally.col
            )
            target_world = self._session.world.local_to_world(
                target.row, target.col
            )
            row_direction, col_direction = world_direction(
                origin_world, target_world
            )
            self._session.last_move_direction = (row_direction, col_direction)
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
