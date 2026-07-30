from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import time

import action.fight.fight_utils as fight_utils


# 这些月度流程会进入其他城市或独立场景，返回大地图后当前城市可能改变。
CITY_CHANGING_FESTIVAL_MONTHS = frozenset({3, 5})


def preprocess_events(context: Context) -> bool:
    """前处理：检测并处理随机事件"""
    logger.info("检测随机事件...")

    max_iterations = 15
    no_event_count = 0
    for i in range(max_iterations):
        screenshot = context.tasker.controller.post_screencap().wait().get()
        event_type = detect_and_manage_event(context, screenshot)

        if event_type is None:
            no_event_count += 1
            if no_event_count >= 3:
                logger.info("连续3次无事件，检测完成")
                return True
        else:
            no_event_count = 0
            logger.info(f"事件处理完成: {event_type}")
            
        time.sleep(0.3)
    return True


def _ensure_at_target_city(context: Context, target_city: str) -> tuple:
    """
    检测当前城市是否为目标城市，如果不在则滑动地图寻找

    Args:
        context: MAA 上下文对象
        target_city: 目标城市名称

    Returns:
        tuple: (是否在目标城市, 是否进行了城市迁移 并点击了确认)
    """
    max_swipe_times = 10

    # 战后结算或随机事件可能把视角留在群岛层。目标主城位于大陆层，
    # 此时继续左右滑动永远找不到目标城市；先识别底部“前往大陆”并
    # 有界切层，看到“前往群岛”或目标城市后才继续。
    layer_img = context.tasker.controller.post_screencap().wait().get()
    if context.run_recognition("ClickGoToContinent", layer_img).hit:
        logger.info("当前位于群岛层，先切换到大陆层")
        layer_result = context.run_task("ClickGoToContinent")
        if not fight_utils._task_succeeded(layer_result):
            logger.error("切换到大陆层的点击节点未成功")
            return False, False

        layer_deadline = time.monotonic() + 5.0
        layer_ready = False
        while time.monotonic() < layer_deadline:
            current_img = context.tasker.controller.post_screencap().wait().get()
            if (
                context.run_recognition("ClickGoToArchipelago", current_img).hit
                or context.run_recognition("EnterCity", current_img).hit
            ):
                layer_ready = True
                break
            time.sleep(0.2)
        if not layer_ready:
            logger.error("点击前往大陆后未确认进入大陆层")
            return False, False

    def target_city_visible() -> bool:
        current_img = context.tasker.controller.post_screencap().wait().get()
        return context.run_recognition("EnterCity", current_img).hit

    if target_city_visible():
        return True, False

    # 战后通常仍停在目标主城附近。先向左探测一格，再向右回到原位并
    # 再检测一次；只有近邻探测均未命中，才进入原有的连续右滑搜索。
    logger.debug(f"未直接识别到 {target_city}，先进行左右近邻探测")
    context.run_task("Map_MoveMainCityLeft")
    if target_city_visible():
        logger.info(f"近邻探测找到目标城市: {target_city}")
        return True, False

    context.run_task("Map_MoveMainCityRight")
    if target_city_visible():
        logger.info(f"回到原视野后找到目标城市: {target_city}")
        return True, False

    logger.info(f"不在目标城市，开始滑动寻找...")
    for swipe_count in range(max_swipe_times):
        logger.info(
            f"滑动寻找目标城市 {target_city} ({swipe_count + 1}/{max_swipe_times})"
        )
        context.run_task("Map_MoveMainCityRight")

        current_img = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("EnterCity", current_img)
        if reco_detail.hit and reco_detail.best_result:
            current_city = reco_detail.best_result.text
            logger.info(f"滑动后当前城市: {current_city}")
            if current_city == target_city:
                context.run_task("EnterCity")
                # 跨王国城市会触发「前往[王国]」传送弹窗。三种已知格式：
                #   - 赫雷斯特：庄园传送 (1000银) / 航海 (1月)
                #   - 佩里亚诺：乘船 (200银, 本月已用变灰) / 步行 (1月)
                #   - 瓦斯塔亚：乘船 (无此交通工具, 灰) / 步行 (1月)
                # 必须先验证弹窗真的消失，否则就是点了禁用按钮。
                travel_img = (
                    context.tasker.controller.post_screencap().wait().get()
                )
                if not context.run_recognition(
                    "TravelDialog", travel_img
                ).hit:
                    if context.run_recognition(
                        "EnterCity_Confirm",
                        context.tasker.controller.post_screencap().wait().get(),
                    ).hit:
                        context.run_task("EnterCity_Confirm")
                    logger.info("已到达目标城市")
                    return True, True

                logger.info(
                    f"检测到跨王国传送弹窗，尝试快传送进入 {target_city}"
                )
                # 先看「乘船」是否被标记为「无此交通工具」/「已使用」等。
                # 如果禁用文字出现在「乘船」行，就跳过快传送直接走步行。
                if context.run_recognition(
                    "TravelDialog_BoatDisabled", travel_img
                ).hit:
                    logger.info(
                        "检测到「乘船」禁用（无交通工具/已使用），跳过快传送"
                    )
                else:
                    context.run_task("TravelDialog_ChooseFast")
                    time.sleep(2.5)
                    post_fast_img = (
                        context.tasker.controller.post_screencap().wait().get()
                    )
                    if not context.run_recognition(
                        "TravelDialog", post_fast_img
                    ).hit:
                        logger.info("快传送成功，弹窗已关闭")
                        return True, True
                    logger.warning(
                        "快传送确认后弹窗仍存在（可能点中禁用按钮），回退到步行"
                    )
                # 回退到步行（最稳的兜底）
                context.run_task("TravelDialog_ChooseSlow")
                time.sleep(2.5)
                post_slow_img = (
                    context.tasker.controller.post_screencap().wait().get()
                )
                if context.run_recognition(
                    "TravelDialog", post_slow_img
                ).hit:
                    logger.error(
                        f"步行/航海仍未关闭弹窗，无法进入 {target_city}"
                    )
                    return False, False
                logger.info("步行/航海确认成功，弹窗已关闭")
                return True, True

    return False, False


def detect_and_manage_event(context: Context, screenshot) -> str:
    """检测事件类型"""
    if context.run_recognition("Event_MercenaryJoin", screenshot).hit:
        logger.info("检测到佣兵加入事件")
        context.run_task("Event_MercenaryJoin")
        return "mercenary_join"
    elif context.run_recognition("Event_MercenaryBaby", screenshot).hit:
        logger.info("检测到佣兵生娃事件")
        AutoNameChild = context.get_node_data("Flag_AutoNameChild").get("enabled")
        if AutoNameChild:
            logger.info("佣兵生娃自动起名已开启，执行起名与好苗子检测")
            auto_result = context.run_task("Auto_PannelCheck")
            remaining_img = context.tasker.controller.post_screencap().wait().get()
            event_remaining = context.run_recognition(
                "Event_MercenaryBaby", remaining_img
            ).hit
            if not fight_utils._task_succeeded(auto_result) or event_remaining:
                logger.warning(
                    "自动起名未完成或出生页仍存在，回退到默认确认流程"
                )
                fallback_img = remaining_img
                if not context.run_recognition("Event_MercenaryBaby", fallback_img).hit:
                    # ChildRec 失败后若详情页不再保留出生文案，有界返回一层。
                    context.run_task("BackButton_500ms")
                    fallback_img = context.tasker.controller.post_screencap().wait().get()
                if context.run_recognition("Event_MercenaryBaby", fallback_img).hit:
                    fallback_result = context.run_task("Event_MercenaryBaby")
                    if not fight_utils._task_succeeded(fallback_result):
                        logger.error("佣兵生娃默认确认流程执行失败")
        else:
            logger.info("佣兵生娃自动起名已关闭，保留游戏默认名字")
            context.run_task("Event_MercenaryBaby")
        return "mercenary_baby"
    elif context.run_recognition("事件_孩子夭折了", screenshot).hit:
        logger.info("检测到孩子夭折事件")
        context.run_task("事件_孩子夭折了")
        return "child_death"
    elif context.run_recognition("Event_HarvestFestival", screenshot).hit:
        logger.info("检测到丰收节事件")
        context.run_task("Event_HarvestFestivalDealWith")
        return "harvest_festival"
    elif context.run_recognition("Event_ConfessionSuccess", screenshot).hit:
        logger.info("检测到告白成功事件")
        context.run_task("Event_ConfessionSuccess")
        return "confession_success"
    elif context.run_recognition("Event_ConfessionFail", screenshot).hit:
        logger.info("检测到告白失败事件")
        context.run_task("Event_ConfessionFailGiveUp")
        return "confession_fail"
    elif context.run_recognition("PopUpWindowTip", screenshot).hit:
        logger.info("检测到提示事件")
        context.run_task("PopUpWindowTip")
        return "PopUpWindowTip"
    elif context.run_recognition("Event_MercenarieRetire", screenshot).hit:
        logger.info("检测到佣兵退休事件")
        context.run_task("Event_MercenarieRetire")
        return "mercenary_retire"
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
    festival_info = {
        2: "祈灵日，跳过",
        3: "启航节",
        5: "春林节，执行相亲",
        6: "铸魂节，跳过",
        8: "丰收节",
        10: "勇士节",
        11: "亡人节，跳过",
        12: "创元节，跳过",
    }
    festival_name = festival_info.get(month, "无节日")
    logger.info(f"当前月份：{month}月 - 本月：{festival_name}")

    if month == 3:
        return handle_sailing_festival(context)
    elif month == 5:
        return handle_marry_festival(context)
    elif month == 8:
        return handle_harvest_festival(context)
    elif month == 10:
        return handle_warrior_festival(context)
    return True


def handle_sailing_festival(context: Context) -> bool:
    """处理启航节（3月）"""
    # 检查是否开启了启航节自动购买
    EnableSailingFestivalPurchase = context.get_node_data(
        "Flag_EnableSailingFestivalPurchase"
    ).get("enabled")
    if not EnableSailingFestivalPurchase:
        logger.info("启航节自动购买已关闭，跳过")
        return True

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


def handle_marry_festival(context: Context) -> bool:
    """处理春林节相亲（5月）"""
    logger.info("处理春林节相亲")

    # 检查是否开启了自动相亲
    EnableMarryTask = context.get_node_data("Flag_EnableMarryTask").get("enabled")
    if not EnableMarryTask:
        logger.info("自动相亲已关闭，跳过")
        return True

    # 执行相亲处理器自定义动作
    context.run_task("Auto_MarryTask")

    # 返回大地图
    if not fight_utils.ensure_at_bigmap(context):
        logger.error("无法回到大地图界面")
        return False

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

    preprocess_events(context)
    EnableGrothTrial = context.get_node_data("Flag_GrowthTrialMode").get("enabled")
    if EnableGrothTrial: 
        growth_result = context.run_task("GrowthTrial_Start")
        if not fight_utils._task_succeeded(growth_result):
            logger.error("成长试炼入口执行失败")
            return False
        if not fight_utils._process_fighting(context):
            return False
        return fight_utils._process_post(context)
    else :
        target_city_data = context.get_node_data("EnterCity")
        target_city = (
            target_city_data.get("recognition", {})
            .get("param", {})
            .get("expected", ["王座堡"])[0]
            if target_city_data
            else "王座堡"
        )
        logger.info(f"目标城市: {target_city}")
        reached, traveled = _ensure_at_target_city(context, target_city)
        if not reached:
            logger.error(f"无法到达目标城市: {target_city}")
            return False

        if traveled:
            preprocess_events(context)
            context.run_task("BackButton_500ms")

        month = check_current_month(context)
        if month is None:
            return False

        if not handle_festival_by_month(context, month):
            return False

        if month in CITY_CHANGING_FESTIVAL_MONTHS:
            logger.info("月度节日可能改变当前城市，接取任务前重新确认目标城市")
            if not fight_utils.ensure_at_bigmap(context):
                logger.error("节日结束后无法回到大地图界面")
                return False

            reached, traveled = _ensure_at_target_city(context, target_city)
            if not reached:
                logger.error(f"节日结束后无法回到目标城市: {target_city}")
                return False

            if traveled:
                preprocess_events(context)
                if not fight_utils.ensure_at_bigmap(context):
                    logger.error("重新到达目标城市后无法回到大地图界面")
                    return False

        # 选择进入的关卡
        return fight_utils.start_task(context)


@AgentServer.custom_action("TaskProcessor")
class TaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:

        # 月度入口可能正停在上一场战斗触发的随机事件页。
        # 先有界处理事件，再执行大地图恢复，避免返回节点空点。
        preprocess_events(context)
        if not fight_utils.ensure_at_bigmap(context):
            logger.error("无法回到大地图界面")
            return CustomAction.RunResult(success=False)

        logger.info("团长大人, 您回来了！")

        success = process_single_month(context)
        return CustomAction.RunResult(success=success)


@AgentServer.custom_action("YearlyTaskProcessor")
class YearlyTaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        logger.info("开始年度任务处理")

        # 读取用户自定义的任务黑名单
        blacklist_data: dict = context.get_node_data("CustomTaskBlacklist")
        if blacklist_data:
            custom_blacklist = (
                blacklist_data.get("recognition", {})
                .get("param", {})
                .get("expected", [""])[0]
            )
            if custom_blacklist:
                from action.zshg.task_hud_recognizer import TaskBlacklist

                TaskBlacklist().add_to_blacklist(custom_blacklist)
                logger.info(f"已加载自定义任务黑名单: {custom_blacklist}")

        preprocess_events(context)
        if not fight_utils.ensure_at_bigmap(context):
            logger.error("无法回到大地图界面")
            return CustomAction.RunResult(success=False)

        months_data = context.get_node_data("YearlyTaskMonths")
        # logger.info(f"YearlyTaskMonths node_data: {months_data}")
        total_months = (
            int(
                months_data.get("recognition", {})
                .get("param", {})
                .get("expected", ["12"])[0]
            )
            if months_data
            else 12
        )
        logger.info(f"年度任务执行月份数: {total_months}")

        logger.info("团长大人, 您回来了！")

        for month_offset in range(total_months):
            if context.tasker.stopping:
                logger.info(f"已停止处理第 {month_offset + 1}/{total_months} 个月")
                break
            logger.info(f"开始处理第 {month_offset + 1}/{total_months} 个月")
            if not process_single_month(context):
                logger.error(
                    f"第 {month_offset + 1}/{total_months} 个月执行失败，停止年度任务"
                )
                return CustomAction.RunResult(success=False)
            time.sleep(3)

        logger.info("年度任务处理完成")
        return CustomAction.RunResult(success=True)
