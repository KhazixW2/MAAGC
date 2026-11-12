import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import action.fight.fight_utils as fight_utils  # noqa: E402


class FakeLayerContext:
    """模拟底部图层页签 + 图层选择弹窗的最小上下文。"""

    def __init__(self, layer_name="", options=None, bigmap=False):
        self.layer_name = layer_name
        self.options = list(options or [])
        self.bigmap = bigmap
        self.popup_opened = False
        self.tasks = []
        self.overrides = []
        self.tasker = SimpleNamespace(stopping=False)

    def run_recognition(self, node, img=None):
        if node == fight_utils.MAP_LAYER_TAB_NODE:
            results = (
                [SimpleNamespace(text=self.layer_name, box=[0, 0, 10, 10])]
                if self.layer_name
                else []
            )
            return SimpleNamespace(hit=bool(results), all_results=results)
        if node == fight_utils.MAP_LAYER_POPUP_OPTION_NODE:
            names = self.options if self.popup_opened else []
            results = [
                SimpleNamespace(text=name, box=[0, 0, 10, 10]) for name in names
            ]
            return SimpleNamespace(hit=bool(results), all_results=results)
        if node == fight_utils.MAP_LAYER_ARCHIPELAGO_NODE:
            return SimpleNamespace(hit=self.layer_name == "瑞格群岛", all_results=[])
        if node == "UI_MainWindows":
            return SimpleNamespace(hit=self.bigmap, all_results=[])
        return SimpleNamespace(hit=False, all_results=[])

    def run_task(self, node, pipeline_override=None):
        self.tasks.append(node)
        if pipeline_override:
            self.overrides.append(pipeline_override)
            expected = pipeline_override[node]["expected"]
            self.layer_name = expected[0]
        elif node == fight_utils.MAP_LAYER_POPUP_OPEN_NODE:
            self.popup_opened = True
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))


class MapLayerReadTests(unittest.TestCase):
    def setUp(self):
        fight_utils._continent_layer_name = None

    def test_read_map_layer_name_picks_longest_text(self):
        context = FakeLayerContext(layer_name="加尔提兰")
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            self.assertEqual("加尔提兰", fight_utils.read_map_layer_name(context))

    def test_read_map_layer_name_returns_none_without_text(self):
        context = FakeLayerContext()
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            self.assertIsNone(fight_utils.read_map_layer_name(context))

    def test_same_layer_matches_truncated_names(self):
        self.assertTrue(fight_utils._same_layer("加尔提", "加尔提兰"))
        self.assertTrue(fight_utils._same_layer("加尔提兰", "加尔提"))
        self.assertFalse(fight_utils._same_layer("瑞格群岛", "加尔提兰"))
        self.assertFalse(fight_utils._same_layer(None, "加尔提兰"))
        self.assertFalse(fight_utils._same_layer("加尔提兰", None))

    def test_on_archipelago_layer_detects_archipelago_tab(self):
        context = FakeLayerContext(layer_name="瑞格群岛")
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            self.assertTrue(fight_utils.on_archipelago_layer(context))

    def test_on_archipelago_layer_false_on_continent(self):
        context = FakeLayerContext(layer_name="加尔提兰")
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            self.assertFalse(fight_utils.on_archipelago_layer(context))


class ContinentMemoryTests(unittest.TestCase):
    def setUp(self):
        fight_utils._continent_layer_name = None

    def tearDown(self):
        fight_utils._continent_layer_name = None

    def test_remember_layer_on_bigmap(self):
        context = FakeLayerContext(layer_name="加尔提兰", bigmap=True)
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            fight_utils.remember_continent_layer(context)
        self.assertEqual("加尔提兰", fight_utils.get_continent_layer_name())

    def test_remember_layer_ignored_when_not_on_bigmap(self):
        # 战斗页的「战斗开始」按钮和页签同处一块区域，
        # 不在大地图上就不能把按钮文字当成图层名记下来。
        context = FakeLayerContext(layer_name="战斗升始", bigmap=False)
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            fight_utils.remember_continent_layer(context)
        self.assertIsNone(fight_utils.get_continent_layer_name())

    def test_remember_layer_ignored_on_archipelago(self):
        context = FakeLayerContext(layer_name="瑞格群岛", bigmap=True)
        with patch(
            "action.fight.fight_utils.screencap", return_value=object()
        ):
            fight_utils.remember_continent_layer(context)
        self.assertIsNone(fight_utils.get_continent_layer_name())


class SwitchMapLayerTests(unittest.TestCase):
    def setUp(self):
        fight_utils._continent_layer_name = None

    def tearDown(self):
        fight_utils._continent_layer_name = None

    def test_switch_to_named_layer_clicks_matched_option(self):
        context = FakeLayerContext(
            layer_name="瑞格群岛", options=["加尔提兰", "瑞格群岛"]
        )
        with (
            patch("action.fight.fight_utils.screencap", return_value=object()),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            switched = fight_utils.switch_map_layer(context, "加尔提兰")

        self.assertTrue(switched)
        self.assertIn(fight_utils.MAP_LAYER_POPUP_OPEN_NODE, context.tasks)
        self.assertEqual(
            {"expected": ["加尔提兰"]},
            context.overrides[0][fight_utils.MAP_LAYER_POPUP_OPTION_NODE],
        )

    def test_switch_falls_back_to_first_option(self):
        context = FakeLayerContext(
            layer_name="瑞格群岛", options=["加尔提兰", "瑞格群岛"]
        )
        with (
            patch("action.fight.fight_utils.screencap", return_value=object()),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            switched = fight_utils.switch_map_layer(
                context, None, fallback_to_first=True
            )

        self.assertTrue(switched)
        self.assertEqual(
            {"expected": ["加尔提兰"]},
            context.overrides[0][fight_utils.MAP_LAYER_POPUP_OPTION_NODE],
        )

    def test_switch_without_target_and_without_fallback_fails(self):
        context = FakeLayerContext(
            layer_name="瑞格群岛", options=["加尔提兰", "瑞格群岛"]
        )
        with (
            patch("action.fight.fight_utils.screencap", return_value=object()),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            switched = fight_utils.switch_map_layer(context)

        self.assertFalse(switched)
        self.assertEqual([], context.overrides)

    def test_switch_is_noop_when_already_on_target(self):
        context = FakeLayerContext(
            layer_name="加尔提兰", options=["加尔提兰", "瑞格群岛"]
        )
        with (
            patch("action.fight.fight_utils.screencap", return_value=object()),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            switched = fight_utils.switch_map_layer(context, "加尔提兰")

        self.assertTrue(switched)
        self.assertEqual([], context.tasks)


if __name__ == "__main__":
    unittest.main()
