from asyncio import tasks
import os
import sys
import json
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.tasker import TaskDetail
from utils import logger


from action.zshg.task_extractor import TaskExtractor


@AgentServer.custom_action("test_func")
class test_func(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:

        context.run_task("GetCityTask")
        if context.run_recognition(
            "InTaskPannel", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.info("获取城市任务成功")
        else:
            logger.error("获取城市任务失败")

        reco_detail = context.run_recognition(
            "GetCityTaskDetails",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={
                "GetCityTaskDetails": {
                    "recognition": "OCR",
                    "expected": ["接受"],
                    "roi": [15, 382, 697, 841],
                }
            },
        )
        print(reco_detail)
        print("\n\n")
        print(reco_detail.all_results)
        if reco_detail and reco_detail.all_results:
            extractor = TaskExtractor(roi=[15, 382, 697, 841])
            tasks = extractor.extract_tasks(reco_detail.all_results)
            extractor.print_task_details(tasks)
        else:
            logger.warning("未获取到OCR结果")

        return CustomAction.RunResult(success=True)
