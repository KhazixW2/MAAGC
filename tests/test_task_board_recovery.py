import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight.fight_utils import _accept_new_task  # noqa: E402


class _Job:
    def __init__(self, value=None):
        self.value = value
        self.succeeded = True

    def wait(self):
        return self

    def get(self):
        return self.value


class _Controller:
    def __init__(self):
        self.clicks = []

    def post_screencap(self):
        return _Job(object())

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return _Job()


class _Context:
    def __init__(self):
        self.tasker = SimpleNamespace(controller=_Controller())
        self.run_tasks = []

    def run_task(self, node):
        self.run_tasks.append(node)
        return SimpleNamespace(status=SimpleNamespace(succeeded=True))


def _run_accept_new_task(results):
    """用给定的 HUD 识别结果序列跑一次接取任务流程。"""
    context = _Context()
    recognizer = SimpleNamespace(
        recognize_and_get_best_task=lambda *_a, **_k: next(results)
    )

    with (
        patch("action.fight.fight_utils.TaskHudRecognizer", return_value=recognizer),
        patch("action.fight.fight_utils.time.sleep"),
    ):
        accepted = _accept_new_task(context)

    return context, accepted


class TaskBoardRecoveryTests(unittest.TestCase):
    def test_task_list_is_reset_to_top_before_hud_scan(self):
        task = SimpleNamespace(accept_button_box=[500, 700, 160, 80])

        context, accepted = _run_accept_new_task(iter([task]))

        self.assertTrue(accepted)
        self.assertEqual(["FindCityTask_SwipeUp"] * 5, context.run_tasks)
        self.assertEqual([(580, 740)], context.tasker.controller.clicks)

    def test_empty_pool_is_refreshed_once_then_rescanned(self):
        task = SimpleNamespace(accept_button_box=[500, 700, 160, 80])

        context, accepted = _run_accept_new_task(iter([None] * 6 + [task]))

        self.assertTrue(accepted)
        self.assertEqual(10, context.run_tasks.count("FindCityTask_SwipeUp"))
        self.assertEqual(5, context.run_tasks.count("FindCityTask_SwipeDown"))
        self.assertEqual(1, context.run_tasks.count("FindCityTask_Refresh"))
        self.assertEqual([(580, 740)], context.tasker.controller.clicks)


if __name__ == "__main__":
    unittest.main()
