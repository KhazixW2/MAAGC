"""战场地图层：局部视野网格与全局累积地图的统一模型。

地图层只回答“战场现在是什么样”，不读像素、不点击：

- :class:`CellKind` / :class:`Cell` / :class:`BattleGrid`
  当前 6x10 视野的局部网格（格子归一、点击坐标、可攻击/可移动标记）。
- :class:`BattleMap`
  局部网格与全局累积坐标的**统一模型**，负责两套坐标系的换算、
  单位观测的合并与老化。
- :class:`BattleSessionState` / :class:`BattleSessionRegistry`
  跨 ``CustomAction`` 调用存活的会话状态。

本模块不依赖 MaaFramework，地图规则可以脱离游戏单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from itertools import chain
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple


# ========================
# 局部网格常量
# ========================

ROWS: int = 10
COLS: int = 6
CELL_WIDTH: int = 120
CELL_HEIGHT: int = 120

GridPoint = Tuple[int, int]
Direction = Tuple[int, int]

CARDINAL_CLOCKWISE: Tuple[Direction, ...] = (
    (0, 1),   # camera right
    (1, 0),   # camera down
    (0, -1),  # camera left
    (-1, 0),  # camera up
)

EIGHT_DIRECTIONS: Tuple[Direction, ...] = (
    (-1, 0),
    (-1, 1),
    (0, 1),
    (1, 1),
    (1, 0),
    (1, -1),
    (0, -1),
    (-1, -1),
)

# 目标观测的有效期：敌人每回合都可能移动，旧坐标不能继续当终点；
# 环境目标是静态的，允许短暂遮挡后仍然有效。
ENEMY_OBSERVATION_MAX_AGE = 0
ENVIRONMENT_OBSERVATION_MAX_AGE = 2


class CellKind(Enum):
    """格子里是什么。

    角色（我方 / 敌方 / 友军）与场地（祭坛、石碑等静态目标）共用一套
    词汇，避免“枚举 + 布尔”双写导致的分类分裂。
    """

    EMPTY = "empty"
    SELF = "self"
    ENEMY = "enemy"
    FRIEND = "friend"
    ENVIRONMENT = "environment"


@dataclass
class Cell:
    """局部网格中的一格。"""

    row: int
    col: int
    kind: CellKind = CellKind.EMPTY
    unit_center: Tuple[int, int] = (0, 0)
    target_center: Tuple[int, int] = (0, 0)
    move_center: Tuple[int, int] = (0, 0)
    attack_center: Tuple[int, int] = (0, 0)
    is_moveable: bool = False
    is_attackable: bool = False

    def safe_click_point(self) -> Tuple[int, int]:
        """单位优先点击当前截图中的实际状态条中心。"""
        if self.unit_center != (0, 0):
            return self.unit_center
        return (
            self.col * CELL_WIDTH + CELL_WIDTH // 2,
            self.row * CELL_HEIGHT + CELL_HEIGHT // 2,
        )

    def action_click_point(self, action: str) -> Tuple[int, int]:
        """返回当前截图中与动作对应的实际范围中心。"""
        if action == "move" and self.move_center != (0, 0):
            # 战斗格会随镜头平移，视觉中心实测位于 120/240/360...；
            # 不能用静态格心 60/180/300... 替代当前范围框中心。
            return self.move_center
        if action == "attack":
            # 第一次点目标框生成攻击预览，第二次同点确认。点状态条只会
            # 打开敌人资料面板，因此必须优先使用角标聚合出的目标框中心。
            if self.attack_center != (0, 0):
                return self.attack_center
            if self.target_center != (0, 0):
                return self.target_center
            if self.unit_center != (0, 0):
                return self.unit_center
        return self.safe_click_point()


class BattleGrid:
    """当前视野的局部网格：只包含数据结构，提供格子访问接口。"""

    def __init__(self) -> None:
        self.cells = [[Cell(r, c) for c in range(COLS)] for r in range(ROWS)]

    def __iter__(self) -> Iterator[Cell]:
        return chain.from_iterable(self.cells)

    def _of_kind(self, kind: CellKind) -> List[Cell]:
        return [cell for cell in self if cell.kind == kind]

    @property
    def self_units(self) -> List[Cell]:
        return self._of_kind(CellKind.SELF)

    @property
    def enemy_units(self) -> List[Cell]:
        return self._of_kind(CellKind.ENEMY)

    @property
    def environment_units(self) -> List[Cell]:
        return self._of_kind(CellKind.ENVIRONMENT)

    @property
    def friend_units(self) -> List[Cell]:
        return self._of_kind(CellKind.FRIEND)

    def reset_flags(self) -> None:
        """重置所有格子的移动/攻击标记。"""
        for cell in self:
            cell.is_moveable = False
            cell.is_attackable = False
            cell.move_center = (0, 0)
            cell.attack_center = (0, 0)

    def reset(self) -> None:
        """清空全部动态状态，保证每张截图不会继承幽灵单位。"""
        for cell in self:
            cell.kind = CellKind.EMPTY
            cell.unit_center = (0, 0)
            cell.target_center = (0, 0)
            cell.move_center = (0, 0)
            cell.attack_center = (0, 0)
            cell.is_moveable = False
            cell.is_attackable = False

    def get_cell(self, row: int, col: int) -> Optional[Cell]:
        if 0 <= row < ROWS and 0 <= col < COLS:
            return self.cells[row][col]
        return None


# ========================
# 全局地图
# ========================


@dataclass
class UnitObservation:
    """Last known dynamic-unit observation in battle-global coordinates."""

    point: GridPoint
    kind: CellKind
    round_seen: int
    source: str
    confidence: float = 1.0
    predicted: bool = False


@dataclass
class BattleMap:
    """局部视野与全局累积坐标的统一地图。

    ``grid`` 是当前这一帧的 6x10 视野；``units`` / ``observed`` 是把
    历次视野累积到全局坐标后的结果。所有对外查询都返回全局坐标，
    局部 ``row/col`` 只在 ``to_world`` / ``to_local`` 的边界上出现。
    """

    rows: int = ROWS
    cols: int = COLS
    cell_width: int = CELL_WIDTH
    cell_height: int = CELL_HEIGHT
    grid: BattleGrid = field(default_factory=BattleGrid)
    _camera_row_cells: float = 0.0
    _camera_col_cells: float = 0.0
    observed: Dict[GridPoint, int] = field(default_factory=dict)
    walkable: Set[GridPoint] = field(default_factory=set)
    blocked: Set[GridPoint] = field(default_factory=set)
    units: Dict[CellKind, Dict[GridPoint, UnitObservation]] = field(
        default_factory=lambda: {
            CellKind.SELF: {},
            CellKind.ENEMY: {},
            CellKind.FRIEND: {},
            CellKind.ENVIRONMENT: {},
        }
    )
    boundaries: Dict[Direction, GridPoint] = field(default_factory=dict)
    successful_pans: int = 0

    # ----------------
    # 坐标系
    # ----------------

    @property
    def camera_origin(self) -> GridPoint:
        return (
            int(round(self._camera_row_cells)),
            int(round(self._camera_col_cells)),
        )

    def local_to_world(self, row: int, col: int) -> GridPoint:
        origin_row, origin_col = self.camera_origin
        return origin_row + row, origin_col + col

    def cell_to_world(self, cell: Cell) -> GridPoint:
        return self.local_to_world(cell.row, cell.col)

    def world_to_local(self, point: GridPoint) -> Optional[GridPoint]:
        origin_row, origin_col = self.camera_origin
        local = point[0] - origin_row, point[1] - origin_col
        if 0 <= local[0] < self.rows and 0 <= local[1] < self.cols:
            return local
        return None

    def visible_points(self) -> Set[GridPoint]:
        return {
            self.local_to_world(row, col)
            for row in range(self.rows)
            for col in range(self.cols)
        }

    def apply_camera_motion(
        self, shift_x: float, shift_y: float
    ) -> GridPoint:
        """Apply observed background motion and return integer origin delta.

        ``cv2.phaseCorrelate(before, after)`` reports content motion.  The camera
        origin moves in the opposite direction, so a left-shifting background
        advances the world viewport to the right.
        """

        before_row, before_col = self.camera_origin
        self._camera_col_cells += -shift_x / float(self.cell_width)
        self._camera_row_cells += -shift_y / float(self.cell_height)
        after_row, after_col = self.camera_origin
        delta = after_row - before_row, after_col - before_col
        if delta != (0, 0):
            self.successful_pans += 1
        return delta

    def mark_boundary(self, direction: Direction) -> None:
        self.boundaries[direction] = self.camera_origin

    def mark_view_observed(self, round_seen: int) -> None:
        for point in self.visible_points():
            self.observed[point] = round_seen

    # ----------------
    # 观测合并
    # ----------------

    def merge_units(
        self,
        kind: CellKind,
        local_points: Iterable[GridPoint],
        round_seen: int,
        source: str,
        *,
        replace_visible_source: bool = False,
        confidence: float = 1.0,
    ) -> List[GridPoint]:
        """Merge unit observations from the current camera view.

        Replacement is source-specific.  A normal red-bar scan may invalidate
        an old red-bar observation in the visible area, but it must not erase a
        hidden enemy previously obtained from the threat overlay.
        """

        store = self.units.setdefault(kind, {})
        if replace_visible_source:
            visible = self.visible_points()
            for point, observation in list(store.items()):
                if point in visible and observation.source == source:
                    del store[point]

        merged: List[GridPoint] = []
        for row, col in local_points:
            if not (0 <= row < self.rows and 0 <= col < self.cols):
                continue
            point = self.local_to_world(row, col)
            store[point] = UnitObservation(
                point=point,
                kind=kind,
                round_seen=round_seen,
                source=source,
                confidence=max(0.0, min(1.0, confidence)),
            )
            merged.append(point)
        return merged

    def merge_view(
        self,
        grid: BattleGrid,
        round_seen: int,
        *,
        replace_visible: bool = True,
    ) -> None:
        """把一张局部网格并入全局地图。"""

        self.grid = grid
        self.mark_view_observed(round_seen)
        if replace_visible:
            visible = self.visible_points()
            # A normal frame is authoritative for allies in its viewport,
            # including positions that were only predicted after a move.
            for kind in (CellKind.SELF, CellKind.FRIEND, CellKind.ENVIRONMENT):
                store = self.units.setdefault(kind, {})
                for point in list(store):
                    if point in visible:
                        del store[point]
        self.merge_units(
            CellKind.SELF,
            [(cell.row, cell.col) for cell in grid.self_units],
            round_seen,
            "status_bar",
            replace_visible_source=False,
        )
        self.merge_units(
            CellKind.ENEMY,
            [(cell.row, cell.col) for cell in grid.enemy_units],
            round_seen,
            "status_bar",
            replace_visible_source=replace_visible,
        )
        self.merge_units(
            CellKind.FRIEND,
            [(cell.row, cell.col) for cell in grid.friend_units],
            round_seen,
            "status_bar",
            replace_visible_source=False,
        )
        self.merge_units(
            CellKind.ENVIRONMENT,
            [(cell.row, cell.col) for cell in grid.environment_units],
            round_seen,
            "status_bar",
            replace_visible_source=False,
        )

    def merge_threat_cells(
        self,
        local_points: Iterable[GridPoint],
        round_seen: int,
        confidence: float = 0.8,
    ) -> List[GridPoint]:
        # The enemy cannot move during our own turn.  Clockwise exploration
        # observes overlapping viewports, and the jagged hole can be hidden by
        # edge clipping in a later viewport.  Do not let that empty same-round
        # frame erase a threat cell that was already confirmed.  Once the game
        # advances to a new round the old visible threat cells are stale and
        # may be replaced normally.
        store = self.units.setdefault(CellKind.ENEMY, {})
        visible = self.visible_points()
        for point, observation in list(store.items()):
            if (
                point in visible
                and observation.source == "threat"
                and observation.round_seen < round_seen
            ):
                del store[point]
        return self.merge_units(
            CellKind.ENEMY,
            local_points,
            round_seen,
            "threat",
            replace_visible_source=False,
            confidence=confidence,
        )

    def unit_points(
        self,
        kind: CellKind,
        current_round: Optional[int] = None,
        max_age: Optional[int] = None,
    ) -> List[GridPoint]:
        observations = self.units.get(kind, {}).values()
        if current_round is not None and max_age is not None:
            observations = (
                item
                for item in observations
                if current_round - item.round_seen <= max_age
            )
        return [
            item.point
            for item in sorted(
                observations,
                key=lambda value: (
                    -value.round_seen,
                    -value.confidence,
                    value.point,
                ),
            )
        ]

    def has_allies(self) -> bool:
        return bool(self.units.get(CellKind.SELF))

    def has_enemies(self) -> bool:
        return bool(
            self.units.get(CellKind.ENEMY)
            or self.units.get(CellKind.ENVIRONMENT)
        )

    def fresh_targets(
        self, current_round: int
    ) -> Tuple[List[GridPoint], List[GridPoint]]:
        """返回本回合仍可作为目标的敌人点与环境目标点。

        敌人每回合都能移动，旧坐标最多只能当方向提示，不能继续当 A* 终点；
        环境目标（祭坛 / 石碑）是静态的，允许短暂遮挡后仍然有效。
        """
        enemies = self.unit_points(
            CellKind.ENEMY,
            current_round=current_round,
            max_age=ENEMY_OBSERVATION_MAX_AGE,
        )
        environment = self.unit_points(
            CellKind.ENVIRONMENT,
            current_round=current_round,
            max_age=ENVIRONMENT_OBSERVATION_MAX_AGE,
        )
        return enemies, environment

    def has_fresh_targets(self, current_round: int) -> bool:
        enemies, environment = self.fresh_targets(current_round)
        return bool(enemies or environment)

    def record_predicted_move(
        self, origin: GridPoint, target: GridPoint, round_seen: int
    ) -> None:
        store = self.units.setdefault(CellKind.SELF, {})
        observation = store.pop(origin, None)
        confidence = observation.confidence if observation is not None else 0.75
        store[target] = UnitObservation(
            point=target,
            kind=CellKind.SELF,
            round_seen=round_seen,
            source="predicted_move",
            confidence=confidence,
            predicted=True,
        )
        self.walkable.add(target)

    def decay_dynamic_units(
        self,
        current_round: int,
        *,
        enemy_max_age: int = 12,
        ally_max_age: int = 4,
    ) -> None:
        limits = {
            CellKind.SELF: ally_max_age,
            CellKind.ENEMY: enemy_max_age,
            CellKind.FRIEND: ally_max_age,
        }
        for kind, max_age in limits.items():
            store = self.units.get(kind, {})
            for point, observation in list(store.items()):
                age = current_round - observation.round_seen
                if age > max_age:
                    del store[point]
                elif age > 0:
                    observation.confidence *= 0.92


@dataclass
class ClockwiseExplorer:
    """Right/down/left/up boundary exploration for one mapping pass."""

    max_swipes_per_direction: int = 12
    direction_index: int = 0
    swipes_in_direction: int = 0
    completed: bool = False
    successful_swipes: int = 0

    @property
    def direction(self) -> Direction:
        return CARDINAL_CLOCKWISE[min(self.direction_index, 3)]

    def record_pan(self, moved: bool) -> None:
        if self.completed:
            return
        if moved:
            self.swipes_in_direction += 1
            self.successful_swipes += 1
            if self.swipes_in_direction < self.max_swipes_per_direction:
                return

        self.direction_index += 1
        self.swipes_in_direction = 0
        if self.direction_index >= len(CARDINAL_CLOCKWISE):
            self.completed = True


@dataclass
class BattleRuntime:
    """跨层共享的运行时标志位。

    这些字段会被建图层写、被策略层读，放在一起避免它们散落成多个
    模块各自的隐藏状态。它们都是**单次 run 调用**内有效的；
    跨调用存活的只有 :class:`BattleSessionState`。
    """

    #: 威胁层给出的粗粒度八方向（只有方向，绝不是敌人坐标）。
    coarse_threat_direction: Optional[Direction] = None
    #: 上一回合确认过的移动方向，用于避免立刻掉头。
    last_confirmed_move_direction: Optional[Direction] = None
    #: 危险覆盖层当前是否可信（关闭失败会污染红条识别）。
    threat_overlay_safe: bool = True
    #: 跨回合敌人方向记忆：上次成功搜索到的敌人相对原点的螺旋方向。
    last_known_enemy_direction: Optional[Direction] = None
    #: 自上次成功记录方向以来经过的回合数。
    last_known_enemy_age: int = 0


@dataclass
class BattleSessionState:
    """Mutable state that survives repeated AutoFight CustomAction calls."""

    key: str
    created_at: float = field(default_factory=time.monotonic)
    confirmed_rounds: int = 0
    action_cycles: int = 0
    no_progress_cycles: int = 0
    exploration_passes: int = 0
    last_move_direction: Optional[Direction] = None
    # 战斗结算结果是否已经打印过。同一场战斗会被多个调用点反复检测，
    # 结算提示只打一次；新的一场战斗会新建 session，自然重新允许打印。
    reported_result: Optional[bool] = None
    recent_move_points: List[GridPoint] = field(default_factory=list)
    map: BattleMap = field(default_factory=BattleMap)
    explorer: ClockwiseExplorer = field(default_factory=ClockwiseExplorer)

    def active(self, passive_rounds: int = 20) -> bool:
        return self.confirmed_rounds >= passive_rounds

    def record_round_advance(self) -> None:
        self.confirmed_rounds += 1
        self.map.decay_dynamic_units(self.confirmed_rounds)

    def record_progress(self) -> None:
        self.no_progress_cycles = 0

    def record_move_point(self, point: GridPoint, history_limit: int = 8) -> None:
        """Remember accepted destinations so the planner can avoid short loops."""

        self.recent_move_points.append(point)
        if len(self.recent_move_points) > history_limit:
            del self.recent_move_points[:-history_limit]

    def record_no_progress(self) -> int:
        self.no_progress_cycles += 1
        return self.no_progress_cycles

    def new_exploration_pass(self) -> ClockwiseExplorer:
        self.exploration_passes += 1
        self.explorer = ClockwiseExplorer()
        return self.explorer


class BattleSessionRegistry:
    """Process-local registry keyed by controller/tasker identity."""

    _sessions: Dict[str, BattleSessionState] = {}

    @classmethod
    def begin(cls, key: str, *, force_new: bool = False) -> BattleSessionState:
        if force_new or key not in cls._sessions:
            cls._sessions[key] = BattleSessionState(key=key)
        return cls._sessions[key]

    @classmethod
    def get(cls, key: str) -> Optional[BattleSessionState]:
        return cls._sessions.get(key)

    @classmethod
    def end(cls, key: str) -> Optional[BattleSessionState]:
        return cls._sessions.pop(key, None)

    @classmethod
    def clear(cls) -> None:
        cls._sessions.clear()


def session_key(context: object) -> str:
    """Build a stable process-local key for one bound Maa tasker/controller."""

    tasker = getattr(context, "tasker", None)
    controller = getattr(tasker, "controller", None)
    return f"tasker:{id(tasker)}:controller:{id(controller)}"
