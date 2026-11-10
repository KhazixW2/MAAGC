"""策略层：决定「这一回合该做什么」。

只做取舍，不碰画面：

- 站在这里时能不能打？谁最近？
- 打不过就往哪走？沿全图 A* 还是沿粗粒度威胁方向？
- 走到哪一格？

执行由 :mod:`battle_action` 负责，结果校验也由它负责。本模块只产出
:class:`ActionPlan`，把「做什么」表达清楚，交给动作层去落地。

A* 与落点挑选也放在这里：它们是“为哪个目标走哪条路”的决策算法，
不是地图状态本身。
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from math import inf
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from maa.context import Context

from utils import logger

from .battle_action import ActionKind, BattleActions
from .battle_classify import GridClassifier
from .battle_geometry import (
    ally_is_actionable,
    grid_direction,
    is_adjacent_cell,
    move_reduces_enemy_distance,
    octile_distance,
    screen_distance,
    sign_direction,
    world_direction,
)
from .battle_map import (
    EIGHT_DIRECTIONS,
    BattleGrid,
    BattleMap,
    BattleRuntime,
    BattleSessionState,
    Cell,
    CellKind,
    Direction,
    GridPoint,
)


@dataclass
class ActionPlan:
    """一次待执行的动作。

    ``kind`` 决定点击方式与结果校验语义；``label`` 只用于日志。
    """

    target: Cell
    kind: ActionKind = ActionKind.MOVE
    label: str = "移动"


# ====================
# 八方向寻路
# ====================


def _attack_ring(
    targets: Iterable[GridPoint], blocked: Set[GridPoint]
) -> Set[GridPoint]:
    goals: Set[GridPoint] = set()
    target_set = set(targets)
    for row, col in target_set:
        for row_delta, col_delta in EIGHT_DIRECTIONS:
            point = row + row_delta, col + col_delta
            if point not in blocked and point not in target_set:
                goals.add(point)
    return goals


def astar_path(
    start: GridPoint,
    goals: Iterable[GridPoint],
    *,
    blocked: Iterable[GridPoint] = (),
    explored: Iterable[GridPoint] = (),
    unknown_penalty: int = 6,
    max_expansions: int = 20000,
) -> List[GridPoint]:
    """Return an eight-direction path from ``start`` to any goal."""

    goal_set = set(goals)
    if not goal_set:
        return []
    if start in goal_set:
        return [start]

    blocked_set = set(blocked)
    explored_set = set(explored)
    relevant = goal_set | explored_set | {start}
    min_row = min(point[0] for point in relevant) - 8
    max_row = max(point[0] for point in relevant) + 8
    min_col = min(point[1] for point in relevant) - 8
    max_col = max(point[1] for point in relevant) + 8

    def heuristic(point: GridPoint) -> int:
        return min(octile_distance(point, goal) for goal in goal_set)

    queue: List[Tuple[int, int, int, GridPoint]] = []
    counter = itertools.count()
    heapq.heappush(queue, (heuristic(start), 0, next(counter), start))
    came_from: Dict[GridPoint, GridPoint] = {}
    cost_so_far: Dict[GridPoint, int] = {start: 0}
    expansions = 0

    while queue and expansions < max_expansions:
        _, current_cost, _, current = heapq.heappop(queue)
        if current_cost != cost_so_far.get(current):
            continue
        if current in goal_set:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path

        expansions += 1
        for row_delta, col_delta in EIGHT_DIRECTIONS:
            neighbor = current[0] + row_delta, current[1] + col_delta
            if not (
                min_row <= neighbor[0] <= max_row
                and min_col <= neighbor[1] <= max_col
            ):
                continue
            if neighbor in blocked_set:
                continue
            if row_delta and col_delta:
                side_a = current[0] + row_delta, current[1]
                side_b = current[0], current[1] + col_delta
                if side_a in blocked_set and side_b in blocked_set:
                    continue

            step_cost = 14 if row_delta and col_delta else 10
            if neighbor not in explored_set:
                step_cost += max(0, unknown_penalty)
            new_cost = current_cost + step_cost
            if new_cost >= cost_so_far.get(neighbor, inf):
                continue
            cost_so_far[neighbor] = new_cost
            came_from[neighbor] = current
            heapq.heappush(
                queue,
                (
                    new_cost + heuristic(neighbor),
                    new_cost,
                    next(counter),
                    neighbor,
                ),
            )
    return []


def choose_move_candidate(
    start: GridPoint,
    candidates: Sequence[GridPoint],
    targets: Sequence[GridPoint],
    *,
    blocked: Iterable[GridPoint] = (),
    explored: Iterable[GridPoint] = (),
    last_direction: Optional[Direction] = None,
    recent_positions: Iterable[GridPoint] = (),
) -> Optional[GridPoint]:
    """Choose a reachable cell that makes strict progress toward one target.

    Returning ``None`` while ``start`` is already in the attack ring is
    intentional.  Moving to another ring cell would make the unit orbit an
    enemy when the real problem is a clipped attack marker or an unstable
    selected-state frame.
    """

    if not candidates or not targets:
        return None
    blocked_set = set(blocked)
    explored_set = set(explored)
    goals = _attack_ring(targets, blocked_set)
    if not goals:
        goals = set(targets)
    if start in goals:
        return None

    def path_cost(path: Sequence[GridPoint]) -> float:
        if not path:
            return inf
        return sum(
            (14 if a[0] != b[0] and a[1] != b[1] else 10)
            + (0 if b in explored_set else 6)
            for a, b in zip(path, path[1:])
        )

    start_path = astar_path(
        start,
        goals,
        blocked=blocked_set,
        explored=explored_set,
    )
    start_route_cost = path_cost(start_path)
    recent_set = set(recent_positions)

    best: Optional[Tuple[float, int, int, GridPoint]] = None
    for candidate in candidates:
        path = astar_path(
            candidate,
            goals,
            blocked=blocked_set,
            explored=explored_set,
        )
        route_cost = path_cost(path)
        if route_cost == inf:
            continue
        if start_route_cost != inf and route_cost >= start_route_cost:
            continue
        row_direction = (
            0
            if candidate[0] == start[0]
            else (1 if candidate[0] > start[0] else -1)
        )
        col_direction = (
            0
            if candidate[1] == start[1]
            else (1 if candidate[1] > start[1] else -1)
        )
        backtrack_penalty = 0
        if last_direction is not None:
            dot = (
                row_direction * last_direction[0]
                + col_direction * last_direction[1]
            )
            if dot < 0:
                # Backtracking remains legal; it is only less desirable.
                backtrack_penalty = 20
        progress_tiebreak = min(
            octile_distance(candidate, target) for target in targets
        )
        revisit_penalty = 80 if candidate in recent_set else 0
        score = route_cost + backtrack_penalty + revisit_penalty
        choice = score, revisit_penalty, progress_tiebreak, candidate
        if best is None or choice < best:
            best = choice
    return best[3] if best is not None else None


# ====================
# 威胁方向
# ====================


class BattleStrategy:
    """单角色的攻击 / 移动决策。"""

    MOVE_TARGET_MIN_ALLY_DISTANCE = 105

    def __init__(
        self,
        session: BattleSessionState,
        actions: BattleActions,
        classifier: GridClassifier,
        runtime: BattleRuntime,
    ) -> None:
        self.session = session
        self.actions = actions
        self.classifier = classifier
        self.runtime = runtime

    # ====================
    # 入口
    # ====================

    def decide_and_act(
        self,
        context: Context,
        grid: BattleGrid,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        pursuit_direction: Optional[Direction],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """返回 ``(是否生效, 移动后的格子, 是否已经换回合)``。"""
        attack_targets = self.collect_attack_targets(grid, ally)
        logger.debug(
            "可攻击敌人位置(E1): "
            + str([(cell.row, cell.col) for cell in attack_targets])
        )
        if attack_targets:
            attack_result = self.attempt_attack(
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
            reselected = self.reselect_ally(context, grid, ally)
            if reselected is None:
                return False, None, False
            before_img, grid = reselected

        if not attack_targets:
            hidden_result = self.attempt_adjacent_hidden_attack(
                context,
                grid,
                ally,
                before_img,
                round_reference,
                hidden_enemy_cell,
            )
            if hidden_result is not None:
                return hidden_result

        plan = self.plan_action(
            grid, ally, pursuit_direction, hidden_enemy_cell
        )
        if plan is None:
            return False, None, False
        return self.execute_action(
            context, ally, before_img, round_reference, plan
        )

    # ====================
    # 攻击
    # ====================

    def collect_attack_targets(
        self, grid: BattleGrid, ally: Cell
    ) -> List[Cell]:
        """当前可安全点击的敌人格；黄框被遮挡时尝试恢复相邻目标。"""
        attack_targets = [
            cell
            for cell in grid.enemy_units
            if cell.is_attackable and not cell.is_moveable
        ]
        if not attack_targets:
            attack_targets = (
                self.classifier.promote_occluded_adjacent_targets(grid, ally)
            )
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

    def attempt_attack(
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
        self.actions.refresh_progress(f"攻击目标 ({target.row}, {target.col})")
        if not self.actions.click_cell(
            context, target, ActionKind.ATTACK, "攻击"
        ):
            return False, None, False
        accepted, advanced = self.actions.verify_action_result(
            context,
            before_img,
            round_reference,
            ActionKind.ATTACK,
            "攻击",
            ally,
            target,
        )
        if accepted or advanced:
            if accepted:
                self.session.record_progress()
            return accepted, None, advanced
        logger.warning(
            "黄色综合范围内的敌人未能实际攻击，"
            "重新选择我方并回退为向敌人移动"
        )
        return None

    def attempt_adjacent_hidden_attack(
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

        label = "相邻威胁中心攻击"
        logger.debug(
            "相邻锯齿威胁中心虽无黄色角标，直接双击攻击格子: "
            f"({hidden_target.row}, {hidden_target.col})"
        )
        if not self.actions.click_cell(
            context, hidden_target, ActionKind.ATTACK, label
        ):
            return False, None, False
        accepted, advanced = self.actions.verify_action_result(
            context,
            before_img,
            round_reference,
            ActionKind.ATTACK,
            label,
            ally,
            hidden_target,
        )
        if accepted or advanced:
            if accepted:
                self.session.record_progress()
            return accepted, None, advanced
        logger.warning(
            "相邻威胁中心双击未得到胜利、回合变化或血条变化确认，"
            "取消选择后下回合重建威胁图"
        )
        self.actions.cancel_selection(context)
        return False, None, False

    def reselect_ally(
        self, context: Context, grid: BattleGrid, ally: Cell
    ) -> Optional[Tuple[Any, BattleGrid]]:
        """攻击失败后重新选中我方，并重新捕获动作范围。"""
        if not self.actions.cancel_selection(context):
            return None
        ally_x, ally_y = ally.safe_click_point()
        if not self.actions.click_and_log(
            context, ally_x, ally_y, "攻击失败后重新选择我方"
        ):
            return None
        fallback_state = self.actions.capture_selected_state(
            context, list(grid.enemy_units)
        )
        if fallback_state is None:
            return None
        before_img, fallback_grid, _ = fallback_state
        return before_img, fallback_grid

    # ====================
    # 落点规划
    # ====================

    def plan_action(
        self,
        grid: BattleGrid,
        ally: Cell,
        pursuit_direction: Optional[Direction],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Optional[ActionPlan]:
        """规划本角色这一回合的动作。"""
        move_targets = self.collect_move_targets(grid, ally)
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

        target, used_world_plan = self.world_plan_target(
            grid, ally, move_targets, hidden_enemy_cell
        )
        if target is not None:
            return ActionPlan(target=target)
        if used_world_plan:
            logger.debug(
                "我方已在目标攻击环或没有严格缩短路径的落点，"
                "本回合不绕行，等待攻击框/敌方位置刷新"
            )
            return None
        if grid.enemy_units:
            target = self.approach_visible_enemy(
                move_targets, ally, grid.enemy_units
            )
            return ActionPlan(target=target) if target is not None else None
        if pursuit_direction is not None:
            return self.pursue_remote_direction(
                grid, ally, move_targets, pursuit_direction, hidden_enemy_cell
            )
        return None

    def collect_move_targets(
        self, grid: BattleGrid, ally: Cell
    ) -> List[Cell]:
        """可点击的空白移动格，并排除被本方立绘遮挡的中心。"""
        move_targets = [
            cell
            for row in grid.cells
            for cell in row
            if cell.is_moveable
            and not cell.is_attackable
            and cell.kind == CellKind.EMPTY
        ]
        return self.without_ally_occlusion(move_targets, ally)

    def world_plan_target(
        self,
        grid: BattleGrid,
        ally: Cell,
        move_targets: List[Cell],
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Tuple[Optional[Cell], bool]:
        """用世界地图 A* 规划落点，返回 ``(落点, 是否采用了世界计划)``。"""
        world = self.session.map
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
            fresh_enemies, fresh_environment = self.fresh_world_targets()
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
            last_direction=self.session.last_move_direction,
            recent_positions=self.session.recent_move_points,
        )
        if selected_world is None:
            return None, True
        logger.debug(
            "八方向 A* 选择本回合落点: "
            f"ally={ally_world}, target={selected_world}, "
            f"enemy={world_targets[0]}"
        )
        return candidate_cells[selected_world], True

    def approach_visible_enemy(
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
        target = self.farthest_move_target(
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

    def pursue_remote_direction(
        self,
        grid: BattleGrid,
        ally: Cell,
        move_targets: List[Cell],
        pursuit_direction: Direction,
        hidden_enemy_cell: Optional[Tuple[int, int]],
    ) -> Optional[ActionPlan]:
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
            return ActionPlan(
                target=hidden_target,
                kind=ActionKind.ATTACK,
                label="威胁中心攻击",
            )

        coarse_move_targets = self.coarse_move_candidates(move_targets, ally)
        if not coarse_move_targets:
            return None
        target = self.farthest_move_target(
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
        return ActionPlan(target=target)

    # ====================
    # 全局目标
    # ====================

    def fresh_world_targets(
        self,
    ) -> Tuple[List[GridPoint], List[GridPoint]]:
        """本回合仍可作为目标的敌人点与环境目标点。"""
        return self.session.map.fresh_targets(self.session.confirmed_rounds)

    def world_has_fresh_targets(self) -> bool:
        return self.session.map.has_fresh_targets(
            self.session.confirmed_rounds
        )

    def world_pursuit(
        self, grid: BattleGrid, ally: Optional[Cell] = None
    ) -> Tuple[Optional[Direction], Optional[Tuple[int, int]], bool]:
        """Return screen-order direction, local target and environment mode."""
        world = self.session.map
        if ally is None:
            actionable = [
                cell for cell in grid.self_units if ally_is_actionable(cell)
            ]
            if not actionable:
                return None, None, False
            ally = actionable[0]
        ally_world = world.local_to_world(ally.row, ally.col)
        enemy_points, environment_points = self.fresh_world_targets()
        environment_mode = not enemy_points and bool(environment_points)
        targets = enemy_points or environment_points
        if not targets:
            return self.runtime.coarse_threat_direction, None, environment_mode
        target = min(
            targets,
            key=lambda point: (
                max(
                    abs(point[0] - ally_world[0]),
                    abs(point[1] - ally_world[1]),
                ),
                point,
            ),
        )
        row_delta = target[0] - ally_world[0]
        col_delta = target[1] - ally_world[1]
        # 屏幕序方向为 (列方向, 行方向)，与局部网格的 (col, row) 一致。
        direction = sign_direction(col_delta, row_delta)
        return direction, world.world_to_local(target), environment_mode

    # ====================
    # 落点挑选
    # ====================

    def farthest_move_target(
        self,
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
            step_direction = grid_direction(ally, cell)
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

    def coarse_move_candidates(
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
        candidates = self.without_immediate_backtrack(move_targets, ally)
        if not candidates:
            # Wait one turn, then permit a reversal if the refreshed threat
            # direction still requires it.  This breaks ping-pong movement
            # without permanently forbidding a necessary retreat.
            self.runtime.last_confirmed_move_direction = None
            self.session.last_move_direction = None
            return []

        if not self.session.recent_move_points:
            return candidates
        recent = set(self.session.recent_move_points[-4:])
        fresh = [
            cell
            for cell in candidates
            if self.session.map.local_to_world(cell.row, cell.col)
            not in recent
        ]
        if fresh:
            logger.debug(
                f"粗方向追击优先 {len(fresh)} 个近期未访问移动格"
            )
            return fresh
        return candidates

    def without_immediate_backtrack(
        self, move_targets: List[Cell], ally: Cell
    ) -> List[Cell]:
        """排除上一移动方向后方的半平面，避免下一步大角度掉头。"""
        last_direction = self.runtime.last_confirmed_move_direction
        if last_direction is None:
            return move_targets
        candidates = [
            cell
            for cell in move_targets
            if (
                grid_direction(ally, cell)[0] * last_direction[0]
                + grid_direction(ally, cell)[1] * last_direction[1]
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
        logger.debug("局部图只剩大角度回退格，本回合不移动并等待地图变化")
        return []

    def without_ally_occlusion(
        self, move_targets: List[Cell], ally: Cell
    ) -> List[Cell]:
        """排除落在当前大体型立绘上的蓝格中心，避免点击被角色截获。"""
        ally_x, ally_y = ally.safe_click_point()
        min_distance_sq = self.MOVE_TARGET_MIN_ALLY_DISTANCE**2
        candidates = []
        for cell in move_targets:
            target_x, target_y = cell.action_click_point("move")
            if (
                target_x - ally_x
            ) ** 2 + (
                target_y - ally_y
            ) ** 2 >= min_distance_sq:
                candidates.append(cell)
        removed = len(move_targets) - len(candidates)
        if removed:
            logger.debug(f"局部路径排除 {removed} 个被我方立绘覆盖的近邻蓝格")
        return candidates or move_targets

    # ====================
    # 动作执行
    # ====================

    def execute_action(
        self,
        context: Context,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        plan: ActionPlan,
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """执行计划中的动作并校验结果。"""
        if plan.kind == ActionKind.ATTACK:
            return self.execute_attack(
                context, ally, before_img, round_reference, plan
            )
        self.actions.refresh_progress(
            f"移动目标 ({plan.target.row}, {plan.target.col})"
        )
        if not self.actions.click_cell(
            context, plan.target, plan.kind, plan.label
        ):
            return False, None, False
        accepted, advanced = self.actions.verify_action_result(
            context,
            before_img,
            round_reference,
            plan.kind,
            plan.label,
            ally,
            plan.target,
        )
        destination = (plan.target.row, plan.target.col) if accepted else None
        if accepted:
            self.record_move(ally, plan.target)
        return accepted, destination, advanced

    def execute_attack(
        self,
        context: Context,
        ally: Cell,
        before_img: Any,
        round_reference: Any,
        plan: ActionPlan,
    ) -> Tuple[bool, Optional[Tuple[int, int]], bool]:
        """执行一次攻击；未被接受时取消选择并交回外层。"""
        if not self.actions.click_cell(
            context, plan.target, plan.kind, plan.label
        ):
            return False, None, False
        accepted, advanced = self.actions.verify_action_result(
            context,
            before_img,
            round_reference,
            plan.kind,
            plan.label,
            ally,
            plan.target,
        )
        if accepted or advanced:
            if accepted:
                self.session.record_progress()
            return accepted, None, advanced
        logger.warning(f"{plan.label}未被游戏接受，取消选择后下回合重试")
        self.actions.cancel_selection(context)
        return False, None, False

    def record_move(self, ally: Cell, target: Cell) -> None:
        """记录本回合的移动方向，并同步更新持久地图的移动预测。"""
        self.runtime.last_confirmed_move_direction = grid_direction(
            ally, target
        )
        world = self.session.map
        origin_world = world.local_to_world(ally.row, ally.col)
        target_world = world.local_to_world(target.row, target.col)
        row_direction, col_direction = world_direction(
            origin_world, target_world
        )
        self.session.last_move_direction = (row_direction, col_direction)
        world.record_predicted_move(
            origin_world,
            target_world,
            self.session.confirmed_rounds,
        )
        self.session.record_move_point(target_world)
        self.session.record_progress()
        logger.debug(
            "记录局部路径移动方向: "
            f"{self.runtime.last_confirmed_move_direction}"
        )
