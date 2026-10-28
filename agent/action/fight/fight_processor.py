from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import re
import time
from typing import Any, Optional

import action.fight.fight_utils as fight_utils


# 这些月度流程会进入其他城市或独立场景，返回大地图后当前城市可能改变。
CITY_CHANGING_FESTIVAL_MONTHS = frozenset({3, 5})

# 年度任务是长流程：任何“看起来点击成功”的动作都必须在有限时间内
# 重新观察到一个支持的状态，否则停止而不是继续消耗任务/资源。
YEARLY_MONTH_RETRIES = 2
YEARLY_RECOVERY_STEPS = 12
YEARLY_MONTH_WAIT_SECONDS = 8.0

# 市场定时采购：清单由界面「市场定时采购」输入框给出（格式 物品:年数）。


def _recovery_state(context: Context, img: Any = None) -> str:
    """返回年度流程可处理的稳定状态，不对未知画面做动作。"""
    if img is None:
        img = fight_utils._screencap(context)
    if img is None:
        return "unknown"
    # 交通确认弹窗覆盖在大地图上时，底层 UI_MainWindows 仍会命中。
    # 必须先处理弹窗，否则后续所有城市滑动都会落在遮罩上。
    if context.run_recognition("TravelDialog", img).hit:
        return "travel_dialog"
    # 进入城市时偶发误落到商城页；商城不是年度流程状态，且禁止
    # 任何商城/付费资源操作。只允许识别标题后安全返回大地图。
    if context.run_recognition("MallPageHeader", img).hit:
        return "mall_page"
    # 多锚点判定大地图：右下角「佣兵团」图标（UI_MainWindows 的模板）在部分
    # 界面状态下不渲染，只用它会把这个明明可行走的大地图判成 unknown。
    if fight_utils._at_bigmap(context, img):
        return "bigmap"
    battle_state = fight_utils._detect_battle_state(context, img)
    if battle_state != "unknown":
        return f"battle_{battle_state}"
    if context.run_recognition("BackButton_500ms", img).hit:
        return "backable"
    return "unknown"


def _screen_fingerprint(img: Any) -> int:
    """画面内容的廉价指纹，用于判断「同一个事件」背后画面是否其实变了。"""
    try:
        return hash(img.tobytes())
    except Exception:
        return 0


def _recover_yearly_to_bigmap(context: Context, max_steps: int = YEARLY_RECOVERY_STEPS) -> bool:
    """有界恢复年度入口/月份间断点，未知状态直接停止。"""
    last_event = None
    last_screen = None
    repeated_event_count = 0
    for step in range(max_steps):
        img = fight_utils._screencap(context)
        if img is None:
            logger.error("年度恢复截图失败")
            return False

        # 海盗事件的航海弹窗与战斗准备页都是事件处理器的合法断点。
        # 必须在通用 TravelDialog / PopUpWindowTip 之前分流，否则通用节点
        # 会只点掉弹窗，随后把战斗准备页误判为未知画面。
        if (
            context.run_recognition("Event_PirateRaid_TravelDialog", img).hit
            or context.run_recognition("Event_PirateRaid_BattlePage", img).hit
        ):
            logger.info(
                f"年度流程恢复状态: pirate_raid "
                f"({step + 1}/{max_steps})"
            )
            result = context.run_task("Event_PirateRaid_Dispatch")
            if not fight_utils._task_succeeded(result):
                logger.error("年度流程恢复海盗事件失败")
                return False
            time.sleep(0.5)
            continue

        # 年终那一串弹窗（年度结算收入页/支出页 → 口粮预交 → 一键祈祷 → 退出
        # 确认）在每年 12 月滚动过去时同样会出现，而且**不在丰收节上下文里**，
        # 之前没人认它，一路走到「未知画面」把整轮停掉（实测 2026-10-02 第
        # 36/60 个月）。实测 Event_HarvestFestivalDealWith 这条老链能完整吃下
        # 这一串并停在 UI_MainWindows（大地图），所以这里直接复用它。
        if (
            context.run_recognition("AnnualSettlement", img).hit
            or context.run_recognition("Event_HarvestFestivalPrePay", img).hit
        ):
            logger.info(
                f"年度流程恢复状态: year_end_festival ({step + 1}/{max_steps})"
            )
            result = context.run_task("Event_HarvestFestivalDealWith")
            if not fight_utils._task_succeeded(result):
                logger.error("年度流程恢复：年终结算/丰收节流程未走完")
                return False
            time.sleep(0.8)
            continue

        # 最上层通用提示会遮住佣兵加入等事件按钮；旅行弹窗正文含“前往”，
        # 已由更严格的 TravelDialog 单独识别，不能再把所有“提示”都当旅行。
        if context.run_recognition("TravelDialog", img).hit:
            state = "travel_dialog"
        elif context.run_recognition("PopUpWindowTip", img).hit:
            logger.info(
                f"年度流程恢复状态: popup_tip ({step + 1}/{max_steps})"
            )
            result = context.run_task("PopUpWindowTip")
            if not fight_utils._task_succeeded(result):
                return False
            time.sleep(0.5)
            continue
        else:
            event_name = detect_and_manage_event(context, img)
            if event_name:
                logger.info(
                    f"年度流程恢复状态: event_{event_name} "
                    f"({step + 1}/{max_steps})"
                )
                # 同一事件名重复出现不等于卡死：生孩子 / 表白失败这类是「可消耗
                # 弹窗」，一次调用只关掉一个，后面还排着队（实测连续弹出不同的
                # 表白失败信）。只有「事件名相同且画面也一模一样」才算没进展。
                fingerprint = _screen_fingerprint(img)
                if event_name == last_event and fingerprint == last_screen:
                    repeated_event_count += 1
                else:
                    last_event = event_name
                    last_screen = fingerprint
                    repeated_event_count = 1
                if repeated_event_count > 2:
                    logger.error(f"事件 {event_name} 连续处理无变化，停止恢复")
                    return False
                time.sleep(0.5)
                continue
            state = _recovery_state(context, img)
        logger.info(f"年度流程恢复状态: {state} ({step + 1}/{max_steps})")
        if state == "bigmap":
            return True
        if state in {"battle_start", "battle_ready", "battle_victory"}:
            if state == "battle_victory":
                battle_ok = True
            else:
                battle_ok = fight_utils._process_fighting(context)
            if not battle_ok:
                return False
            if not fight_utils._process_post(context):
                return False
            continue
        if state == "battle_fail":
            # 战斗失败结算页：点掉它（FightFail = 模板匹配 UI/FightFail.png + Click），
            # 然后回到循环继续恢复，最终回到大地图。
            # 旧行为是直接 return False「不自动重开战斗」，代价是整轮年度任务停在这个
            # 页面上不动（实测 2026-10-03 第 31/60 个月：40 回合僵局主动撤退 → 判负 →
            # 弹失败页 → 恢复流程在这里放弃 → 整个 60 个月任务终止）。
            # 点掉失败页并不消耗任何资源，也不会重开战斗，是安全的。
            logger.warning("恢复时识别到战斗失败结算页，点掉它后继续恢复")
            result = context.run_task("FightFail")
            if not fight_utils._task_succeeded(result):
                logger.error("战斗失败结算页点击失败，停止恢复")
                return False
            time.sleep(0.8)
            continue
        if state == "travel_dialog":
            # 只走已知的步行兜底，避免在交通资源状态不明时重复购买/消耗。
            result = context.run_task("TravelDialog_ChooseSlow")
            if not fight_utils._task_succeeded(result):
                return False
            time.sleep(1.0)
            continue
        if state == "mall_page":
            # 商城页只执行右上角返回，不尝试领取或购买任何礼包。
            logger.warning("检测到商城页，安全返回大地图，跳过所有商城操作")
            result = context.run_task("BackButton_500ms")
            if not fight_utils._task_succeeded(result):
                return False
            time.sleep(0.5)
            continue
        if state == "backable":
            result = context.run_task("BackButton_500ms")
            if not fight_utils._task_succeeded(result):
                return False
            time.sleep(0.5)
            continue
        logger.error("年度流程处于未知画面，停止恢复以避免盲点")
        return False
    logger.error("年度流程恢复超过有界步数，停止")
    return False


def _wait_for_month_advance(
    context: Context, previous_month: Optional[int], timeout: float = YEARLY_MONTH_WAIT_SECONDS
) -> Optional[int]:
    """等待地图稳定并确认月份确实前进；未知月份返回 None。"""
    deadline = time.monotonic() + timeout
    observed_month = None
    cleared = 0
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return None
        img = fight_utils._screencap(context)
        # 多锚点判大地图：只认 UI_MainWindows 时，图标不渲染会让这里一直
        # 等到超时，月份明明已经前进却被判成「未确认进度」。
        if img is None or not fight_utils._at_bigmap(context, img):
            time.sleep(0.3)
            continue
        observed_month = check_current_month(context)
        if observed_month is not None and observed_month != previous_month:
            return observed_month
        # 读不到月份，常见原因是模态弹窗（生孩子 Event_MercenaryBaby /
        # 表白失败信 Event_ConfessionFail …）压在月份表盘上并把整屏压暗，
        # UI/month/*.png 模板因此永远匹配不上。这里按事件表清一次弹窗，
        # 并给校验窗口续时，避免「刚清完弹窗就到点」。
        if cleared < 6:
            event_name = detect_and_manage_event(context, img)
            if event_name:
                cleared += 1
                logger.info(
                    f"月份校验期间清理弹窗事件: {event_name} ({cleared}/6)"
                )
                deadline = max(deadline, time.monotonic() + 6.0)
        time.sleep(0.5)
    logger.error(
        f"月份进度未确认: previous={previous_month}, observed={observed_month}"
    )
    return None


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
    logger.error(f"随机事件处理超过 {max_iterations} 轮仍未收敛，停止当前任务")
    return False


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
    #
    # 2026-09-27 补充：身在大陆层时底栏页签显示当前地区名「加尔提兰」，
    # 身在群岛层时显示「瑞格群岛」——「前往大陆」这个文案只在大陆层
    # 以外的部分界面上出现，所以旧判定会漏掉“人在群岛”这个最关键的
    # 情形（现场：队伍停在瑞格群岛，10 次滑动都找不到翠庭，整条年度
    # 任务归零）。这里再加上 MapTabArchipelago 作为群岛层判据，并允许
    # 走「地区切换浮窗」兜底（Event_PirateRaid_GoContinent 就是这条链）。
    layer_img = context.tasker.controller.post_screencap().wait().get()
    if (
        context.run_recognition("ClickGoToContinent", layer_img).hit
        or context.run_recognition("MapTabArchipelago", layer_img).hit
    ):
        logger.info("当前位于群岛层，先切换到大陆层")
        layer_result = context.run_task("ClickGoToContinent")
        if not fight_utils._task_succeeded(layer_result):
            logger.warning("底栏没有「前往大陆」，改走地区切换浮窗")
            layer_result = context.run_task("Event_PirateRaid_GoContinent")
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
                or context.run_recognition("MapTabContinent", current_img).hit
            ):
                layer_ready = True
                break
            time.sleep(0.2)
        if not layer_ready:
            logger.error("点击前往大陆后未确认进入大陆层")
            return False, False

    # 原项目的城市归中规避：无条件先向左、再向右，稳定城市入口位置，
    # 避免翠庭靠近右侧固定商城按钮时 EnterCity 偏移误点商城。
    logger.debug(f"定位 {target_city} 前执行主城左右归中")
    context.run_task("Map_MoveMainCityLeft")
    context.run_task("Map_MoveMainCityRight")

    def target_city_visible() -> bool:
        current_img = context.tasker.controller.post_screencap().wait().get()
        return context.run_recognition("EnterCity", current_img).hit

    if target_city_visible():
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
    elif context.run_recognition("Event_GrainConvert", screenshot).hit:
        # 2026-09-26 19:02 实测：相亲流程把游戏带进「粮食转换」弹窗，而事件表
        # 里没有它，_recovery_state 判定为 unknown → 「处于未知画面，停止恢复」
        # → 第 49/240 个月恢复失败，整条 240 月归零。这里补上安全关闭。
        # 注意：该弹窗只有「确定」一个按钮，点击会按游戏预填的数量兑换一次。
        logger.info("检测到粮食转换弹窗，按预填数量确认后关闭")
        context.run_task("Event_GrainConvert")
        return "grain_convert"
    elif context.run_recognition("Event_ChildGrowUp", screenshot).hit:
        # 2026-09-26 21:58 实测：「XX的孩子成年了！可以加入佣兵团 / 招募 · 取消」
        # 弹窗在事件表里没有任何条目，挡住月份校验与任务板恢复 →
        # 第 2/240 个月连续两次未确认进度 → 达到重试上限，整条 240 月归零。
        # 这里补上：交给 Event_MercenaryJoin 链，由界面开关「开启自动接受佣兵」
        # 决定点「招募」还是「取消」（确认按钮识别不到时会退回取消）。
        # 2026-09-26 23:11 实测：该弹窗「招募」按钮 OCR 读成「招莫」→ 点不掉 →
        # 恢复 12 步耗尽 → 第 2/240 个月失败，整条 240 月归零。
        logger.info("检测到孩子成年弹窗，按「开启自动接受佣兵」开关处理")
        context.run_task("Event_ChildGrowUp")
        return "child_grow_up"
    elif context.run_recognition("PopUpWindowTip", screenshot).hit:
        logger.info("检测到提示事件")
        context.run_task("PopUpWindowTip")
        return "PopUpWindowTip"
    elif context.run_recognition("Event_MercenarieRetire", screenshot).hit:
        logger.info("检测到佣兵退休事件")
        context.run_task("Event_MercenarieRetire")
        return "mercenary_retire"
    elif context.run_recognition("Event_PirateRaid", screenshot).hit:
        logger.info("检测到海盗袭击庄园事件，跨图前往群岛作战")
        # PirateRaidProcessor 内部保证战斗结束回到大陆层；
        # 入口先收一次 Esc 防止横幅抖动导致重复触发。
        context.run_task("Event_PirateRaid_Dispatch")
        return "pirate_raid"
    else:
        return None


def check_current_month(context: Context) -> int:
    """检查当前月份"""
    month = fight_utils.Map_CheckCurrentMonth(context)
    return month if 1 <= month <= 12 else None


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

    if not preprocess_events(context):
        logger.error("月度任务前置事件未能在有界轮次内收敛")
        return False
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
            if not preprocess_events(context):
                return False
            if not fight_utils._task_succeeded(context.run_task("BackButton_500ms")):
                return False

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
                if not preprocess_events(context):
                    return False
                if not fight_utils.ensure_at_bigmap(context):
                    logger.error("重新到达目标城市后无法回到大地图界面")
                    return False

        if not fight_utils.abandon_noncombat_accepted_task(context):
            logger.error("无法安全校验或放弃已接取的非战斗任务")
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
        if not _recover_yearly_to_bigmap(context):
            logger.error("年度任务无法安全恢复到大地图界面")
            return CustomAction.RunResult(success=False)

        logger.info("团长大人, 您回来了！")

        success = process_single_month(context)
        return CustomAction.RunResult(success=success)


# 数量弹窗里的「＋」按钮（720 坐标系，实测中心 455,800）
MARKET_QTY_PLUS_BOX = [441, 786, 28, 28]
# 单次最多点几下「＋」：再多就退回「买光」，避免一次点几百下
MARKET_QTY_MAX_CLICKS = 60
# 「固定数量」= 两段式：长按「＋」快速冲到位 → 单击「＋」补齐零头。
# 实测标定（2026-10-03，木材 21 银/个，数量从 1 起）：
#   长按 450ms → +7    长按 660ms → +17    长按 1000ms → +69
# 结论：长按是【加速】曲线（同样 210ms，前段涨 10、后段涨 52），单靠时长算不准，
# 所以长按只负责冲到目标的 ~70%，差额用单击补齐（单击一下正好 1 个，可靠）。
# 阶梯长按：三档时长/增益（按实测曲线取值），配合 max_hit 循环若干次
#   第1档 1200ms ≈ +129   第2档 800ms ≈ +26   第3档 450ms ≈ +7
# 规划思路：先按"目标 95%"用大档位凑（大档位省次数），零头用单击补齐（单击=+1，绝对准）。
# 留 5% 不给长按，是因为曲线在长按后段很陡（1000→1200 多涨 60），宁少勿多。
MARKET_QTY_TIERS = (
    # (档位节点名, 按住毫秒, 每次增益, 最多循环次数)
    ("MarketBread_MaxQty", 1200, 129, 2),
    ("MarketBread_MaxQtyB", 800, 26, 8),
    ("MarketBread_MaxQtyC", 450, 7, 20),
)
# 固定数量上限：最多买到 300 个（超过就退回「买光」）
MARKET_QTY_MAX = 300
# 单击补齐上限：按上面的规划，300 个最多也就补十几下；超过说明规划失效，改买光
MARKET_QTY_TOPUP_MAX_CLICKS = 45
# 「恢复大地图 → 进场」最多试几轮：_recover_yearly_to_bigmap 是有界的，可能
# 报成功但画面还没回大地图（实测 2026-10-03 01:56 就是这么点空了「城堡」）
MARKET_ENTER_ATTEMPTS = 2


def _parse_market_purchase_plan(context: Context) -> list[tuple[str, int, int]]:
    """解析界面「市场定时采购」里的清单。

    格式：`物品:年数[:数量]`，多项用逗号/顿号/分号隔开。
    例：`面包:3`（每 3 年买光）、`牛:1:10`（每 1 年买 10 个）、`香料:5:全部`。
    数量省略，或写 0 / 全部 / all，表示「把剩余的全买下」。
    单位默认「年」，也接受 `36月` 这种写法。解析不了的条目只告警、不抛异常。
    """
    raw = ""
    try:
        data = context.get_node_data("MarketPurchasePlan")
        if data:
            raw = (
                data.get("recognition", {})
                .get("param", {})
                .get("expected", [""])[0]
                or ""
            )
    except Exception as exc:  # noqa: BLE001 - 读不到清单时按不采购处理
        logger.warning(f"读取市场定时采购清单失败: {exc}")
        return []

    plan: list[tuple[str, int, int]] = []
    for chunk in re.split(r"[,，、;；\n\r]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = re.split(r"[:：=]", chunk, maxsplit=2)
        if len(parts) < 2:
            logger.warning(
                f"市场定时采购：条目应写成『物品:年数』或『物品:年数:数量』，已忽略: {chunk}"
            )
            continue
        name = parts[0].strip()
        spec = parts[1].strip()
        matched = re.match(
            r"^(\d+(?:\.\d+)?)\s*(年|月|y|m|months?|years?)?$", spec, re.IGNORECASE
        )
        if not name or not matched:
            logger.warning(f"市场定时采购：无法解析间隔，已忽略: {chunk}")
            continue
        value = float(matched.group(1))
        unit = (matched.group(2) or "年").lower()
        months = int(round(value * 12)) if unit.startswith(("年", "y")) else int(round(value))
        if months <= 0:
            logger.warning(f"市场定时采购：间隔必须大于 0，已忽略: {chunk}")
            continue
        qty = 0
        if len(parts) >= 3:
            spec_qty = parts[2].strip().lower()
            if spec_qty in ("", "0", "全部", "所有", "all", "max"):
                qty = 0
            elif spec_qty.isdigit() and int(spec_qty) > 0:
                qty = int(spec_qty)
            else:
                logger.warning(
                    f"市场定时采购：数量无法解析，按『买光』处理: {chunk}"
                )
        plan.append((name, months, qty))

    if plan:
        summary = "，".join(
            (
                f"{name} 每 {months // 12} 年"
                if months % 12 == 0
                else f"{name} 每 {months} 个月"
            )
            + ("（买光）" if qty <= 0 else f"（每次 {qty} 个）")
            for name, months, qty in plan
        )
        logger.info(f"市场定时采购清单: {summary}")
    else:
        logger.info("市场定时采购清单为空，本次不采购")
    return plan


def _run_market_purchases(context: Context, items: list[tuple[str, int]]) -> None:
    """按清单到市场买物品：qty <= 0 表示买光剩余，qty > 0 表示只买 qty 个。

    任一步失败都只告警，绝不影响年度任务。
    """
    # 一次进场 → 按清单顺序一件一件找、一件一件买 → 最后离场。
    # 原来是「每件都完整跑一遍城堡→市场→购买→返回」：又慢，又会被上一件留下
    # 的脏画面毒化 —— 2026-10-02 第 36/60 个月，香料那件只活了 3.9 秒就断了，
    # 就是被面包那件翻了 20 次页的残留画面害的。进场这一发仍先做有界恢复。
    # 「恢复大地图 → 进场」最多来 MARKET_ENTER_ATTEMPTS 轮：恢复函数是有界的，
    # 可能报成功但画面其实没回大地图，于是紧接着「点城堡」必然点空（01:56 实况）。
    enter_status = None
    for attempt in range(1, MARKET_ENTER_ATTEMPTS + 1):
        if not _recover_yearly_to_bigmap(context):
            logger.warning(
                f"市场定时采购：第 {attempt}/{MARKET_ENTER_ATTEMPTS} 轮无法回到大地图"
            )
            continue
        enter_detail = context.run_task("MarketEnter")
        enter_status = (
            getattr(enter_detail, "status", None) if enter_detail is not None else None
        )
        if enter_status is not None and getattr(enter_status, "succeeded", False):
            break
        enter_status = None
        logger.warning(
            f"市场定时采购：第 {attempt}/{MARKET_ENTER_ATTEMPTS} 轮进场失败"
            "（城堡/市场/购买 没点开），再恢复一次画面重试"
        )
    if enter_status is None:
        logger.warning(
            f"市场定时采购：进场失败（城堡/市场/购买 没点开，已重试 "
            f"{MARKET_ENTER_ATTEMPTS} 轮），本次跳过全部采购"
        )
        return
    logger.info(f"市场定时采购：已进市场货架，按清单顺序采购 {len(items)} 件")
    try:
        for item, qty in items:
            if qty > 0:
                logger.info(f"市场定时采购：找 {item}（要 {qty} 个）")
            else:
                logger.info(f"市场定时采购：找 {item}（买光剩余）")
            override: dict[str, dict] = {
                "MarketBread_PickBread": {"expected": [item]}
            }
            if qty > 1:
                # 上游作者 2026-10-06 review：固定数量控制「很难控制具体数量，建议后期再加」。
                # 因此本版只保留「买光」路径；下面的分档长按规划保留给后续「数量控制」迭代使用，
                # 当前一律走 else 分支（弹窗内按「↑」一键买光）。
                if False and qty <= MARKET_QTY_MAX:
                    # 阶梯规划：长按大档位先冲，零头用单击补。
                    # 实测教训（2026-10-03 买木材 266 个）：按 95% 规划、零头精确补齐，
                    # 实际买到 267 个 —— 长按每档有 ±1 的抖动，7 次长按累计多 1。
                    # 所以：目标降到 90%，并且每按一次长按就预留 1 个名额的余量，
                    # 宁可少按几下、靠单击补齐（单击 = 精确 +1，绝不抖）。
                    plan_target = int(qty * 0.90)
                    rest = plan_target
                    ops = []
                    for node, hold, gain, cap in MARKET_QTY_TIERS:
                        times = min(rest // gain, cap)
                        if times > 0:
                            ops.append((node, hold, times))
                            rest -= times * gain
                    presses = sum(times for _, _, times in ops)
                    delivered = plan_target - rest
                    topup = max(0, qty - delivered - presses)
                else:
                    ops = []
                    topup = MARKET_QTY_TOPUP_MAX_CLICKS + 1
                if ops and topup <= MARKET_QTY_TOPUP_MAX_CLICKS:
                    # 用户实测：长按「＋」会快速连加，比一下一下点快得多。
                    # 所以固定数量改成 LongPress(duration=算出来的毫秒数)。
                    order = [
                        "MarketBread_MaxQty",
                        "MarketBread_MaxQtyB",
                        "MarketBread_MaxQtyC",
                        "MarketBread_MaxQtyTopUp",
                    ]
                    used = {node: (hold, times) for node, hold, times in ops}
                    for idx, node in enumerate(order[:-1]):
                        nxt = order[idx + 1]
                        if node in used:
                            hold, times = used[node]
                            override[node] = {
                                "target": MARKET_QTY_PLUS_BOX,
                                "action": "LongPress",
                                "duration": hold,
                                "max_hit": times,
                                "next": [node, nxt],
                            }
                        else:
                            override[node] = {"next": [nxt]}
                    override["MarketBread_MaxQtyTopUp"] = {
                        "target": MARKET_QTY_PLUS_BOX,
                        "max_hit": topup,
                        "next": [
                            "MarketBread_MaxQtyTopUp",
                            "MarketBread_Confirm",
                        ],
                    }
                    logger.info(
                        f"市场定时采购：{item} 固定数量 {qty} 个 → 长按 "
                        + "，".join(f"{hold}ms×{times}" for _, hold, times in ops)
                        + f"，最后单击补齐 {topup} 下"
                    )
                else:
                    logger.warning(
                        f"市场定时采购：{item} 要的 {qty} 个超出固定数量上限"
                        f"（{MARKET_QTY_MAX} 个），本次改为买光"
                    )
            started = time.monotonic()
            detail = context.run_task("MarketPickOne", pipeline_override=override)
            elapsed = time.monotonic() - started
            status = getattr(detail, "status", None) if detail is not None else None
            node_names = [
                name
                for name in (
                    getattr(node, "name", None)
                    for node in (getattr(detail, "nodes", None) or [])
                )
                if name
            ]
            reached = " -> ".join(node_names) if node_names else "（一个节点都没命中）"
            if status is None:
                logger.warning(f"市场定时采购：{item} 未返回任务详情，本次未买到")
            elif getattr(status, "succeeded", False):
                if "MarketBread_MaxQty" in node_names:
                    amount = f"{qty} 个" if qty > 0 else "剩余全部"
                    logger.info(f"市场定时采购：{item} 已买下（{amount}），用时 {elapsed:.1f}s")
                else:
                    # 链子「成功」不等于买到了：该物品市场剩余为 0 时，右下「购买」
                    # 按钮是灰的，点了不会弹数量窗，链会走 MarketBread_Exit 体面退出，
                    # 于是 status=succeeded 但一个都没买。判据很硬：轨迹里没有
                    # MarketBread_MaxQty（数量弹窗从头到尾没出现过）。
                    # 2026-10-03 实测：香料（剩余347）轨迹含 MaxQty → 真买到 347 个；
                    # 面包（剩余0）轨迹无 MaxQty → 什么都没买。
                    logger.warning(
                        f"市场定时采购：{item} 本月已售罄（点购买没弹出数量窗），"
                        f"跳过这一件，用时 {elapsed:.1f}s，节点轨迹: {reached}"
                    )
            else:
                logger.warning(
                    f"市场定时采购：{item} 本次没买到，用时 {elapsed:.1f}s，"
                    f"节点轨迹: {reached}"
                )
    except Exception as exc:  # noqa: BLE001 - 采购失败不拖垮年度任务
        logger.warning(f"市场定时采购：采购过程异常，已跳过剩余物品: {exc}")
    finally:
        # 不管中间成没成，最后都要把队伍带回大地图：
        # 否则下一个月度任务会从「货架」起步，第一步「点城堡」必然点空。
        leave_detail = context.run_task("MarketLeave")
        leave_status = (
            getattr(leave_detail, "status", None) if leave_detail is not None else None
        )
        if not (leave_status is not None and getattr(leave_status, "succeeded", False)):
            logger.warning("市场定时采购：离场失败，画面可能仍停在市场")


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

        # 读取用户自定义的市场定时采购清单（格式 物品:年数）
        market_plan = _parse_market_purchase_plan(context)

        if not _recover_yearly_to_bigmap(context):
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

        if total_months <= 0:
            logger.error(f"无效的年度任务月份数: {total_months}")
            return CustomAction.RunResult(success=False)

        for month_offset in range(total_months):
            if context.tasker.stopping:
                logger.info(f"已停止处理第 {month_offset + 1}/{total_months} 个月")
                return CustomAction.RunResult(success=False)
            logger.info(f"开始处理第 {month_offset + 1}/{total_months} 个月")
            completed = False
            for attempt in range(YEARLY_MONTH_RETRIES):
                if not _recover_yearly_to_bigmap(context):
                    logger.error(
                        f"第 {month_offset + 1}/{total_months} 个月恢复失败，停止年度任务"
                    )
                    return CustomAction.RunResult(success=False)
                previous_month = check_current_month(context)
                if previous_month is None:
                    # 出生页/随机事件可能在恢复之后又弹出来盖住月份盘。
                    # 2026-10-03 实测：第 13/60 个月被「孩子出生」的角色属性页
                    # 挡住 → check_current_month 认不到 → 直接放弃整条年度任务。
                    # 这里再清几轮事件并重试，仍然看不到月份盘才放弃。
                    for rescue in range(YEARLY_MONTH_RETRIES + 1):
                        handled = fight_utils._handle_blocking_event(context)
                        if not handled and not _recover_yearly_to_bigmap(context):
                            break
                        previous_month = check_current_month(context)
                        if previous_month is not None:
                            logger.info(
                                f"清掉遮挡事件后重新看到月份盘: {previous_month} 月"
                            )
                            break
                        logger.warning(
                            f"月度开始前仍看不到月份盘，清事件后重试 "
                            f"({rescue + 1}/{YEARLY_MONTH_RETRIES + 1})"
                        )
                if previous_month is None:
                    logger.error("无法识别月度任务开始月份，停止而不盲目重试")
                    return CustomAction.RunResult(success=False)
                if process_single_month(context):
                    next_month = _wait_for_month_advance(context, previous_month)
                    if next_month is not None:
                        logger.info(
                            f"第 {month_offset + 1}/{total_months} 个月完成: "
                            f"{previous_month} -> {next_month}"
                        )
                        completed = True
                        break
                logger.warning(
                    f"第 {month_offset + 1}/{total_months} 个月未确认进度 "
                    f"({attempt + 1}/{YEARLY_MONTH_RETRIES})"
                )
                time.sleep(0.5)
            if not completed:
                logger.error(
                    f"第 {month_offset + 1}/{total_months} 个月达到重试上限，停止年度任务"
                )
                return CustomAction.RunResult(success=False)

            # 市场定时采购：到点的月份把清单里所有到期物品各买一次；失败不影响年度任务。
            due_items = [
                (name, qty)
                for name, interval, qty in market_plan
                if (month_offset + 1) % interval == 0
            ]
            if due_items:
                _run_market_purchases(context, due_items)

        logger.info("年度任务处理完成")
        return CustomAction.RunResult(success=True)
