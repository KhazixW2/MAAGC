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


class TaskBoardRecoveryTests(unittest.TestCase):
    def test_task_list_is_reset_to_top_before_hud_scan(self):
        context = _Context()
        task = SimpleNamespace(accept_button_box=[500, 700, 160, 80])
        recognizer = SimpleNamespace(recognize_and_get_best_task=lambda *_a, **_k: task)

        with (
            patch("action.fight.fight_utils.TaskHudRecognizer", return_value=recognizer),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            accepted = _accept_new_task(context)

        self.assertTrue(accepted)
        self.assertEqual(["FindCityTask_SwipeUp"] * 5, context.run_tasks)
        self.assertEqual([(580, 740)], context.tasker.controller.clicks)

    def test_scan_hits_task_without_crystal_refresh(self):
        """池内扫描命中任务时接取，且**全程不触发 FindCityTask_Refresh**。

        行为契约：5 次 SwipeUp 回到顶部 → 逐屏扫描（每屏未命中则 SwipeDown）
        → 命中任务即接取。该节点会消耗水晶（付费资源），年度流程禁止任何
        水晶/钻石消耗，故已替换为 SkipMonth_Rest（休息跳月）。
        """
        context = _Context()
        task = SimpleNamespace(accept_button_box=[500, 700, 160, 80])
        results = iter([None, None, task])
        recognizer = SimpleNamespace(
            recognize_and_get_best_task=lambda *_a, **_k: next(results)
        )

        with (
            patch("action.fight.fight_utils.TaskHudRecognizer", return_value=recognizer),
            patch("action.fight.fight_utils.time.sleep"),
        ):
            accepted = _accept_new_task(context)

        self.assertTrue(accepted)
        self.assertEqual(5, context.run_tasks.count("FindCityTask_SwipeUp"))
        self.assertEqual(2, context.run_tasks.count("FindCityTask_SwipeDown"))
        self.assertEqual(0, context.run_tasks.count("FindCityTask_Refresh"))
        self.assertEqual([(580, 740)], context.tasker.controller.clicks)

    def test_empty_pool_falls_back_to_rest_skip_month(self):
        """扫完整个任务池仍无任务时，走「休息跳月」而不是刷新任务池。

        旧行为：直接返回 False，导致一个月接不到任务就把整条年度任务归零。
        新行为：返回大地图 → 点沙漏 → 确认，让游戏时间继续推进。
        """
        context = _Context()
        recognizer = SimpleNamespace(recognize_and_get_best_task=lambda *_a, **_k: None)

        with (
            patch("action.fight.fight_utils.TaskHudRecognizer", return_value=recognizer),
            patch("action.fight.fight_utils.time.sleep"),
            patch(
                "action.fight.fight_utils._rest_to_skip_month", return_value=True
            ) as rest,
        ):
            accepted = _accept_new_task(context)

        self.assertTrue(accepted)
        rest.assert_called_once()
        self.assertEqual(5, context.run_tasks.count("FindCityTask_SwipeUp"))
        self.assertEqual(5, context.run_tasks.count("FindCityTask_SwipeDown"))
        self.assertEqual(0, context.run_tasks.count("FindCityTask_Refresh"))


if __name__ == "__main__":
    unittest.main()
