from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger

import action.fight.fight_utils as fight_utils


@AgentServer.custom_action("TaskProcessor")
class TaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:

        # 1. 先确定已经在游戏界面了
        if not context.run_recognition(
            "UI_MainWindows", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.error("未在游戏界面")
            return CustomAction.RunResult(success=False)
        else:
            logger.info("团长大人, 您回来了！")

        # 1.1 获取当前游戏信息，如当前的游戏月份， 检查当前的佣兵团配置（先默认紫砂小盾）
        current_month = fight_utils.Map_CheckCurrentMonth(context)
        logger.info(f"当前游戏月份为：{current_month}")

        # 2. 开始循环流程，先按照一年12个月进行安排
        for month in range(1, 13):
            logger.info(f"开始处理第{month}月的任务")
            # 2.1 先点击月历，切换到当前月
            context.run_task("ClickMonthCalendar")
            # 2.2 检查是否有任务可接受
            if context.run_recognition(
                "TaskAcceptable",
                context.tasker.controller.post_screencap().wait().get(),
            ).hit:
                logger.info("有任务可接受")
                # 2.2.1 点击接受任务
                context.run_task("AcceptTask")
                # 2.2.2 等待任务完成
                context.run_task("WaitTaskComplete")
            else:
                logger.info("当前月无任务可接受")

        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("FightTestFunc")
class FightTestFunc(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        fight_utils.start_task(context)
        return CustomAction.RunResult(success=True)
