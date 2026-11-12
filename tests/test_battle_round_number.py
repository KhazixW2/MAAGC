import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight.battle_action import BattleActions  # noqa: E402
from action.fight.battle_map import BattleSessionState  # noqa: E402


def _frame(value: int = 0) -> np.ndarray:
    """构造一帧 720p 画面。"""
    return np.full((1280, 720, 3), value, dtype=np.uint8)


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


class _ButtonContext:
    """按脚本返回结束回合按钮可见性的假上下文。

    ``button_states`` 逐次给出「结束回合」按钮是否可见，
    ``round_texts`` 逐次给出 HUD 回合数字的 OCR 文本，
    ``popup`` 为 True 时模拟战斗弹窗盖住了按钮。
    """

    def __init__(self, button_states, round_texts=None, popup=False):
        self.tasker = SimpleNamespace(stopping=False)
        self._button_states = list(button_states)
        self._round_texts = list(round_texts or [])
        self._round_index = 0
        self._popup = popup

    def run_recognition(self, node, image):
        if node == "FightEndRound":
            if len(self._button_states) > 1:
                state = self._button_states.pop(0)
            else:
                state = self._button_states[0]
            return SimpleNamespace(hit=state, all_results=[])
        if node == "FightPopUp":
            return SimpleNamespace(hit=self._popup, all_results=[])
        if node == "FightRoundNumber":
            if self._round_index < len(self._round_texts):
                texts = self._round_texts[self._round_index]
            else:
                texts = []
            self._round_index += 1
            results = [SimpleNamespace(text=text, score=1.0) for text in texts]
            return SimpleNamespace(hit=bool(results), all_results=results)
        if node == "FightRoundNumberDigit":
            raise AssertionError("不应再使用单独的回合数字识别节点")
        raise AssertionError(f"意外的识别节点: {node}")


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

    def test_read_round_number_is_remembered_in_session(self):
        """读到的回合数字会记进会话，作为后续基线的兜底。"""
        session = BattleSessionState(key="round-number")
        probe = BattleActions(session, classifier=None)
        context = _Context([["回合:5"]])
        probe.read_round_number(context, None)
        self.assertEqual(session.last_round_number, 5)

    def test_read_round_number_from_split_digit_box(self):
        """“回合：”与数字分成两个文本框时也能读出数字。

        节点的 expected 是数组，两个框都会保留下来。
        """
        context = _Context([["回合：", "7"]])
        self.assertEqual(self._probe().read_round_number(context, None), 7)

    def test_read_round_number_digit_box_without_label_is_not_trusted(self):
        """只有数字、没有“回合”标签时不采信（不是战斗 HUD）。"""
        context = _Context([["7"]])
        self.assertIsNone(self._probe().read_round_number(context, None))


class WatchRoundNumberTest(unittest.TestCase):
    """逐帧观察回合数字变化（动作结果确认用）。"""

    def _probe(self):
        return BattleActions(
            BattleSessionState(key="watch-round"), classifier=None
        )

    def test_requires_two_stable_frames(self):
        """连续两帧读到同一个新数字才确认回合推进。"""
        context = _Context(
            [["回合:3"], ["回合:3"], ["回合:4"], ["回合:4"], ["回合:5"]]
        )
        frames = iter(["f1", "f2", "f3", "f4"])
        results = list(
            self._probe().watch_round_number(context, "reference", frames)
        )
        self.assertEqual(
            [changed for _img, changed in results],
            [False, False, True, False],
        )

    def test_same_round_never_changes(self):
        """回合数字不变时不会误判为回合推进。"""
        context = _Context([["回合:3"], ["回合:3"], ["回合:3"]])
        frames = iter(["f1", "f2"])
        results = list(
            self._probe().watch_round_number(context, "reference", frames)
        )
        self.assertTrue(all(not changed for _img, changed in results))

    def test_without_baseline_never_confirms(self):
        """读不到基线时不确认推进（不再用无效的像素差分兜底）。"""
        context = _Context([[], [], []])
        frames = iter(["f1", "f2"])
        results = list(
            self._probe().watch_round_number(context, "reference", frames)
        )
        self.assertTrue(all(not changed for _img, changed in results))


class RoundBaselineTest(unittest.TestCase):
    """回合基线的获取。"""

    def _probe(self, session=None):
        return BattleActions(
            session or BattleSessionState(key="round-baseline"),
            classifier=None,
        )

    def test_acquire_round_baseline_retries_until_readable(self):
        """首帧读不到数字时继续取帧重试。"""
        context = _ButtonContext([], round_texts=[[], ["回合:7"]])
        probe = self._probe()
        with patch(
            "action.fight.battle_action.screencap", return_value=_frame(0)
        ):
            baseline = probe.acquire_round_baseline(
                context, _frame(0), timeout=1.0
            )
        self.assertEqual(baseline, 7)

    def test_acquire_round_baseline_falls_back_to_session(self):
        """整段都读不到时退回会话里上一次读到的数字。"""
        session = BattleSessionState(key="round-baseline")
        session.last_round_number = 4
        context = _ButtonContext([], round_texts=[[], [], []])
        probe = self._probe(session)
        with patch(
            "action.fight.battle_action.screencap", return_value=_frame(0)
        ):
            baseline = probe.acquire_round_baseline(
                context, _frame(0), timeout=0.3
            )
        self.assertEqual(baseline, 4)


class RoundAdvanceTest(unittest.TestCase):
    """回合推进的确认。"""

    def _probe(self):
        return BattleActions(
            BattleSessionState(key="round-advance"), classifier=None
        )

    def _wait(self, probe, context, baseline=None, timeout=1.2):
        with patch(
            "action.fight.battle_action.screencap", return_value=_frame(0)
        ):
            return probe.wait_for_round_advance(
                context, baseline, timeout=timeout
            )

    def test_ocr_number_change_confirms_advance(self):
        """读到不同的回合数字（连续两帧）即确认回合推进。"""
        context = _ButtonContext(
            [True, True, True, True],
            round_texts=[["回合:3"], ["回合:4"], ["回合:4"]],
        )
        self.assertTrue(self._wait(self._probe(), context, baseline=3))

    def test_same_round_number_never_confirms(self):
        """回合数字没变时不确认推进（按钮也一直没消失）。"""
        context = _ButtonContext(
            [True, True, True, True, True],
            round_texts=[["回合:3"], ["回合:3"], ["回合:3"]],
        )
        self.assertFalse(self._wait(self._probe(), context, baseline=3))

    def test_button_cycle_confirms_advance(self):
        """按钮消失后重新出现即确认回合推进（不依赖 OCR）。"""
        context = _ButtonContext([False, False, True, True])
        self.assertTrue(self._wait(self._probe(), context, baseline=None))

    def test_button_cycle_never_confirms_when_button_stays(self):
        """按钮一直可见时不确认推进（点击可能没生效）。"""
        context = _ButtonContext([True, True, True, True, True, True])
        self.assertFalse(self._wait(self._probe(), context, baseline=None))

    def test_popup_does_not_count_as_enemy_turn(self):
        """弹窗盖住按钮时不算敌方回合，不会误判成回合推进。"""
        context = _ButtonContext([False, False, True, True], popup=True)
        self.assertFalse(self._wait(self._probe(), context, baseline=None))

    def test_button_hidden_means_our_turn_ended(self):
        """按钮在整个窗口内都不可见，说明我方回合已经结束。"""
        context = _ButtonContext([False])
        probe = self._probe()
        with patch(
            "action.fight.battle_action.screencap", return_value=_frame(0)
        ):
            self.assertTrue(
                probe.wait_for_end_round_button_hidden(context, timeout=0.6)
            )

    def test_button_visible_means_our_turn_continues(self):
        """按钮一直可见时我方回合还没结束。"""
        context = _ButtonContext([True])
        probe = self._probe()
        with patch(
            "action.fight.battle_action.screencap", return_value=_frame(0)
        ):
            self.assertFalse(
                probe.wait_for_end_round_button_hidden(context, timeout=0.6)
            )

    def test_button_flicker_does_not_end_our_turn(self):
        """按钮只是短暂消失（行动动画）时不算我方回合结束。"""
        context = _ButtonContext([False, True])
        probe = self._probe()
        with patch(
            "action.fight.battle_action.screencap", return_value=_frame(0)
        ):
            self.assertFalse(
                probe.wait_for_end_round_button_hidden(context, timeout=0.6)
            )


if __name__ == "__main__":
    unittest.main()
