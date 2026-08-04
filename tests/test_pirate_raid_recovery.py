import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight.pirate_raid_processor import (  # noqa: E402
    ACTIVE_BATTLE_NODE,
    ARCHIPELAGO_READY_NODE,
    BATTLE_READY_NODE,
    PirateRaidProcessor,
)
from action.fight.fight_processor import _recover_yearly_to_bigmap  # noqa: E402


class FakeContext:
    def __init__(self, initial_img, resumed_img):
        self.initial_img = initial_img
        self.resumed_img = resumed_img
        self.run_tasks = []
        self.tasker = SimpleNamespace(stopping=False)

    def run_recognition(self, node, img):
        hit = img is self.resumed_img and node == BATTLE_READY_NODE
        return SimpleNamespace(hit=hit)

    def run_task(self, node):
        self.run_tasks.append(node)
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))


class PirateRaidPipelineTests(unittest.TestCase):
    def test_sailing_confirm_roi_contains_observed_button(self):
        pipeline_path = (
            Path(__file__).resolve().parents[1]
            / "assets/resource/base/pipeline/event_utils.json"
        )
        data = json.loads(pipeline_path.read_text(encoding="utf-8"))

        confirm = data["Event_PirateRaid_SailingConfirm"]
        x, y, width, height = confirm["roi"]
        self.assertLessEqual(x, 473)
        self.assertLessEqual(y, 686)
        self.assertGreaterEqual(x + width, 607)
        self.assertGreaterEqual(y + height, 742)
        self.assertEqual("Click", confirm["action"])

        victory = data["Event_PirateRaid_VictoryConfirm"]
        vx, vy, vwidth, vheight = victory["roi"]
        self.assertLessEqual(vx, 277)
        self.assertLessEqual(vy, 967)
        self.assertGreaterEqual(vx + vwidth, 443)
        self.assertGreaterEqual(vy + vheight, 1032)
        self.assertEqual("Click", victory["action"])

        wait_node = data["Event_PirateRaid_SailingDialog"]
        self.assertEqual(["瑞格群岛"], wait_node["expected"])
        self.assertGreater(wait_node["max_hit"], 0)
        self.assertLessEqual(wait_node["max_hit"], 6)

        travel_dialog = data["Event_PirateRaid_TravelDialog"]
        self.assertEqual(["瑞格群岛"], travel_dialog["expected"])
        tx, ty, twidth, theight = travel_dialog["roi"]
        self.assertLessEqual(tx, 276)
        self.assertLessEqual(ty, 586)
        self.assertGreaterEqual(tx + twidth, 446)
        self.assertGreaterEqual(ty + theight, 616)

        # Python PirateRaidProcessor 是唯一编排者；这些点击节点必须保持原子性，
        # 否则第一次 run_task 会沿 next 跑完整场战斗，处理器随后又会重复点击。
        atomic_nodes = [
            "Event_PirateRaid_ClickBanner",
            "Event_PirateRaid_EnterBattle",
            "Event_PirateRaid_SailingConfirm",
            "Event_PirateRaid_BattleStart",
        ]
        for node in atomic_nodes:
            self.assertNotIn("next", data[node], node)


class PirateRaidRecoveryTests(unittest.TestCase):
    def test_visible_travel_dialog_resumes_at_battle_page(self):
        initial_img = object()
        resumed_img = object()
        context = FakeContext(initial_img, resumed_img)
        processor = PirateRaidProcessor()

        with (
            patch(
                "action.fight.pirate_raid_processor._screencap",
                side_effect=[initial_img, resumed_img],
            ),
            patch(
                "action.fight.pirate_raid_processor._dialog_visible",
                return_value=True,
            ),
            patch(
                "action.fight.pirate_raid_processor._handle_visible_travel_dialog",
                return_value=True,
            ) as handle_dialog,
            patch(
                "action.fight.pirate_raid_processor._wait_for_layer_ready",
                return_value=True,
            ),
            patch(
                "action.fight.pirate_raid_processor._tap",
                return_value=True,
            ) as tap,
            patch(
                "action.fight.pirate_raid_processor.fight_utils._task_succeeded",
                return_value=True,
            ),
            patch.object(processor, "_recover_to_continent", return_value=True),
        ):
            result = processor.run(context, SimpleNamespace())

        self.assertTrue(result.success)
        handle_dialog.assert_called_once_with(context, initial_img)
        tapped_nodes = [call.args[1] for call in tap.call_args_list]
        self.assertEqual(
            ["Event_PirateRaid_BattleStart", "Event_PirateRaid_VictoryConfirm"],
            tapped_nodes,
        )
        self.assertNotIn("Event_PirateRaid_ClickBanner", tapped_nodes)
        self.assertNotIn("Event_PirateRaid_EnterBattle", tapped_nodes)
        self.assertEqual(["AutoFight_Start"], context.run_tasks)

    def test_yearly_recovery_dispatches_pirate_battle_page_first(self):
        battle_page = object()
        bigmap = object()
        context = FakeContext(battle_page, bigmap)

        def recognize(node, img):
            hit = (
                img is battle_page and node == BATTLE_READY_NODE
            ) or (
                img is bigmap and node == "UI_MainWindows"
            )
            return SimpleNamespace(hit=hit)

        context.run_recognition = recognize

        with (
            patch(
                "action.fight.fight_processor.fight_utils._screencap",
                side_effect=[battle_page, bigmap],
            ),
            patch(
                "action.fight.fight_processor.fight_utils._task_succeeded",
                return_value=True,
            ),
            patch("action.fight.fight_processor.time.sleep"),
        ):
            recovered = _recover_yearly_to_bigmap(context, max_steps=2)

        self.assertTrue(recovered)
        self.assertEqual(["Event_PirateRaid_Dispatch"], context.run_tasks)


if __name__ == "__main__":
    unittest.main()
