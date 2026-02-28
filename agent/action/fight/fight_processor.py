from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import time

import action.fight.fight_utils as fight_utils


def preprocess_events(context: Context) -> bool:
    """前处理：检测并处理随机事件"""
    logger.info("检测随机事件...")

    max_iterations = 10
    for i in range(max_iterations):
        screenshot = context.tasker.controller.post_screencap().wait().get()
        event_type = detect_and_manage_event(context, screenshot)

        if event_type is None:
            return True

    return True


def detect_and_manage_event(context: Context, screenshot) -> str:
    """检测事件类型"""
    if context.run_recognition("Event_MercenaryJoin", screenshot).hit:
        logger.info("检测到佣兵加入事件")
        context.run_task("Event_MercenaryJoin")
        return "mercenary_join"
    elif context.run_recognition("Event_MercenaryBaby", screenshot).hit:
        logger.info("检测到佣兵生娃事件")
        context.run_task("Event_MercenaryBaby")
        return "mercenary_baby"
    elif context.run_recognition("Event_HarvestFestival", screenshot).hit:
        logger.info("检测到丰收节事件")
        context.run_task("Event_HarvestFestivalDealWith")
        return "harvest_festival"
    else:
        return None


def check_current_month(context: Context) -> int:
    """检查当前月份"""
    for month in range(1, 13):
        template_name = f"UI/month/{month}.png"
        result = context.run_recognition(
            "Map_GetMonth",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={
                "Map_GetMonth": {
                    "recognition": "TemplateMatch",
                    "template": template_name,
                    "roi": [58, 2, 610, 221],
                }
            },
        )
        if result.hit:
            return month
    return None


def handle_festival_by_month(context: Context, month: int) -> bool:
    """根据月份处理节日"""
    if month == 2:
        logger.info("本月：祈灵日，跳过")
        return True
    elif month == 3:
        logger.info("本月：启航节")
        return handle_sailing_festival(context)
    elif month == 5:
        logger.info("本月：春林节，跳过")
        return True
    elif month == 6:
        logger.info("本月：铸魂节，跳过")
        return True
    elif month == 8:
        logger.info("本月：丰收节")
        return handle_harvest_festival(context)
    elif month == 10:
        logger.info("本月：勇士节")
        return handle_warrior_festival(context)
    elif month == 11:
        logger.info("本月：亡人节，跳过")
        return True
    elif month == 12:
        logger.info("本月：创元节，跳过")
        return True
    else:
        logger.info("本月：无节日")
        return True


def handle_sailing_festival(context: Context) -> bool:
    """处理启航节（3月）"""
    current_month = check_current_month(context)
    if current_month != 3:
        logger.warning(f"当前月份不是3月，而是{current_month}月，跳过启航节")
        return True

    if not context.run_recognition(
        "Event_Launch", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        logger.info("启航节已过")
        return True

    context.run_task("Event_Launch")
    if context.run_recognition(
        "Event_LaunchEnter", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        context.run_task("Event_LaunchEnter")
    elif context.run_recognition(
        "Event_LaunchLongDistance",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        logger.info("启航节城市距离过远")
        return False

    if context.run_recognition(
        "Event_LaunchPage", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        context.run_task("Event_LaunchPage")
    else:
        logger.error("无法进入启航节页面")
        return False

    recoDetail = context.run_recognition(
        "Event_LaunchGoods", context.tasker.controller.post_screencap().wait().get()
    )

    if recoDetail.hit:
        logger.info(f"检测到{len(recoDetail.filtered_results)}件商品")
        for good in recoDetail.filtered_results:
            box = good.box
            rect_x, rect_y = box[0] + box[2] // 2, box[1] + box[3] // 2
            logger.info(f"点击商品：{good.text}")
            context.tasker.controller.post_click(rect_x, rect_y).wait()
            time.sleep(0.5)
            context.run_task("Event_LaunchGoodsBuy")

            if context.run_recognition(
                "Event_LaunchGoodsBuyMax",
                context.tasker.controller.post_screencap().wait().get(),
            ).hit:
                context.run_task("Event_LaunchGoodsBuyMax")

            context.run_task("Event_LaunchGoodsBuyConfirm")
    else:
        logger.info("没有商品")

    context.run_task("UI_ReturnBigMap")
    return True


def handle_harvest_festival(context: Context) -> bool:
    """处理丰收节（8月）"""
    logger.info("处理丰收节")
    return True


def handle_warrior_festival(context: Context) -> bool:
    """处理勇士节（10月）"""
    logger.info("处理勇士节")
    return True


def process_single_month(context: Context) -> bool:
    """处理单个月份的完整流程"""
    logger.info("========== 开始月份处理 ==========")

    preprocess_events(context)

    month = check_current_month(context)
    if month is None:
        return False

    logger.info(f"当前月份：{month}月")
    handle_festival_by_month(context, month)

    fight_utils.start_task(context)

    return True


@AgentServer.custom_action("TaskProcessor")
class TaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:

        if not fight_utils.ensure_at_bigmap(context):
            logger.error("无法回到大地图界面")
            return CustomAction.RunResult(success=False)

        logger.info("团长大人, 您回来了！")

        process_single_month(context)

        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("FightTestFunc")
class FightTestFunc(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        fight_utils.start_task(context)
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("YearlyTaskProcessor")
class YearlyTaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        logger.info("========== 开始年度任务处理 ==========")

        if not fight_utils.ensure_at_bigmap(context):
            logger.error("无法回到大地图界面")
            return CustomAction.RunResult(success=False)

        logger.info("团长大人, 您回来了！")

        for month_offset in range(12):
            if context.tasker.stopping:
                logger.info(f"已停止处理第 {month_offset + 1}/12 个月")
                break
            logger.info(f"========== 开始处理第 {month_offset + 1}/12 个月 ==========")
            process_single_month(context)
            time.sleep(3)

        logger.info("========== 年度任务处理完成 ==========")
        return CustomAction.RunResult(success=True)
