import sys
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.zshg.auto_fight_processor import AutoFightProcessor
from action.zshg.battle_grid import BattleGrid, Cell, CellType
from action.zshg.battle_world_map import BattleSessionState


class RecordingScanner:
    def __init__(self) -> None:
        self.cell_types = "not-called"

    def scan_grid(self, grid, context, img, cell_types=None):
        self.cell_types = cell_types
        return img


class WorldViewScanTests(unittest.TestCase):
    def make_processor(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor.scanner = RecordingScanner()
        processor._session = BattleSessionState(key="test")
        processor._coarse_threat_direction = None
        processor._merge_open_threat_overlay = lambda *args: []
        return processor

    def test_threat_overlay_does_not_run_red_enemy_scan(self):
        processor = self.make_processor()

        processor._scan_world_view(
            object(), object(), 1, overlay_open=True
        )

        self.assertEqual(
            set(processor.scanner.cell_types),
            {CellType.SELF, CellType.FRIEND},
        )

    def test_normal_view_scans_all_unit_colours(self):
        processor = self.make_processor()

        processor._scan_world_view(
            object(), object(), 1, overlay_open=False
        )

        self.assertIsNone(processor.scanner.cell_types)

    def test_unit_marker_churn_does_not_reset_no_progress_watchdog(self):
        processor = self.make_processor()
        processor._session.no_progress_cycles = 5
        frames = iter((object(), object()))
        processor._screencap = lambda _context: next(frames)
        processor._pan_camera = lambda *_args, **_kwargs: True
        processor._camera_motion = lambda *_args: (-120.0, 0.0, 1.0)
        processor._camera_shift_is_real = lambda *_args: True

        def scan_with_transient_enemy(_context, _img, round_seen, **_kwargs):
            processor._session.world.merge_threat_cells(
                [(2, 2)], round_seen=round_seen
            )
            return BattleGrid()

        processor._scan_world_view = scan_with_transient_enemy

        result = processor._pan_world_once(
            object(), (0, 1), 3, overlay_open=True, phase="test"
        )

        self.assertIsNotNone(result)
        self.assertEqual(5, processor._session.no_progress_cycles)


class ThreatCellTests(unittest.TestCase):
    def make_processor(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = BattleSessionState(key="threat-test")
        processor._coarse_threat_direction = None
        return processor

    @staticmethod
    def make_grid_with_ally() -> BattleGrid:
        grid = BattleGrid()
        ally = grid.get_cell(5, 1)
        ally.cell_type = CellType.SELF
        ally.unit_center = (180, 660)
        return grid

    def test_region_or_continuous_origin_is_direction_only(self):
        processor = self.make_processor()
        processor._threat_enemy_cell_from_mask = lambda *_: None
        processor._threat_origin_from_mask = lambda *_: (600, 180, 0.8)
        context = SimpleNamespace(
            run_recognition=lambda *_: SimpleNamespace(
                hit=True,
                filtered_results=[SimpleNamespace(box=(240, 120, 360, 480))],
            )
        )

        points = processor._merge_open_threat_overlay(
            context,
            np.zeros((1280, 720, 3), dtype=np.uint8),
            self.make_grid_with_ally(),
            3,
        )

        self.assertEqual([], points)
        self.assertEqual((1, -1), processor._coarse_threat_direction)
        self.assertEqual([], processor._session.world.unit_points("enemy"))

    def test_only_exact_jagged_cell_is_persisted(self):
        processor = self.make_processor()
        processor._threat_enemy_cell_from_mask = lambda *_: (2, 3, 1.2)
        context = SimpleNamespace(run_recognition=lambda *_: None)

        points = processor._merge_open_threat_overlay(
            context,
            np.zeros((1280, 720, 3), dtype=np.uint8),
            self.make_grid_with_ally(),
            4,
        )

        self.assertEqual([(2, 3)], points)
        self.assertEqual([(2, 3)], processor._session.world.unit_points("enemy"))

    def test_jagged_hollow_cell_is_selected_from_red_grid(self):
        processor = self.make_processor()
        hsv = np.zeros((1200, 720, 3), dtype=np.uint8)
        red = np.array((10, 160, 120), dtype=np.uint8)
        row, col = 4, 3
        for neighbour in ((3, 3), (5, 3), (4, 2), (4, 4)):
            nr, nc = neighbour
            hsv[nr * 120 : (nr + 1) * 120, nc * 120 : (nc + 1) * 120] = red
        y0, x0 = row * 120, col * 120
        hsv[y0 : y0 + 25, x0 : x0 + 120] = red
        hsv[y0 + 95 : y0 + 120, x0 : x0 + 120] = red
        hsv[y0 : y0 + 120, x0 : x0 + 25] = red
        hsv[y0 : y0 + 120, x0 + 95 : x0 + 120] = red
        image = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

        result = processor._threat_enemy_cell_from_mask(image, [])

        self.assertIsNotNone(result)
        self.assertEqual((row, col), result[:2])


class EdgeRecenterTests(unittest.TestCase):
    def test_corner_unit_uses_diagonal_direction(self):
        unit = Cell(0, 5, unit_center=(690, 60))

        direction = AutoFightProcessor._edge_recenter_direction(
            unit,
            x_bounds=(90, 630),
            y_bounds=(160, 1000),
        )

        self.assertEqual((1, -1), direction)

    def test_hidden_bottom_right_target_uses_diagonal_nudge(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = BattleSessionState(key="edge-hidden")
        grid = BattleGrid()
        ally = grid.get_cell(5, 3)
        ally.cell_type = CellType.SELF
        ally.unit_center = (420, 660)
        calls = []

        def fake_pan(_context, direction, _round_seen, **_kwargs):
            calls.append(direction)
            return False, None, None

        processor._pan_world_once = fake_pan

        processor._recenter_edge_allies(
            object(), grid, 1, known_target=(9, 5)
        )

        self.assertEqual([(1, 1)], calls)


class FocusKnownAllyTests(unittest.TestCase):
    def test_actionable_edge_ally_is_kept_when_camera_hits_boundary(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = BattleSessionState(key="focus-boundary")
        processor._set_threat_overlay = lambda *_args: True
        image = object()
        processor._screencap = lambda _context: image

        grid = BattleGrid()
        ally = grid.get_cell(9, 3)
        ally.cell_type = CellType.SELF
        processor._scan_world_view = lambda *_args, **_kwargs: grid
        processor._pan_world_once = (
            lambda *_args, **_kwargs: (False, None, image)
        )

        focused = processor._focus_known_ally(object(), round_seen=4)

        self.assertIsNotNone(focused)
        self.assertIs(grid, focused[0])
        self.assertIs(image, focused[1])


class CoarsePursuitLoopTests(unittest.TestCase):
    def make_processor(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = BattleSessionState(key="coarse-loop")
        processor._last_confirmed_move_direction = None
        return processor

    def test_only_immediate_reverse_waits_one_turn_and_clears_direction(self):
        processor = self.make_processor()
        processor._last_confirmed_move_direction = (-1, 1)
        processor._session.last_move_direction = (1, -1)
        ally = Cell(5, 1)
        reverse = Cell(4, 2)

        candidates = processor._coarse_move_candidates([reverse], ally)

        self.assertEqual([], candidates)
        self.assertIsNone(processor._last_confirmed_move_direction)
        self.assertIsNone(processor._session.last_move_direction)

    def test_recent_coarse_destination_is_deprioritized(self):
        processor = self.make_processor()
        ally = Cell(5, 1)
        recent = Cell(4, 2)
        fresh = Cell(6, 2)
        processor._session.record_move_point((4, 2))

        candidates = processor._coarse_move_candidates(
            [recent, fresh], ally
        )

        self.assertEqual([fresh], candidates)


class OccludedAttackTargetTests(unittest.TestCase):
    def test_adjacent_red_bar_is_promoted_when_attack_layer_exists(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor.scanner = SimpleNamespace(ATTACK_MARKER_Y_OFFSET=40)
        grid = BattleGrid()
        ally = Cell(1, 1, cell_type=CellType.SELF, unit_center=(180, 200))
        enemy = grid.get_cell(1, 2)
        enemy.cell_type = CellType.ENEMY
        enemy.unit_center = (300, 200)
        grid.get_cell(3, 3).is_attackable = True

        promoted = processor._promote_occluded_adjacent_targets(grid, ally)

        self.assertEqual([enemy], promoted)
        self.assertEqual((300, 160), enemy.attack_center)

    def test_adjacent_red_bar_is_not_promoted_without_attack_layer(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor.scanner = SimpleNamespace(ATTACK_MARKER_Y_OFFSET=40)
        grid = BattleGrid()
        ally = Cell(1, 1, cell_type=CellType.SELF, unit_center=(180, 200))
        enemy = grid.get_cell(1, 2)
        enemy.cell_type = CellType.ENEMY
        enemy.unit_center = (300, 200)

        promoted = processor._promote_occluded_adjacent_targets(grid, ally)

        self.assertEqual([], promoted)
        self.assertFalse(enemy.is_attackable)

    def test_adjacent_hidden_enemy_is_double_clicked_without_yellow_marker(self):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = BattleSessionState(key="hidden-adjacent")
        processor._promote_occluded_adjacent_targets = lambda *_: []
        clicked = []
        processor._click_cell = (
            lambda _context, cell, label: clicked.append(
                (cell.row, cell.col, label)
            )
            or True
        )
        processor._verify_action_result = lambda *_: (True, False)

        grid = BattleGrid()
        ally = grid.get_cell(5, 3)
        ally.cell_type = CellType.SELF

        result = processor._decide_and_act(
            object(),
            grid,
            ally,
            object(),
            object(),
            None,
            (5, 2),
        )

        self.assertEqual((True, None, False), result)
        self.assertEqual([(5, 2, "相邻威胁中心攻击")], clicked)

    def test_hidden_enemy_attack_requires_eight_direction_adjacency(self):
        ally = Cell(5, 3)

        self.assertTrue(
            AutoFightProcessor._is_adjacent_cell(ally, Cell(4, 2))
        )
        self.assertFalse(
            AutoFightProcessor._is_adjacent_cell(ally, Cell(5, 3))
        )
        self.assertFalse(
            AutoFightProcessor._is_adjacent_cell(ally, Cell(5, 1))
        )


class EdgePipelineTests(unittest.TestCase):
    def test_all_four_diagonal_pipeline_nodes_exist(self):
        pipeline_path = (
            Path(__file__).resolve().parents[1]
            / "assets/resource/base/pipeline/battle_grid.json"
        )
        data = json.loads(pipeline_path.read_text(encoding="utf-8"))

        for name in (
            "Battle_MapPanUpLeft",
            "Battle_MapPanUpRight",
            "Battle_MapPanDownLeft",
            "Battle_MapPanDownRight",
        ):
            self.assertEqual("Swipe", data[name]["action"])

    def test_enemy_and_ally_edge_axes_are_combined(self):
        enemy = Cell(0, 3, unit_center=(360, 80))
        ally = Cell(4, 5, unit_center=(690, 540))

        direction = AutoFightProcessor._edge_recenter_direction(
            [enemy, ally],
            x_bounds=(90, 630),
            y_bounds=(160, 1000),
        )

        self.assertEqual((1, -1), direction)


if __name__ == "__main__":
    unittest.main()
