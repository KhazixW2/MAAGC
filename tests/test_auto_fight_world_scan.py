import sys
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import cv2
import numpy as np
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight import battle_vision
from action.fight.battle_action import ActionKind, BattleActions
from action.fight.battle_classify import GridClassifier, ThreatClassifier
from action.fight.battle_geometry import is_adjacent_cell
from action.fight.battle_map import (
    BattleGrid,
    BattleRuntime,
    BattleSessionState,
    Cell,
    CellKind,
)
from action.fight.battle_mapping import BattleMapBuilder
from action.fight.battle_strategy import BattleStrategy


class RecordingClassifier:
    """只记录 ``scan_grid`` 收到的格子类型，不做真实识别。"""

    def __init__(self) -> None:
        self.cell_types = "not-called"

    def scan_grid(self, grid, context, img, cell_types=None):
        self.cell_types = cell_types
        return img

    def scan_ranges(self, grid, context, img=None):
        return img


class FakeActions:
    """动作层的最小替身：所有操作都成功。"""

    def __init__(self) -> None:
        self.pan_calls = []
        self.overlay_enabled = False

    def pan_camera(self, context, direction, fine=False) -> bool:
        self.pan_calls.append((direction, fine))
        return True

    def set_threat_overlay(self, context, enabled: bool) -> bool:
        self.overlay_enabled = enabled
        return True

    def wait_for_scene_settle(self, context, timeout: float) -> bool:
        return True

    def cancel_selection(self, context) -> bool:
        return True

    def dismiss_role_card(self, context) -> bool:
        return True

    def click_and_log(self, context, x, y, label) -> bool:
        return True


def make_mapper(session=None, classifier=None, threat_classifier=None):
    session = session or BattleSessionState(key="test")
    classifier = classifier or RecordingClassifier()
    return BattleMapBuilder(
        session,
        classifier,
        threat_classifier or ThreatClassifier(),
        FakeActions(),
        BattleRuntime(),
    )


class WorldViewScanTests(unittest.TestCase):
    def make_mapper(self):
        self.classifier = RecordingClassifier()
        self.mapper = make_mapper(classifier=self.classifier)
        self.mapper.merge_open_threat_overlay = lambda *args: []
        return self.mapper

    def test_threat_overlay_does_not_run_red_enemy_scan(self):
        mapper = self.make_mapper()

        mapper.scan_world_view(object(), object(), 1, overlay_open=True)

        self.assertEqual(
            set(self.classifier.cell_types),
            {CellKind.SELF, CellKind.FRIEND},
        )

    def test_normal_view_scans_all_unit_colours(self):
        mapper = self.make_mapper()

        mapper.scan_world_view(object(), object(), 1, overlay_open=False)

        self.assertIsNone(self.classifier.cell_types)

    def test_unit_marker_churn_does_not_reset_no_progress_watchdog(self):
        mapper = self.make_mapper()
        session = mapper.session
        session.no_progress_cycles = 5
        frames = iter((object(), object()))

        def scan_with_transient_enemy(
            _context, _img, round_seen, **_kwargs
        ):
            session.map.merge_threat_cells([(2, 2)], round_seen=round_seen)
            return BattleGrid()

        mapper.scan_world_view = scan_with_transient_enemy

        with (
            patch(
                "action.fight.battle_mapping.screencap",
                side_effect=lambda _context: next(frames),
            ),
            patch(
                "action.fight.battle_vision.camera_motion",
                return_value=(-120.0, 0.0, 1.0),
            ),
            patch(
                "action.fight.battle_vision.camera_shift_is_real",
                return_value=True,
            ),
        ):
            result = mapper.pan_world_once(
                object(), (0, 1), 3, overlay_open=True, phase="test"
            )

        self.assertIsNotNone(result)
        self.assertEqual(5, session.no_progress_cycles)


class ThreatCellTests(unittest.TestCase):
    def make_mapper(self):
        self.threat_classifier = SimpleNamespace(
            hidden_enemy_cell=lambda *_args: None,
            threat_origin=lambda *_args: None,
            visible_regions=lambda *_args: [],
            largest_region_key=ThreatClassifier.largest_region_key,
        )
        self.mapper = make_mapper(
            session=BattleSessionState(key="threat-test"),
            threat_classifier=self.threat_classifier,
        )
        return self.mapper

    @staticmethod
    def make_grid_with_ally() -> BattleGrid:
        grid = BattleGrid()
        ally = grid.get_cell(5, 1)
        ally.kind = CellKind.SELF
        ally.unit_center = (180, 660)
        return grid

    def test_region_or_continuous_origin_is_direction_only(self):
        mapper = self.make_mapper()
        self.threat_classifier.threat_origin = lambda *_args: (600, 180, 0.8)
        context = SimpleNamespace(
            run_recognition=lambda *_: SimpleNamespace(
                hit=True,
                filtered_results=[SimpleNamespace(box=(240, 120, 360, 480))],
            )
        )

        points = mapper.merge_open_threat_overlay(
            context,
            np.zeros((1280, 720, 3), dtype=np.uint8),
            self.make_grid_with_ally(),
            3,
        )

        self.assertEqual([], points)
        self.assertEqual((1, -1), mapper.runtime.coarse_threat_direction)
        self.assertEqual(
            [], mapper.session.map.unit_points(CellKind.ENEMY)
        )

    def test_only_exact_jagged_cell_is_persisted(self):
        mapper = self.make_mapper()
        self.threat_classifier.hidden_enemy_cell = lambda *_args: (2, 3, 1.2)
        context = SimpleNamespace(run_recognition=lambda *_: None)

        points = mapper.merge_open_threat_overlay(
            context,
            np.zeros((1280, 720, 3), dtype=np.uint8),
            self.make_grid_with_ally(),
            4,
        )

        self.assertEqual([(2, 3)], points)
        self.assertEqual(
            [(2, 3)], mapper.session.map.unit_points(CellKind.ENEMY)
        )

    def test_jagged_hollow_cell_is_selected_from_red_grid(self):
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

        result = ThreatClassifier().hidden_enemy_cell(image, [])

        self.assertIsNotNone(result)
        self.assertEqual((row, col), result[:2])


class EdgeRecenterTests(unittest.TestCase):
    def test_corner_unit_uses_diagonal_direction(self):
        unit = Cell(0, 5, unit_center=(690, 60))

        direction = BattleMapBuilder.edge_recenter_direction(
            unit,
            x_bounds=(90, 630),
            y_bounds=(160, 1000),
        )

        self.assertEqual((1, -1), direction)

    def test_hidden_bottom_right_target_uses_diagonal_nudge(self):
        mapper = make_mapper(session=BattleSessionState(key="edge-hidden"))
        grid = BattleGrid()
        ally = grid.get_cell(5, 3)
        ally.kind = CellKind.SELF
        ally.unit_center = (420, 660)
        calls = []

        def fake_pan(_context, direction, _round_seen, **_kwargs):
            calls.append(direction)
            return False, None, None

        mapper.pan_world_once = fake_pan

        mapper.recenter_edge_allies(
            object(), grid, 1, known_target=(9, 5)
        )

        self.assertEqual([(1, 1)], calls)


class FocusKnownAllyTests(unittest.TestCase):
    def test_actionable_edge_ally_is_kept_when_camera_hits_boundary(self):
        mapper = make_mapper(
            session=BattleSessionState(key="focus-boundary")
        )
        image = object()

        grid = BattleGrid()
        ally = grid.get_cell(9, 3)
        ally.kind = CellKind.SELF
        mapper.scan_world_view = lambda *_args, **_kwargs: grid
        mapper.pan_world_once = (
            lambda *_args, **_kwargs: (False, None, image)
        )

        with patch(
            "action.fight.battle_mapping.screencap", return_value=image
        ):
            focused = mapper.focus_known_ally(object(), round_seen=4)

        self.assertIsNotNone(focused)
        self.assertIs(grid, focused[0])
        self.assertIs(image, focused[1])


class CoarsePursuitLoopTests(unittest.TestCase):
    def make_strategy(self):
        session = BattleSessionState(key="coarse-loop")
        runtime = BattleRuntime()
        strategy = BattleStrategy(
            session,
            BattleActions(session, GridClassifier()),
            GridClassifier(),
            runtime,
        )
        return strategy, session, runtime

    def test_only_immediate_reverse_waits_one_turn_and_clears_direction(self):
        strategy, session, runtime = self.make_strategy()
        runtime.last_confirmed_move_direction = (-1, 1)
        session.last_move_direction = (1, -1)
        ally = Cell(5, 1)
        reverse = Cell(4, 2)

        candidates = strategy.coarse_move_candidates([reverse], ally)

        self.assertEqual([], candidates)
        self.assertIsNone(runtime.last_confirmed_move_direction)
        self.assertIsNone(session.last_move_direction)

    def test_recent_coarse_destination_is_deprioritized(self):
        strategy, session, runtime = self.make_strategy()
        ally = Cell(5, 1)
        recent = Cell(4, 2)
        fresh = Cell(6, 2)
        session.record_move_point((4, 2))

        candidates = strategy.coarse_move_candidates([recent, fresh], ally)

        self.assertEqual([fresh], candidates)


class OccludedAttackTargetTests(unittest.TestCase):
    def test_adjacent_red_bar_is_promoted_when_attack_layer_exists(self):
        classifier = GridClassifier()
        grid = BattleGrid()
        ally = Cell(1, 1, kind=CellKind.SELF, unit_center=(180, 200))
        enemy = grid.get_cell(1, 2)
        enemy.kind = CellKind.ENEMY
        enemy.unit_center = (300, 200)
        grid.get_cell(3, 3).is_attackable = True

        promoted = classifier.promote_occluded_adjacent_targets(grid, ally)

        self.assertEqual([enemy], promoted)
        self.assertEqual((300, 160), enemy.attack_center)

    def test_adjacent_red_bar_is_not_promoted_without_attack_layer(self):
        classifier = GridClassifier()
        grid = BattleGrid()
        ally = Cell(1, 1, kind=CellKind.SELF, unit_center=(180, 200))
        enemy = grid.get_cell(1, 2)
        enemy.kind = CellKind.ENEMY
        enemy.unit_center = (300, 200)

        promoted = classifier.promote_occluded_adjacent_targets(grid, ally)

        self.assertEqual([], promoted)
        self.assertFalse(enemy.is_attackable)

    def test_adjacent_hidden_enemy_is_double_clicked_without_yellow_marker(self):
        session = BattleSessionState(key="hidden-adjacent")
        classifier = GridClassifier()
        actions = BattleActions(session, classifier)
        strategy = BattleStrategy(
            session, actions, classifier, BattleRuntime()
        )
        clicked = []
        actions.click_cell = (
            lambda _context, cell, kind, label: clicked.append(
                (cell.row, cell.col, kind, label)
            )
            or True
        )
        actions.verify_action_result = lambda *_: (True, False)

        grid = BattleGrid()
        ally = grid.get_cell(5, 3)
        ally.kind = CellKind.SELF

        result = strategy.decide_and_act(
            object(),
            grid,
            ally,
            object(),
            object(),
            None,
            (5, 2),
        )

        self.assertEqual((True, None, False), result)
        self.assertEqual(
            [(5, 2, ActionKind.ATTACK, "相邻威胁中心攻击")], clicked
        )

    def test_hidden_enemy_attack_requires_eight_direction_adjacency(self):
        ally = Cell(5, 3)

        self.assertTrue(
            is_adjacent_cell(ally, Cell(4, 2))
        )
        self.assertFalse(
            is_adjacent_cell(ally, Cell(5, 3))
        )
        self.assertFalse(
            is_adjacent_cell(ally, Cell(5, 1))
        )


class ActionKindTests(unittest.TestCase):
    """动作标签 → 点击方式 / 结果校验语义的唯一约定。"""

    def test_action_kind_replaces_label_sniffing(self):
        self.assertEqual(ActionKind.ATTACK.value, "attack")
        self.assertEqual(ActionKind.MOVE.value, "move")

    def test_threat_center_plan_is_an_attack(self):
        session = BattleSessionState(key="threat-center")
        classifier = GridClassifier()
        strategy = BattleStrategy(
            session,
            BattleActions(session, classifier),
            classifier,
            BattleRuntime(),
        )
        grid = BattleGrid()
        ally = grid.get_cell(5, 3)
        ally.kind = CellKind.SELF
        hidden = grid.get_cell(5, 2)
        hidden.is_attackable = True

        plan = strategy.pursue_remote_direction(
            grid, ally, [Cell(5, 4)], (0, 1), (5, 2)
        )

        self.assertIsNotNone(plan)
        self.assertEqual(ActionKind.ATTACK, plan.kind)
        self.assertEqual("威胁中心攻击", plan.label)

    def test_click_cell_uses_the_click_point_matching_the_kind(self):
        actions = BattleActions(
            BattleSessionState(key="click"), GridClassifier()
        )
        clicked = []
        actions.click_and_log = (
            lambda _context, x, y, _label: clicked.append((x, y)) or True
        )
        cell = Cell(2, 3, move_center=(300, 400), attack_center=(310, 410))

        with patch("action.fight.battle_action.time.sleep"):
            actions.click_cell(object(), cell, ActionKind.MOVE, "移动")
            actions.click_cell(object(), cell, ActionKind.ATTACK, "攻击")

        self.assertEqual(
            [(300, 400), (300, 400), (310, 410), (310, 410)], clicked
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

        direction = BattleMapBuilder.edge_recenter_direction(
            [enemy, ally],
            x_bounds=(90, 630),
            y_bounds=(160, 1000),
        )

        self.assertEqual((1, -1), direction)


if __name__ == "__main__":
    unittest.main()
