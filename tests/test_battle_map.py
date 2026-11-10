import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "agent"))

from action.fight.battle_map import (
    BattleGrid,
    BattleMap,
    BattleSessionRegistry,
    CellKind,
    ClockwiseExplorer,
)
from action.fight.battle_strategy import astar_path, choose_move_candidate


def make_grid(
    *,
    self_units=(),
    enemies=(),
    friends=(),
    environment=(),
) -> BattleGrid:
    """构造一张只包含指定单位类型的局部网格。"""
    grid = BattleGrid()
    for row, col in self_units:
        grid.cells[row][col].kind = CellKind.SELF
    for row, col in enemies:
        grid.cells[row][col].kind = CellKind.ENEMY
    for row, col in friends:
        grid.cells[row][col].kind = CellKind.FRIEND
    for row, col in environment:
        # 场地目标与颜色身份正交：实体长条通常来自红色节点，
        # 但它不是普通敌人，不能进 enemy_units。
        grid.cells[row][col].kind = CellKind.ENEMY
        grid.cells[row][col].is_environment = True
    return grid


class BattleMapTests(unittest.TestCase):
    def tearDown(self) -> None:
        BattleSessionRegistry.clear()

    def test_camera_motion_translates_local_cells_to_world(self) -> None:
        world = BattleMap()
        world.merge_view(make_grid(self_units=[(4, 2)]), 0)
        self.assertIn((4, 2), world.units[CellKind.SELF])

        # A pan toward map-right makes the background move 480 pixels left.
        delta = world.apply_camera_motion(-480.0, 0.0)
        self.assertEqual((0, 4), delta)
        world.merge_view(make_grid(enemies=[(3, 1)]), 0)
        self.assertIn((3, 5), world.units[CellKind.ENEMY])

    def test_threat_observation_survives_normal_red_bar_refresh(self) -> None:
        world = BattleMap()
        world.merge_threat_cells([(2, 2)], round_seen=1)
        world.merge_view(make_grid(), 2)
        self.assertIn((2, 2), world.units[CellKind.ENEMY])

    def test_empty_threat_refresh_removes_visible_threat_point(self) -> None:
        world = BattleMap()
        world.merge_threat_cells([(2, 2)], round_seen=1)

        world.merge_threat_cells([], round_seen=2)

        self.assertNotIn((2, 2), world.units[CellKind.ENEMY])

    def test_empty_same_round_threat_refresh_keeps_confirmed_point(self) -> None:
        world = BattleMap()
        world.merge_threat_cells([(2, 2)], round_seen=3)

        # A later overlapping viewport in the same player turn can clip the
        # jagged hole.  The enemy has not had a chance to move, so its confirmed
        # threat cell must remain available to A* pursuit.
        world.merge_threat_cells([], round_seen=3)

        self.assertIn((2, 2), world.units[CellKind.ENEMY])

    def test_visible_status_bar_observation_is_replaced(self) -> None:
        world = BattleMap()
        world.merge_view(make_grid(enemies=[(2, 2)]), 1)
        world.merge_view(make_grid(), 2)
        self.assertNotIn((2, 2), world.units[CellKind.ENEMY])

    def test_visible_predicted_ally_is_replaced_by_normal_scan(self) -> None:
        world = BattleMap()
        world.merge_view(make_grid(self_units=[(2, 2)]), 1)
        world.record_predicted_move((2, 2), (2, 4), round_seen=1)
        world.merge_view(make_grid(), 2)
        self.assertNotIn((2, 4), world.units[CellKind.SELF])

    def test_fresh_targets_expire_enemies_but_keep_environment(self) -> None:
        world = BattleMap()
        world.merge_view(
            make_grid(enemies=[(1, 1)], environment=[(2, 2)]), 3
        )

        enemies, environment = world.fresh_targets(3)

        self.assertEqual([(1, 1)], enemies)
        self.assertEqual([(2, 2)], environment)

        # 敌人每回合都可能移动，旧坐标不能继续当终点；环境目标是静态的。
        later_enemies, later_environment = world.fresh_targets(4)
        self.assertEqual([], later_enemies)
        self.assertEqual([(2, 2)], later_environment)

    def test_session_survives_reentry_and_resets_for_new_battle(self) -> None:
        first = BattleSessionRegistry.begin("controller")
        for _ in range(20):
            first.record_round_advance()
        self.assertTrue(first.active())
        self.assertIs(first, BattleSessionRegistry.begin("controller"))

        second = BattleSessionRegistry.begin("controller", force_new=True)
        self.assertIsNot(first, second)
        self.assertFalse(second.active())


class ClockwiseExplorerTests(unittest.TestCase):
    def test_right_down_left_up_and_finish(self) -> None:
        explorer = ClockwiseExplorer()
        expected = [(0, 1), (1, 0), (0, -1), (-1, 0)]
        for direction in expected:
            self.assertEqual(direction, explorer.direction)
            explorer.record_pan(True)
            self.assertEqual(direction, explorer.direction)
            explorer.record_pan(False)
        self.assertTrue(explorer.completed)


class AStarTests(unittest.TestCase):
    def test_astar_uses_diagonals(self) -> None:
        path = astar_path((0, 0), {(3, 3)}, explored={(i, i) for i in range(4)})
        self.assertEqual([(0, 0), (1, 1), (2, 2), (3, 3)], path)

    def test_astar_does_not_cut_between_two_blocked_sides(self) -> None:
        path = astar_path(
            (0, 0),
            {(2, 2)},
            blocked={(0, 1), (1, 0)},
            explored={(0, 0), (1, 1), (2, 2)},
        )
        self.assertTrue(path)
        self.assertNotEqual((1, 1), path[1])
        self.assertEqual((2, 2), path[-1])

    def test_candidate_selection_allows_required_backtrack(self) -> None:
        selected = choose_move_candidate(
            (5, 5),
            [(5, 4)],
            [(5, 0)],
            explored={(5, col) for col in range(6)},
            last_direction=(0, 1),
        )
        self.assertEqual((5, 4), selected)

    def test_candidate_selection_uses_global_target(self) -> None:
        selected = choose_move_candidate(
            (5, 5),
            [(5, 7), (7, 5), (6, 6)],
            [(2, 9)],
            explored={(row, col) for row in range(10) for col in range(12)},
        )
        self.assertEqual((5, 7), selected)

    def test_candidate_selection_stays_when_already_in_attack_ring(self) -> None:
        selected = choose_move_candidate(
            (5, 4),
            [(4, 4), (5, 3), (6, 4)],
            [(5, 5)],
            explored={(row, col) for row in range(10) for col in range(10)},
        )

        self.assertIsNone(selected)

    def test_candidate_selection_requires_strict_path_progress(self) -> None:
        selected = choose_move_candidate(
            (5, 5),
            [(4, 5), (6, 5)],
            [(5, 9)],
            explored={(row, col) for row in range(10) for col in range(12)},
        )

        self.assertIsNone(selected)

    def test_candidate_selection_penalizes_recent_destination(self) -> None:
        selected = choose_move_candidate(
            (5, 5),
            [(4, 6), (6, 6)],
            [(5, 9)],
            explored={(row, col) for row in range(10) for col in range(12)},
            recent_positions=[(4, 6)],
        )

        self.assertEqual((6, 6), selected)


if __name__ == "__main__":
    unittest.main()
