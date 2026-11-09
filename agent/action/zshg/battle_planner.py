"""战场规划能力。

把局部战场快照与持久世界地图合并成统一视图，并计算移动落点：
优先真正缩短敌我距离的格子，其次才是沿粗粒度方向的绕行。
"""

from typing import Any, List, Optional, Tuple

from maa.context import Context

from utils import logger

from .battle_geometry import grid_direction, sign_direction
from .battle_grid import Cell, CellType, BattleGrid
from .battle_support import BattleSupport
from .battle_world_map import (
    ENEMY as WORLD_ENEMY,
    ENVIRONMENT as WORLD_ENVIRONMENT,
)


class BattlePlannerMixin(BattleSupport):
    """全局地图合并与移动落点规划。"""

    ENEMY_OBSERVATION_MAX_AGE = 0
    ENVIRONMENT_OBSERVATION_MAX_AGE = 2
    MOVE_TARGET_MIN_ALLY_DISTANCE = 105

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
        # 屏幕序方向为 (列方向, 行方向)，与局部网格的 (col, row) 一致。
        direction = sign_direction(col_delta, row_delta)
        return direction, world.world_to_local(target), environment_mode

    def _farthest_move_target(
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
            logger.debug(
                "相邻敌人攻击框被本体遮挡，按红条与攻击层恢复目标: "
                + str([(cell.row, cell.col) for cell in promoted])
            )
        return promoted
