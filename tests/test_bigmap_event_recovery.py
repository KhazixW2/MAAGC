import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight import fight_utils  # noqa: E402
from action.fight.fight_utils import ensure_at_bigmap  # noqa: E402

PIPELINE_DIR = (
    Path(__file__).resolve().parents[1] / "assets/resource/base/pipeline"
)

# 实测（2026-11-10 debug/maafw.log）：「感a力b科虎佩公向肖恩表白失败！」横幅
# 在 720x1280 下的 OCR 文本框。ROI 必须完整包住它，否则检测器会截断首字，
# 把整行读成「力b科度佩公向省恩表自失收！」。
CONFESSION_BANNER_BOX = (177, 235, 357, 25)
# 同一份日志里 FightFail 命中的那行文字。
BANNER_TEXT = "感a力b科虎佩公向肖恩表白失败！"


def _load_pipeline(name: str) -> dict:
    return json.loads((PIPELINE_DIR / name).read_text(encoding="utf-8"))


def _expected_list(node: dict) -> list:
    expected = node.get("expected", [])
    return [expected] if isinstance(expected, str) else list(expected)


class _Context:
    """最小可用的 Maa 上下文：大地图在第 N 次识别之后才出现。"""

    def __init__(self, bigmap_after: int = 0):
        self.tasker = SimpleNamespace(stopping=False)
        self.run_tasks = []
        self._bigmap_after = bigmap_after
        self._bigmap_calls = 0

    def run_recognition(self, node, img=None):
        if node == "UI_MainWindows":
            self._bigmap_calls += 1
            return SimpleNamespace(hit=self._bigmap_calls > self._bigmap_after)
        return SimpleNamespace(hit=False)

    def run_task(self, node):
        self.run_tasks.append(node)
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))


class EnsureAtBigmapEventTests(unittest.TestCase):
    """回到大地图时顺手关掉挡住视线的随机事件弹窗。"""

    def _run(self, context, event_names, max_attempts=8):
        handled = []

        def fake_detect(_context, _img):
            handled.append(event_names[0] if event_names else None)
            return event_names.pop(0) if event_names else None

        with (
            patch(
                "action.fight.fight_utils.screencap", return_value=object()
            ),
            patch(
                "action.fight.fight_processor.detect_and_manage_event",
                side_effect=fake_detect,
            ),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            reached = ensure_at_bigmap(context, max_attempts=max_attempts)

        return reached, handled

    def test_pending_event_is_closed_before_any_blind_click(self):
        context = _Context(bigmap_after=1)

        reached, handled = self._run(context, ["confession_fail"])

        self.assertTrue(reached)
        self.assertEqual(["confession_fail"], handled)
        self.assertEqual([], context.run_tasks)

    def test_same_event_is_not_retried_forever(self):
        context = _Context(bigmap_after=99)

        reached, handled = self._run(context, ["confession_fail"] * 10)

        self.assertFalse(reached)
        self.assertEqual(
            fight_utils.MAX_EVENT_DISMISS_REPEATS + 1, len(handled)
        )
        self.assertTrue(context.run_tasks)

    def test_unknown_screen_falls_back_to_generic_recovery(self):
        context = _Context(bigmap_after=99)

        reached, handled = self._run(context, [None] * 10, max_attempts=4)

        self.assertFalse(reached)
        self.assertEqual([None] * 4, handled)
        self.assertTrue(context.run_tasks)


class FightFailRecognitionTests(unittest.TestCase):
    """失败结算页必须靠「战斗失败」判定，不能靠裸的「失败」。"""

    def setUp(self):
        self.node = _load_pipeline("city.json")["FightFail"]

    def test_expected_matches_the_real_fail_banner(self):
        self.assertIn("战斗失败", _expected_list(self.node))

    def test_expected_cannot_match_the_confession_banner(self):
        for expected in _expected_list(self.node):
            self.assertNotIn(expected, BANNER_TEXT)


class ConfessionEventRoiTests(unittest.TestCase):
    """「表白失败」事件节点的 ROI 必须完整包住横幅文本。"""

    def setUp(self):
        self.node = _load_pipeline("event_utils.json")["Event_ConfessionFail"]

    def test_roi_contains_the_measured_banner_text(self):
        roi_x, roi_y, roi_w, roi_h = self.node["roi"]
        text_x, text_y, text_w, text_h = CONFESSION_BANNER_BOX

        self.assertLessEqual(roi_x, text_x - 8)
        self.assertLessEqual(roi_y, text_y - 8)
        self.assertGreaterEqual(roi_x + roi_w, text_x + text_w + 8)
        self.assertGreaterEqual(roi_y + roi_h, text_y + text_h + 8)

    def test_success_and_fail_events_use_different_expected_text(self):
        pipeline = _load_pipeline("event_utils.json")
        success = _expected_list(pipeline["Event_ConfessionSuccess"])
        failure = _expected_list(pipeline["Event_ConfessionFail"])

        self.assertEqual(set(success) & set(failure), set())


if __name__ == "__main__":
    unittest.main()
