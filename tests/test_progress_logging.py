import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight import battle_action
from action.fight.battle_action import BattleActions
from action.fight.battle_map import BattleSessionState
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
    def make_actions(self, session=None):
        session = session or BattleSessionState(key="test")
        return BattleActions(session, classifier=None)

    def test_round_advance_refreshes_the_progress_line(self):
        actions = self.make_actions()
        fake = FakeProgress()

        with patch.object(battle_action, "progress", fake):
            actions.record_round_advance("被动阶段结束回合")

        self.assertEqual(fake.messages, ["整场战斗 回合 1 · 被动阶段结束回合"])

    def test_progress_line_shows_the_latest_action_detail(self):
        actions = self.make_actions()
        fake = FakeProgress()

        with patch.object(battle_action, "progress", fake):
            actions.refresh_progress("攻击目标 (3, 5)")
            actions.refresh_progress()

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


class ItemGainBannerTests(unittest.TestCase):
    """「获得 xx」横幅会盖住城市名，必须先关掉它再点城市。"""

    class _Context:
        def __init__(self, hits):
            self.hits = list(hits)
            self.recognitions = []
            self.tasks = []

        def run_recognition(self, node, img=None):
            self.recognitions.append(node)
            hit = self.hits.pop(0) if self.hits else False
            return SimpleNamespace(hit=hit)

        def run_task(self, node):
            self.tasks.append(node)
            return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    def test_returns_immediately_when_no_banner(self):
        context = self._Context([False])

        with patch.object(fu, "screencap", lambda ctx: object()):
            gone = fu.dismiss_item_gain_banner(context)

        self.assertTrue(gone)
        self.assertEqual([], context.tasks)
        self.assertEqual([fu.ITEM_GAIN_BANNER_NODE], context.recognitions)

    def test_swipes_to_close_the_banner(self):
        context = self._Context([True, False])

        with (
            patch.object(fu, "screencap", lambda ctx: object()),
            patch.object(fu.time, "sleep"),
        ):
            gone = fu.dismiss_item_gain_banner(context)

        self.assertTrue(gone)
        self.assertEqual([fu.ITEM_GAIN_BANNER_DISMISS_NODE], context.tasks)

    def test_gives_up_after_reaching_the_retry_limit(self):
        context = self._Context([True] * 50)

        with (
            patch.object(fu, "screencap", lambda ctx: object()),
            patch.object(fu.time, "sleep"),
        ):
            gone = fu.dismiss_item_gain_banner(context, max_tries=2)

        self.assertFalse(gone)
        self.assertEqual(2, len(context.tasks))

    def test_city_click_closes_the_banner_first(self):
        """点城市之前必须先关掉横幅，否则点击会被吞掉、场景不变。"""
        order = []

        class _CityContext(self._Context):
            def run_recognition(self, node, img=None):
                self.recognitions.append(node)
                return SimpleNamespace(hit=node == "EnterCity", best_result=None)

            def run_task(self, node):
                order.append(node)
                return SimpleNamespace(status=SimpleNamespace(succeeded=True))

        context = _CityContext([])
        real_dismiss = fu.dismiss_item_gain_banner

        def recording_dismiss(ctx, max_tries=3):
            order.append("dismiss")
            return real_dismiss(ctx, max_tries)

        with (
            patch.object(fu, "screencap", lambda ctx: object()),
            patch.object(fu.time, "sleep"),
            patch.object(fu, "dismiss_item_gain_banner", recording_dismiss),
        ):
            fu.open_city_task_panel(context, max_steps=2)

        self.assertEqual(["dismiss", "EnterCity"], order[:2])


class ItemGainBannerEventTests(unittest.TestCase):
    """掉落横幅在随机事件流程里被清掉，不再每回合单独刷一条日志。"""

    class _Context:
        def __init__(self, banner_hit=True, bigmap_hit=True):
            self.banner_hit = banner_hit
            self.bigmap_hit = bigmap_hit
            self.tasks = []

        def run_recognition(self, node, img=None):
            if node == fu.ITEM_GAIN_BANNER_NODE:
                return SimpleNamespace(hit=self.banner_hit)
            if node == "UI_MainWindows":
                return SimpleNamespace(hit=self.bigmap_hit)
            return SimpleNamespace(hit=False)

        def run_task(self, node):
            self.tasks.append(node)
            if node == fu.ITEM_GAIN_BANNER_DISMISS_NODE:
                self.banner_hit = False
            return SimpleNamespace(status=SimpleNamespace(succeeded=True))

    def test_banner_is_closed_in_the_event_flow(self):
        context = self._Context()

        with patch.object(fu, "screencap", lambda ctx: object()):
            event = fp.detect_and_manage_event(context, object())

        self.assertEqual(fu.ITEM_GAIN_BANNER_EVENT, event)
        self.assertEqual([fu.ITEM_GAIN_BANNER_DISMISS_NODE], context.tasks)

    def test_banner_is_not_touched_outside_the_big_map(self):
        context = self._Context(bigmap_hit=False)

        with patch.object(fu, "screencap", lambda ctx: object()):
            event = fp.detect_and_manage_event(context, object())

        self.assertIsNone(event)
        self.assertEqual([], context.tasks)

    def test_banner_event_is_not_logged_as_info(self):
        records = []
        sink_id = logger.add(
            lambda message: records.append(
                (message.record["level"].name, message.record["message"])
            ),
            level="INFO",
        )
        context = self._Context()

        try:
            with (
                patch.object(fp, "screencap", lambda ctx: object()),
                patch.object(fp.time, "sleep"),
                patch.object(fu, "screencap", lambda ctx: object()),
            ):
                handled = fp.preprocess_events(context)
        finally:
            logger.remove(sink_id)

        self.assertTrue(handled)
        infos = [msg for level, msg in records if level == "INFO"]
        self.assertEqual(
            [], [msg for msg in infos if fu.ITEM_GAIN_BANNER_EVENT in msg]
        )


class BattleResultLogTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.sink_id = logger.add(self._collect, level="INFO")

    def tearDown(self):
        logger.remove(self.sink_id)

    def _collect(self, message):
        self.messages.append(message.record["message"])

    def make_actions(self, session=None):
        session = session or BattleSessionState(key="test")
        return BattleActions(session, classifier=None)

    def _victory_logs(self):
        return [m for m in self.messages if "胜利结算页" in m]

    def test_victory_is_logged_once_per_battle(self):
        actions = self.make_actions()

        actions.report_battle_result(True)
        actions.report_battle_result(True)

        self.assertEqual(len(self._victory_logs()), 1)

    def test_failure_is_logged_once_per_battle(self):
        actions = self.make_actions()

        actions.report_battle_result(False)
        actions.report_battle_result(False)

        failures = [m for m in self.messages if "失败结算页" in m]
        self.assertEqual(len(failures), 1)

    def test_a_new_battle_logs_the_result_again(self):
        actions = self.make_actions()
        actions.report_battle_result(True)

        actions = self.make_actions(
            BattleSessionState(key="next-battle")
        )
        actions.report_battle_result(True)

        self.assertEqual(len(self._victory_logs()), 2)


if __name__ == "__main__":
    unittest.main()
