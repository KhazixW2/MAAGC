import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.zshg import auto_fight_processor as afp
from action.zshg.auto_fight_processor import AutoFightProcessor
from action.zshg.battle_world_map import BattleSessionState
from action.fight import fight_processor as fp
from action.fight import fight_utils as fu
from utils import logger
from utils.logger import ProgressPrinter, _console_filter, _PROGRESS_FLAG


class FakeProgress:
    """替换真实进度打印器，只收集刷新过的文本。"""

    def __init__(self) -> None:
        self.messages = []
        self.committed = 0

    def update(self, message: str, depth: int = 2) -> None:
        self.messages.append(message)

    def commit(self) -> None:
        self.committed += 1


class ProgressPrinterTests(unittest.TestCase):
    def test_update_rewrites_the_same_line(self):
        stream = io.StringIO()
        printer = ProgressPrinter(stream=stream)

        printer.update("回合 1")
        printer.update("回合 2")

        output = stream.getvalue()
        self.assertEqual(output.count("\n"), 0)
        self.assertEqual(output.count("\r"), 2)
        self.assertTrue(output.startswith("\r"))
        self.assertIn("进度: 回合 2", output)

    def test_shorter_update_is_padded_to_erase_the_previous_text(self):
        stream = io.StringIO()
        printer = ProgressPrinter(stream=stream)

        printer.update("回合 10 · 攻击目标 (3, 5)")
        printer.update("回合 2")

        last = stream.getvalue().split("\r")[-1]
        self.assertEqual(last.rstrip(), "进度: 回合 2")
        self.assertGreater(len(last), len("进度: 回合 2"))

    def test_commit_ends_the_progress_line(self):
        stream = io.StringIO()
        printer = ProgressPrinter(stream=stream)

        printer.update("回合 1")
        printer.commit()

        self.assertTrue(stream.getvalue().endswith("\n"))

    def test_commit_without_progress_does_not_write_anything(self):
        stream = io.StringIO()
        printer = ProgressPrinter(stream=stream)

        printer.commit()

        self.assertEqual(stream.getvalue(), "")

    def test_record_callback_receives_every_update(self):
        records = []
        printer = ProgressPrinter(
            stream=io.StringIO(), record=lambda text, depth: records.append(text)
        )

        printer.update("回合 1")
        printer.update("回合 2")

        self.assertEqual(records, ["进度: 回合 1", "进度: 回合 2"])

    def test_record_callback_receives_the_requested_depth(self):
        depths = []
        printer = ProgressPrinter(
            stream=io.StringIO(),
            record=lambda text, depth: depths.append(depth),
        )

        printer.update("回合 1", depth=3)

        self.assertEqual(depths, [3])


class ConsoleFilterTests(unittest.TestCase):
    def test_progress_records_are_not_printed_to_the_console(self):
        record = {
            "extra": {_PROGRESS_FLAG: True},
            "level": SimpleNamespace(name="INFO"),
        }

        self.assertFalse(_console_filter(record))

    def test_normal_records_are_printed_with_a_short_level_name(self):
        record = {"extra": {}, "level": SimpleNamespace(name="INFO")}

        self.assertTrue(_console_filter(record))
        self.assertEqual(record["extra"]["level_short"], "info")


class BattleProgressTests(unittest.TestCase):
    def make_processor(self, session=None):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = session or BattleSessionState(key="test")
        return processor

    def test_round_advance_refreshes_the_progress_line(self):
        processor = self.make_processor()
        fake = FakeProgress()

        with patch.object(afp, "progress", fake):
            processor._record_round_advance("被动阶段结束回合")

        self.assertEqual(fake.messages, ["整场战斗 回合 1 · 被动阶段结束回合"])

    def test_progress_line_shows_the_latest_action_detail(self):
        processor = self.make_processor()
        fake = FakeProgress()

        with patch.object(afp, "progress", fake):
            processor._refresh_progress("攻击目标 (3, 5)")
            processor._refresh_progress()

        self.assertEqual(
            fake.messages,
            ["整场战斗 回合 0 · 攻击目标 (3, 5)", "整场战斗 回合 0"],
        )


class YearlyProgressTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeProgress()
        self.patchers = [
            patch.object(fu, "progress", self.fake),
            patch.object(fu, "yearly_progress", fu.YearlyProgress()),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    def test_progress_line_contains_month_city_and_stage(self):
        fu.yearly_progress.set_month(4, 12)
        fu.yearly_progress.city = "雄月城"

        fu.yearly_progress.update("寻找城市")

        self.assertEqual(self.fake.messages, ["第 4/12 月 · 雄月城 · 寻找城市"])

    def test_progress_line_omits_month_before_the_yearly_task_starts(self):
        fu.yearly_progress.update("事件检测")

        self.assertEqual(self.fake.messages, ["事件检测"])


class BattleResultLogTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.sink_id = logger.add(self._collect, level="INFO")

    def tearDown(self):
        logger.remove(self.sink_id)

    def _collect(self, message):
        self.messages.append(message.record["message"])

    def make_processor(self, session=None):
        processor = AutoFightProcessor.__new__(AutoFightProcessor)
        processor._session = session or BattleSessionState(key="test")
        return processor

    def _victory_logs(self):
        return [m for m in self.messages if "胜利结算页" in m]

    def test_victory_is_logged_once_per_battle(self):
        processor = self.make_processor()

        processor._report_battle_result(True)
        processor._report_battle_result(True)

        self.assertEqual(len(self._victory_logs()), 1)

    def test_failure_is_logged_once_per_battle(self):
        processor = self.make_processor()

        processor._report_battle_result(False)
        processor._report_battle_result(False)

        failures = [m for m in self.messages if "失败结算页" in m]
        self.assertEqual(len(failures), 1)

    def test_a_new_battle_logs_the_result_again(self):
        processor = self.make_processor()
        processor._report_battle_result(True)

        processor._session = BattleSessionState(key="next-battle")
        processor._report_battle_result(True)

        self.assertEqual(len(self._victory_logs()), 2)


if __name__ == "__main__":
    unittest.main()
