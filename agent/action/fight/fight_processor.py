from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import time

import action.fight.fight_utils as fight_utils


def preprocess_events(context: Context) -> bool:
    """前处理：检测并处理随机事件（佣兵加入、生娃、退休等）"""
    logger.info("开始前处理：检测随机事件...")

    max_iterations = 10
    for i in range(max_iterations):
        logger.info(f"  第 {i + 1} 次检测随机事件...")

        screenshot = context.tasker.controller.post_screencap().wait().get()
        event_type = detect_and_manage_event(context, screenshot)

        if event_type is None:
            logger.info("  ✓ 未检测到随机事件，前处理完成")
            return True

    logger.warning("  ⚠ 达到最大迭代次数，前处理结束")
    return True


def detect_and_manage_event(context: Context, screenshot) -> str:
    """检测事件类型"""
    if context.run_recognition("Event_MercenaryJoin", screenshot).hit:
        logger.info("  ✓ 检测到佣兵加入事件")
        context.run_task("Event_MercenaryJoin")
        return "mercenary_join"
    elif context.run_recognition("Event_MercenaryBaby", screenshot).hit:
        logger.info("  ✓ 检测到佣兵生娃事件")
        context.run_task("Event_MercenaryBaby")
        return "mercenary_baby"
    # 丰收节事件
    elif context.run_recognition("Event_HarvestFestival", screenshot).hit:
        logger.info("  ✓ 检测到丰收节事件")
        context.run_task("Event_HarvestFestivalDealWith")
        return "harvest_festival"
    # elif context.run_recognition("Event_MercenaryRetire", screenshot).hit:
    #     logger.info("  ✓ 检测到佣兵退休事件")
    #     context.run_task("Event_MercenaryRetire")
    #     return "mercenary_retire"
    else:
        logger.info("  ✓ 未检测到随机事件")
        return None


def check_current_month(context: Context) -> int:
    """检查当前月份，循环12次尝试识别"""
    logger.info("开始检查当前月份...")

    for month in range(1, 13):
        template_name = f"UI/month/{month}.png"
        # logger.info(f"  尝试检测{month}月...")

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
            logger.info(f"  ✓ 成功检测到{month}月")
            return month

    logger.error("  ✗ 无法检测到任何月份")
    return None


def handle_festival_by_month(context: Context, month: int) -> bool:
    """根据月份处理节日"""
    if month == 2:
        logger.info(f"  {month}月：祈灵日，不过")
        return True
    elif month == 3:
        logger.info(f"  {month}月：启航节，必过")
        return handle_sailing_festival(context)
    elif month == 5:
        logger.info(f"  {month}月：春林节，不过")
        return True
    elif month == 6:
        logger.info(f"  {month}月：铸魂节，不过")
        return True
    elif month == 8:
        logger.info(f"  {month}月：丰收节，必过")
        return handle_harvest_festival(context)
    elif month == 10:
        logger.info(f"  {month}月：勇士节，顺路才过")
        return handle_warrior_festival(context)
    elif month == 11:
        logger.info(f"  {month}月：亡人节，不过")
        return True
    elif month == 12:
        logger.info(f"  {month}月：创元节，不过")
        return True
    else:
        logger.info(f"  {month}月：无节日")
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
        logger.info("  ✓ 启航节已经过了")
        return True
    logger.info("  ✓ 检测到启航节")

    context.run_task("Event_Launch")
    if context.run_recognition(
        "Event_LaunchEnter", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        logger.info("  ✓ 检测到进入启航节城市")
        context.run_task("Event_LaunchEnter")
    elif context.run_recognition(
        "Event_LaunchLongDistance",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        logger.info("  ✓ 检测到启航节城市距离过远")
        return False

    if context.run_recognition(
        "Event_LaunchPage", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        logger.info("  ✓ 检测到城市启航节页面")
        context.run_task("Event_LaunchPage")
    else:
        logger.error("  ✗ 无法进入城市启航节页面")
        return False

    recoDetail = context.run_recognition(
        "Event_LaunchGoods", context.tasker.controller.post_screencap().wait().get()
    )

    if recoDetail.hit:
        logger.info(f"  ✓ 检测到有{len(recoDetail.filtered_results)}件商品")
        for good in recoDetail.filtered_results:
            box = good.box
            rect_x, rect_y = box[0] + box[2] // 2, box[1] + box[3] // 2
            logger.info(f"  ✓ 点击商品：{good.text}，坐标：({rect_x}, {rect_y})")
            context.tasker.controller.post_click(rect_x, rect_y).wait()
            time.sleep(0.5)
            context.run_task("Event_LaunchGoodsBuy")

            if context.run_recognition(
                "Event_LaunchGoodsBuyMax",
                context.tasker.controller.post_screencap().wait().get(),
            ).hit:
                logger.info("  ✓ 检测到最大商品图标")
                context.run_task("Event_LaunchGoodsBuyMax")

            context.run_task("Event_LaunchGoodsBuyConfirm")
            logger.info("  ✓ 确认购买")
    else:
        logger.info("  ✓ 检测到没有商品")

    context.run_task("UI_ReturnBigMap")
    return True


def handle_harvest_festival(context: Context) -> bool:
    """处理丰收节（8月）"""
    logger.info("  处理丰收节：必过")
    return True


def handle_warrior_festival(context: Context) -> bool:
    """处理勇士节（10月）"""
    logger.info("  处理勇士节：顺路才过")
    return True


def process_single_month(context: Context, month: int) -> bool:
    """处理单个月份的完整流程：前处理 -> 节日 -> 任务"""
    logger.info(f"========== 开始处理 {month}月 ==========")

    if not preprocess_events(context):
        logger.warning(f"{month}月：前处理事件未全部处理完毕")

    detected_month = check_current_month(context)
    if detected_month is None:
        logger.error(f"{month}月：无法检测到当前月份")
        return False

    festival_result = handle_festival_by_month(context, detected_month)
    if not festival_result:
        logger.warning(f"{month}月：节日处理未完成")

    logger.info(f"{month}月：接取主城任务")
    fight_utils.start_task(context)
    logger.info(f"{month}月：主城任务流程执行完成")

    return True


@AgentServer.custom_action("TaskProcessor")
class TaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:

        if not context.run_recognition(
            "UI_MainWindows", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.error("未在游戏界面")
            return CustomAction.RunResult(success=False)
        else:
            logger.info("团长大人, 您回来了！")

        current_month = fight_utils.Map_CheckCurrentMonth(context)
        logger.info(f"当前游戏月份为：{current_month}")

        if not process_single_month(context, current_month):
            logger.warning("月份处理未完成")

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

        if not context.run_recognition(
            "UI_MainWindows", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.error("未在游戏界面")
            return CustomAction.RunResult(success=False)
        else:
            logger.info("团长大人, 您回来了！")

        start_month = check_current_month(context)
        if start_month is None:
            logger.error("无法检测到当前月份，流程结束")
            return CustomAction.RunResult(success=False)

        logger.info(f"当前月份为：{start_month}月，将从该月份开始执行一年任务")

        for month_offset in range(12):

            current_month = ((start_month - 1 + month_offset) % 12) + 1
            process_single_month(context, current_month)
            time.sleep(3)

        logger.info("========== 年度任务处理完成 ==========")
        return CustomAction.RunResult(success=True)
