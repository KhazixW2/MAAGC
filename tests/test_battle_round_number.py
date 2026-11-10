import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight.battle_action import BattleActions  # noqa: E402
from action.fight.battle_map import BattleSessionState  # noqa: E402


class _Context:
    """按调用顺序返回预设 OCR 文本的假上下文。"""

    def __init__(self, texts):
        self.tasker = SimpleNamespace(stopping=False)
        self._texts = texts
        self._index = 0

    def run_recognition(self, node, image):
        assert node == "FightRoundNumber", f"意外的识别节点: {node}"
        if self._index < len(self._texts):
            texts = self._texts[self._index]
        else:
            texts = self._texts[-1]
        self._index += 1
        results = [SimpleNamespace(text=text, score=1.0) for text in texts]
        return SimpleNamespace(hit=bool(results), all_results=results)


class RoundNumberTest(unittest.TestCase):
    def _probe(self):
        return BattleActions(
            BattleSessionState(key="round-number"), classifier=None
        )

    def test_read_round_number_from_labeled_text(self):
        """“回合:5”整句能读出回合数字。"""
        context = _Context([["回合:5"]])
        self.assertEqual(self._probe().read_round_number(context, None), 5)

    def test_read_round_number_from_split_boxes(self):
        """标签与数字被拆成两个文本框时也能读出数字。"""
        context = _Context([["回合", "12"]])
        self.assertEqual(self._probe().read_round_number(context, None), 12)

    def test_read_round_number_missing(self):
        """没有识别到数字时返回 None。"""
        context = _Context([["回合"]])
        self.assertIsNone(self._probe().read_round_number(context, None))

    def test_read_round_number_without_label(self):
        """没有“回合”标签时不会把别的数字当成回合数字。"""
        context = _Context([["120"]])
        self.assertIsNone(self._probe().read_round_number(context, None))

    def test_watch_round_requires_two_stable_frames(self):
        """连续两帧读到同一个新数字才确认回合推进。"""
        context = _Context(
            [["回合:3"], ["回合:3"], ["回合:4"], ["回合:4"], ["回合:5"]]
        )
        frames = iter(["f1", "f2", "f3", "f4"])
        results = list(
            self._probe().watch_round(context, "reference", frames)
        )
        self.assertEqual(
            [changed for _img, changed in results],
            [False, False, True, False],
        )

    def test_watch_round_ignores_same_round(self):
        """回合数字不变时不会误判为回合推进。"""
        context = _Context([["回合:3"], ["回合:3"], ["回合:3"]])
        frames = iter(["f1", "f2"])
        results = list(
            self._probe().watch_round(context, "reference", frames)
        )
        self.assertTrue(all(not changed for _img, changed in results))

    def test_watch_round_without_baseline_never_changes(self):
        """参考帧读不到数字时不做任何回合推进判定。"""
        context = _Context([[], ["回合:4"], ["回合:4"]])
        frames = iter(["f1", "f2"])
        results = list(
            self._probe().watch_round(context, "reference", frames)
        )
        self.assertTrue(all(not changed for _img, changed in results))


if __name__ == "__main__":
    unittest.main()
