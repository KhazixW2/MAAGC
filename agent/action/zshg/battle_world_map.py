"""Persistent battle-map state and eight-direction path planning.

The game exposes only a 6 x 10 viewport.  ``BattleWorldMap`` translates that
viewport into battle-global grid coordinates by accumulating verified camera
motion.  Dynamic unit observations deliberately keep their last known position
outside the current viewport: enemies are allowed to move between turns, but a
stale direction is still more useful than throwing the map away and searching
from scratch on every ``AutoFightProcessor`` invocation.

This module has no MaaFramework dependency so the mapping and planning rules can
be tested without connecting to the game.
"""

from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass, field
from math import inf
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


GridPoint = Tuple[int, int]
Direction = Tuple[int, int]

SELF = "self"
ENEMY = "enemy"
FRIEND = "friend"
ENVIRONMENT = "environment"

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


@dataclass
class UnitObservation:
    """Last known dynamic-unit observation in battle-global coordinates."""

    point: GridPoint
    kind: str
    round_seen: int
    source: str
    confidence: float = 1.0
    predicted: bool = False


@dataclass
class BattleWorldMap:
    """Persistent world model assembled from successive 6 x 10 viewports."""

    rows: int = 10
    cols: int = 6
    cell_width: int = 120
    cell_height: int = 120
    _camera_row_cells: float = 0.0
    _camera_col_cells: float = 0.0
    observed: Dict[GridPoint, int] = field(default_factory=dict)
    walkable: Set[GridPoint] = field(default_factory=set)
    blocked: Set[GridPoint] = field(default_factory=set)
    units: Dict[str, Dict[GridPoint, UnitObservation]] = field(
        default_factory=lambda: {
            SELF: {},
            ENEMY: {},
            FRIEND: {},
            ENVIRONMENT: {},
        }
    )
    boundaries: Dict[Direction, GridPoint] = field(default_factory=dict)
    successful_pans: int = 0

    @property
    def camera_origin(self) -> GridPoint:
        return (
            int(round(self._camera_row_cells)),
            int(round(self._camera_col_cells)),
        )

    def local_to_world(self, row: int, col: int) -> GridPoint:
        origin_row, origin_col = self.camera_origin
        return origin_row + row, origin_col + col

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

    def merge_units(
        self,
        kind: str,
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
        round_seen: int,
        *,
        allies: Iterable[GridPoint] = (),
        enemies: Iterable[GridPoint] = (),
        friends: Iterable[GridPoint] = (),
        environment: Iterable[GridPoint] = (),
        replace_visible: bool = True,
    ) -> None:
        self.mark_view_observed(round_seen)
        if replace_visible:
            visible = self.visible_points()
            # A normal frame is authoritative for allies in its viewport,
            # including positions that were only predicted after a move.
            for kind in (SELF, FRIEND, ENVIRONMENT):
                store = self.units.setdefault(kind, {})
                for point in list(store):
                    if point in visible:
                        del store[point]
        self.merge_units(
            SELF,
            allies,
            round_seen,
            "status_bar",
            replace_visible_source=False,
        )
        self.merge_units(
            ENEMY,
            enemies,
            round_seen,
            "status_bar",
            replace_visible_source=replace_visible,
        )
        self.merge_units(
            FRIEND,
            friends,
            round_seen,
            "status_bar",
            replace_visible_source=False,
        )
        self.merge_units(
            ENVIRONMENT,
            environment,
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
        store = self.units.setdefault(ENEMY, {})
        visible = self.visible_points()
        for point, observation in list(store.items()):
            if (
                point in visible
                and observation.source == "threat"
                and observation.round_seen < round_seen
            ):
                del store[point]
        return self.merge_units(
            ENEMY,
            local_points,
            round_seen,
            "threat",
            replace_visible_source=False,
            confidence=confidence,
        )

    def unit_points(
        self,
        kind: str,
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
        return bool(self.units.get(SELF))

    def has_enemies(self) -> bool:
        return bool(self.units.get(ENEMY) or self.units.get(ENVIRONMENT))

    def record_predicted_move(
        self, origin: GridPoint, target: GridPoint, round_seen: int
    ) -> None:
        store = self.units.setdefault(SELF, {})
        observation = store.pop(origin, None)
        confidence = observation.confidence if observation is not None else 0.75
        store[target] = UnitObservation(
            point=target,
            kind=SELF,
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
        limits = {SELF: ally_max_age, ENEMY: enemy_max_age, FRIEND: ally_max_age}
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
class BattleSessionState:
    """Mutable state that survives repeated AutoFight CustomAction calls."""

    key: str
    created_at: float = field(default_factory=time.monotonic)
    confirmed_rounds: int = 0
    action_cycles: int = 0
    no_progress_cycles: int = 0
    exploration_passes: int = 0
    last_move_direction: Optional[Direction] = None
    recent_move_points: List[GridPoint] = field(default_factory=list)
    world: BattleWorldMap = field(default_factory=BattleWorldMap)
    explorer: ClockwiseExplorer = field(default_factory=ClockwiseExplorer)

    def active(self, passive_rounds: int = 20) -> bool:
        return self.confirmed_rounds >= passive_rounds

    def record_round_advance(self) -> None:
        self.confirmed_rounds += 1
        self.world.decay_dynamic_units(self.confirmed_rounds)

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


def octile_distance(first: GridPoint, second: GridPoint) -> int:
    delta_row = abs(first[0] - second[0])
    delta_col = abs(first[1] - second[1])
    diagonal = min(delta_row, delta_col)
    straight = max(delta_row, delta_col) - diagonal
    return diagonal * 14 + straight * 10


def _attack_ring(targets: Iterable[GridPoint], blocked: Set[GridPoint]) -> Set[GridPoint]:
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
                (new_cost + heuristic(neighbor), new_cost, next(counter), neighbor),
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
        row_direction = 0 if candidate[0] == start[0] else (1 if candidate[0] > start[0] else -1)
        col_direction = 0 if candidate[1] == start[1] else (1 if candidate[1] > start[1] else -1)
        backtrack_penalty = 0
        if last_direction is not None:
            dot = row_direction * last_direction[0] + col_direction * last_direction[1]
            if dot < 0:
                # Backtracking remains legal; it is only less desirable.
                backtrack_penalty = 20
        progress_tiebreak = min(octile_distance(candidate, target) for target in targets)
        revisit_penalty = 80 if candidate in recent_set else 0
        score = route_cost + backtrack_penalty + revisit_penalty
        choice = score, revisit_penalty, progress_tiebreak, candidate
        if best is None or choice < best:
            best = choice
    return best[3] if best is not None else None


def session_key(context: object) -> str:
    """Build a stable process-local key for one bound Maa tasker/controller."""

    tasker = getattr(context, "tasker", None)
    controller = getattr(tasker, "controller", None)
    return f"tasker:{id(tasker)}:controller:{id(controller)}"
