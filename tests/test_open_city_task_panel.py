import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight.fight_utils import open_city_task_panel


class AsyncValue:
    def __init__(self, value=None, *, succeeded=True):
        self.value = value
        self.succeeded = succeeded

    def wait(self):
        return self

    def get(self):
        return self.value


class FakeController:
    def __init__(self, context, advance_on_click=True):
        self.context = context
        self.advance_on_click = advance_on_click
        self.clicks = []

    def post_screencap(self):
        return AsyncValue(object())

    def post_click(self, x, y):
        self.clicks.append((x, y))
        if self.advance_on_click:
            self.context.state = "city"
        return AsyncValue(succeeded=True)


class FakeContext:
    def __init__(self, state="bigmap", advance_on_click=True):
        self.state = state
        self.tasker = SimpleNamespace()
        self.tasker.controller = FakeController(self, advance_on_click)

    def run_recognition(self, node, _img):
        hit = False
        best = None
        if self.state == "task_panel" and node == "InTaskPannel":
            hit = True
        elif self.state == "free_day" and node == "FreeDayGoButton":
            hit = True
            best = SimpleNamespace(box=(482, 550, 132, 55))
            return SimpleNamespace(
                hit=True,
                best_result=best,
                filtered_results=[best],
            )
        elif self.state in {"bigmap", "arrival_banner", "free_day"} and node == "EnterCity":
            hit = True
            best = SimpleNamespace(box=(320, 561, 80, 33))
        elif self.state == "city" and node == "FindCityTask_OCR":
            hit = True
        return SimpleNamespace(
            hit=hit,
            best_result=best,
            filtered_results=[] if not hit else ([best] if best else []),
        )

    def run_task(self, node):
        if node == "EnterCity":
            self.state = "free_day"
        elif node == "FindCityTask_OCR":
            self.state = "task_panel"
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))


class OpenCityTaskPanelTests(unittest.TestCase):
    @patch("action.fight.fight_utils.time.sleep", return_value=None)
    def test_bigmap_free_day_city_task_panel(self, _sleep):
        context = FakeContext(state="arrival_banner")

        self.assertTrue(open_city_task_panel(context))
        self.assertEqual(context.state, "task_panel")
        self.assertEqual(context.tasker.controller.clicks, [(548, 577)])

    @patch("action.fight.fight_utils.time.sleep", return_value=None)
    def test_free_day_no_progress_stops_after_three_observations(self, _sleep):
        context = FakeContext(state="free_day", advance_on_click=False)

        self.assertFalse(open_city_task_panel(context))
        self.assertEqual(len(context.tasker.controller.clicks), 2)


if __name__ == "__main__":
    unittest.main()
