from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from dataclasses import dataclass
import time
from typing import Any, Optional, Set

from utils import logger, progress
from utils.screen import screencap
from action.zshg.task_hud_recognizer import TaskHudRecognizer

from .battle_world_map import BattleSessionRegistry, session_key

# 地图分层信号：底部出现「前往大陆」= 人在群岛层；出现「前往群岛」或
# 主城可见 = 人在大陆层。fight_processor 与 pirate_raid_processor 共用，
# 节点名只在这里定义一次。
ARCHIPELAGO_READY_NODE = "ClickGoToContinent"
CONTINENT_READY_NODES = ("ClickGoToArchipelago", "EnterCity")

# 「获得 xx」奖励横幅：结算后残留在大地图上，会盖住城市名让点击落空。
# 横幅是屏幕上的模态面板：点面板内部无效，只有在面板之外点一下或滑一下
# 才会消失，所以关闭动作必须落在横幅之外。
ITEM_GAIN_BANNER_NODE = "BigMapItemGainBanner"
ITEM_GAIN_BANNER_DISMISS_NODE = "BigMapDismissItemGainBanner"
# 横幅在随机事件流程里的事件名：它和随机事件一样属于打断流程的浮层。
ITEM_GAIN_BANNER_EVENT = "item_gain_banner"

# 同一个随机事件最多连续处理这么多次；再往后仍然是它，就不再重复触发
# （有些事件会直接开战），改用通用的返回/点击中心兜底。
MAX_EVENT_DISMISS_REPEATS = 2


@dataclass
class YearlyProgress:
    """年度任务的进度状态。

    月度流程的各个阶段共用同一行进度显示：阶段推进时原地刷新同一行，
    终端上随时能看到「第几个月、哪个城市、正在做什么」，而不是每个阶段
    都刷一条新日志。
    """

    month_index: int = 0
    total_months: int = 0
    city: str = ""

    def set_month(self, month_index: int, total_months: int) -> None:
        """切换到新的月份。"""
        self.month_index = month_index
        self.total_months = total_months

    def update(self, stage: str) -> None:
        """刷新年度任务的进度行。"""
        parts = []
        if self.total_months:
            parts.append(f"第 {self.month_index}/{self.total_months} 月")
        if self.city:
            parts.append(self.city)
        parts.append(stage)
        # depth=3：日志里的调用位置要落在调用本方法的业务代码上，
        # 而不是这层封装里。
        progress.update(" · ".join(parts), depth=3)


# 年度任务是单线程顺序执行的，用一个共享实例承载进度状态。
yearly_progress = YearlyProgress()


def Map_CheckCurrentMonth(context: Context) -> int:
    """
    将月份字符串转换为整数表示

    Args:
        month (str): 月份字符串，例如 "1月"、"2月" 等

    Returns:
        int: 月份的整数表示，范围为 1 到 12
    """

    screenshot = screencap(context)
    candidates = []
    for i in range(1, 13):
        reco_detail = context.run_recognition(
            "Map_GetMonth",
            screenshot,
            pipeline_override={
                "Map_GetMonth": {
                    "template": f"UI/month/{i}.png",
                    # 当前月份会沿钟盘旋转，因此必须覆盖整圈；下面会比较
                    # 全部命中的得分，避免按月份顺序误取静态刻度“6”。
                    "roi": [58, 2, 610, 221],
                }
            },
        )
        if not reco_detail.hit or reco_detail.best_result is None:
            continue
        score = float(getattr(reco_detail.best_result, "score", 0.0))
        candidates.append((score, i))
    if candidates:
        score, month = max(candidates)
        logger.debug(f"当前游戏月份为：{month}月 (模板得分={score:.3f})")
        return month
    logger.error("未识别到当前游戏月份")
    return -1


def find_ready_node(
    context: Context, nodes: tuple[str, ...], img: Any
) -> Optional[str]:
    """返回第一个命中的就绪节点；都没命中则返回 None。

    分层信号（大陆/群岛）的判定只有这一处实现，
    fight_utils / pirate_raid_processor 共用。
    """
    for node in nodes:
        if context.run_recognition(node, img).hit:
            return node
    return None


def _at_bigmap(context: Context, img: Any) -> bool:
    """当前画面是否是大地图。

    大地图判据集中在这里，fight_utils / fight_processor 共用同一个锚点。
    """
    return context.run_recognition("UI_MainWindows", img).hit


def _back_or_click_center(context: Context, img: Any) -> None:
    """单步回退：有右上返回就点返回，否则点屏幕中心。

    只做可证明、有上限的一步恢复；调用方负责轮询与重试。
    """
    if context.run_recognition("BackButton_500ms", img).hit:
        context.run_task("BackButton_500ms")
    else:
        context.run_task("ClickCenter_500ms")


def dismiss_item_gain_banner(
    context: Context, max_tries: int = 3, img: Any = None
) -> bool:
    """有界地关掉大地图上残留的「获得 xx」奖励横幅。

    战斗或事件结算后，大地图上会残留一条「获得 物品」横幅。它盖住城市名，
    ``EnterCity`` 的点击落在横幅上被吞掉，场景不变化，任务板就永远打不开
    （表现为「进入任务板状态连续无变化: open_city_list」）。

    横幅是屏幕上的模态面板（实测 2026-11-09）：点面板内部（比如横幅标题）
    没有任何反应，只有在面板**之外**点一下或滑一下才会消失；面板外的点击
    会被横幅吃掉，不会顺带触发底下的城市或商城按钮。

    这里用一次滑动来关闭：滑动不会被当成点击，最坏情况只是让地图平移几十
    像素，城市仍在识别范围内。返回 True 表示横幅已消失；False 表示试了若干
    次仍未确认消失。

    掉落横幅每场战斗结算都会出现，属于常规噪声，日志只记 debug；
    真正失败时才用 warning 提醒。``img`` 可以传入调用方已有的截图，
    避免重复截屏。
    """
    for attempt in range(max_tries):
        if img is None:
            img = screencap(context)
        if img is None:
            return False
        if not context.run_recognition(ITEM_GAIN_BANNER_NODE, img).hit:
            return True
        logger.debug(f"检测到「获得」奖励横幅，滑动关闭（第 {attempt + 1} 次）")
        if not _task_succeeded(context.run_task(ITEM_GAIN_BANNER_DISMISS_NODE)):
            logger.warning("关闭「获得」奖励横幅的滑动未成功")
        time.sleep(0.3)
        img = None

    img = screencap(context)
    if img is not None and not context.run_recognition(ITEM_GAIN_BANNER_NODE, img).hit:
        return True
    logger.warning("「获得」奖励横幅未能关闭，按当前画面继续")
    return False


def ensure_at_bigmap(
    context: Context, auto_return: bool = True, max_attempts: int = 8
) -> bool:
    """
    检测当前是否在大地图界面，不在的话尝试返回大地图

    随机事件（例如「表白失败」的信纸弹窗）是模态窗口，会把大地图压暗，
    点城市、滑动地图全部落空。事件识别只有 detect_and_manage_event
    一处，这里复用它把弹窗关掉再继续回大地图，不再另开战后事件分支。

    Args:
        context: MAA 上下文对象
        auto_return: 是否自动尝试返回大地图，默认True

    Returns:
        bool: 成功在大地图返回True，否则返回False
    """
    attempts = max(1, max_attempts if auto_return else 1)
    last_event = None
    repeated_event_count = 0
    event_handling_allowed = True
    for _ in range(attempts):
        img = screencap(context)
        if img is None:
            return False
        if _at_bigmap(context, img):
            return True
        if not auto_return:
            return False

        event_name = (
            _dismiss_pending_event(context, img) if event_handling_allowed else None
        )
        if event_name:
            if event_name == last_event:
                repeated_event_count += 1
            else:
                last_event = event_name
                repeated_event_count = 1
            if repeated_event_count <= MAX_EVENT_DISMISS_REPEATS:
                continue
            # 同一个事件反复出现说明这次处理没有生效，不再重复触发它
            # （某些事件会直接开战），改用通用的返回/点击中心兜底。
            event_handling_allowed = False
            logger.warning(f"随机事件 {event_name} 连续处理无变化，改用通用回退")

        # UI_ReturnBigMap 内含 JumpBack 循环，遇到未知弹窗时会无界等待。
        # 这里只执行可证明、有上限的单步恢复。
        _back_or_click_center(context, img)
        time.sleep(0.4)

    return False


def _dismiss_pending_event(context: Context, img: Any) -> Optional[str]:
    """关掉挡住大地图的随机事件弹窗，返回事件名；没有事件时返回 None。"""
    # 局部导入避免 fight_processor -> fight_utils 的模块加载环。
    from action.fight.fight_processor import detect_and_manage_event

    event_name = detect_and_manage_event(context, img)
    if not event_name:
        return None
    logger.debug(f"随机事件已处理: {event_name}")
    time.sleep(0.5)
    return event_name


def ensure_task_accepted(context: Context) -> bool:
    """
    检测任务列表中是否已接取任务（通过快速定位图标判断）

    该函数会先检查任务列表是否已打开，若未打开则先点击打开。
    然后检查任务列表中是否存在快速定位图标，若存在则表示已接取任务。

    Args:
        context: MAA 上下文对象

    Returns:
        bool: True 表示已接取任务，False 表示未接取
    """
    if not context.run_recognition(
        "UI_TaskPannelPageClose",
        screencap(context),
    ).hit:
        context.run_task("UI_TaskPannelPageOpen")

    if context.run_recognition("TaskQuickLocation", screencap(context)).hit:
        return True

    return False


def open_city_task_panel(context: Context, max_steps: int = 12) -> bool:
    """有界进入主城任务板，并处理自由日/旅行等中间页。

    旧 ``OpenCityTaskPanel`` Pipeline 会在自由日城市选择框中反复命中
    ``EnterCity``，但它的点击偏移只适用于大地图城堡图标，最终形成
    无上限的 ``EnterCity -> OpenCityTaskPanel`` 循环。这里逐步观察页面，
    每个动作后重新识别；同一状态连续三次没有变化就停止交给月度恢复。
    """
    last_state = ""
    repeated_state = 0

    def accept_state(state: str) -> bool:
        nonlocal last_state, repeated_state
        if state == last_state:
            repeated_state += 1
        else:
            last_state = state
            repeated_state = 1
        if repeated_state >= 3:
            logger.error(f"进入任务板状态连续无变化: {state}")
            return False
        return True

    for step in range(max_steps):
        img = screencap(context)
        if img is None:
            return False
        if context.run_recognition("InTaskPannel", img).hit:
            logger.debug(f"主城任务板已就绪 ({step + 1}/{max_steps})")
            return True

        if context.run_recognition("TravelDialog", img).hit:
            state = "travel_dialog"
            if not accept_state(state):
                return False
            logger.info("进入任务板途中出现旅行弹窗，选择有界步行兜底")
            if not _task_succeeded(context.run_task("TravelDialog_ChooseSlow")):
                return False
            time.sleep(0.5)
            continue

        go_button = context.run_recognition("FreeDayGoButton", img)
        if go_button.hit:
            state = "free_day"
            if not accept_state(state):
                return False
            city = context.run_recognition("EnterCity", img)
            if not city.hit or city.best_result is None:
                logger.error("自由日城市列表中没有识别到目标城市")
                return False
            _, city_y, _, city_height = (
                int(value) for value in city.best_result.box
            )
            city_center_y = city_y + city_height // 2
            candidates = (
                go_button.filtered_results
                if go_button.filtered_results
                else [go_button.best_result]
            )
            candidates = [item for item in candidates if item is not None]
            if not candidates:
                return False
            button = min(
                candidates,
                key=lambda item: abs(
                    int(item.box[1]) + int(item.box[3]) // 2 - city_center_y
                ),
            )
            button_x, button_y, button_width, button_height = (
                int(value) for value in button.box
            )
            button_center_y = button_y + button_height // 2
            if abs(button_center_y - city_center_y) > 70:
                logger.error("自由日目标城市同一行没有可信的前去按钮")
                return False
            button_x += button_width // 2
            button_y = button_center_y
            clicked = (
                context.tasker.controller.post_click(button_x, button_y)
                .wait()
                .succeeded
            )
            logger.info(
                "自由日按同行按钮进入目标城市: "
                f"({button_x}, {button_y}), succeeded={clicked}"
            )
            if not clicked:
                return False
            time.sleep(1.0)
            continue

        if context.run_recognition("EnterCity_Confirm", img).hit:
            state = "enter_confirm"
            if not accept_state(state):
                return False
            if not _task_succeeded(context.run_task("EnterCity_Confirm")):
                return False
            continue

        if context.run_recognition("FindCityTask_OCR", img).hit:
            state = "task_entry"
            if not accept_state(state):
                return False
            if not _task_succeeded(context.run_task("FindCityTask_OCR")):
                return False
            continue

        if context.run_recognition("SwitchInnerCity", img).hit:
            state = "switch_inner"
            if not accept_state(state):
                return False
            if not _task_succeeded(context.run_task("SwitchInnerCity")):
                return False
            continue

        if context.run_recognition("EnterCity", img).hit:
            state = "open_city_list"
            if not accept_state(state):
                return False
            # 结算残留的「获得 xx」横幅会吞掉落在它上面的点击（场景不变），
            # 先把它关掉，再点城市。
            dismiss_item_gain_banner(context)
            if not _task_succeeded(context.run_task("EnterCity")):
                return False
            continue

        logger.error(
            f"进入任务板遇到未知画面 ({step + 1}/{max_steps})，停止盲点"
        )
        return False

    logger.error(f"进入任务板超过有界步数 {max_steps}")
    return False


def abandon_noncombat_accepted_task(context: Context, max_swipes: int = 5) -> bool:
    """在当前主城任务板中放弃已接取的采购/配送任务。

    仅点击同时满足以下条件的任务：描述命中明确的非战斗关键词，且按钮
    OCR 为“放弃”。可领取的同类任务按钮是“接受”，不会被误点。
    """
    img = screencap(context)
    if img is None:
        return False
    if not context.run_recognition("TaskQuickLocation", img).hit:
        return True

    logger.debug("检测到已接取任务，进入主城任务板校验任务类型")
    if context.run_recognition("UI_TaskPannelPageClose", img).hit:
        if not _task_succeeded(context.run_task("UI_TaskPannelPageClose")):
            return False

    if not open_city_task_panel(context):
        logger.error("无法打开当前主城任务板校验已接取任务")
        return False

    recognizer = TaskHudRecognizer()
    for swipe_index in range(max_swipes + 1):
        board_img = screencap(context)
        if board_img is None:
            return False
        tasks = recognizer.recognize_tasks(context, board_img)
        for task in tasks:
            keyword = recognizer.non_combat_keyword(task)
            if keyword is None or "放弃" not in task.action_text:
                continue
            if task.accept_button_box is None:
                continue

            logger.warning(
                f"放弃已接取非战斗任务: {task.task_name} | "
                f"{task.task_type} | 关键词={keyword}"
            )
            box = task.accept_button_box
            x, y = box[0] + box[2] // 2, box[1] + box[3] // 2
            if not context.tasker.controller.post_click(x, y).wait().succeeded:
                return False
            time.sleep(0.5)

            confirm_img = screencap(context)
            if confirm_img is None:
                return False
            if context.run_recognition("PopUpWindowConfirm", confirm_img).hit:
                if not _task_succeeded(context.run_task("PopUpWindowConfirm")):
                    return False
                time.sleep(0.5)
            else:
                # 当前版本点击“放弃”会直接把按钮改回“接受”，没有确认框。
                # 重新识别任务板证明原任务已不再处于“放弃”状态后才继续。
                remaining = any(
                    visible.task_name == task.task_name
                    and "放弃" in visible.action_text
                    for visible in recognizer.recognize_tasks(context, confirm_img)
                )
                if remaining:
                    logger.error("非战斗任务仍显示为已接取，放弃动作未生效")
                    return False
                logger.debug("非战斗任务已直接放弃（当前版本无二次确认框）")
            tip_img = screencap(context)
            if tip_img is not None and context.run_recognition(
                "PopUpWindowTip", tip_img
            ).hit:
                context.run_task("PopUpWindowTip")
            context.run_task("BackButton_500ms")
            return ensure_at_bigmap(context)

        if swipe_index < max_swipes:
            context.run_task("FindCityTask_SwipeDown")

    logger.debug("当前已接取任务未命中采购/配送规则，保留并继续执行")
    context.run_task("BackButton_500ms")
    return ensure_at_bigmap(context)


def start_task(context: Context) -> bool:
    """
    开始执行任务流程

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 任务执行成功返回 True，否则返回 False
    """

    if not _preprocess_accept_task(context):
        return False

    return _process_fight(context)


@AgentServer.custom_action("SingleFightTaskProcessor")
class SingleFightTaskProcessor(CustomAction):
    """完成恰好一个普通战斗任务，不处理节日、城市移动或月度事件。"""

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        logger.debug("====== 单次战斗任务开始 ======")

        # 战后可能停在群岛层；普通任务在配置的主城接取。
        # 仅复用定位逻辑，不执行月度事件或节日处理。
        from action.fight.fight_processor import _ensure_at_target_city

        target_data = context.get_node_data("EnterCity") or {}
        expected = target_data.get("recognition", {}).get("param", {}).get(
            "expected", []
        )
        target_city = expected[0] if expected else ""
        reached, _ = _ensure_at_target_city(context, target_city)
        if not reached:
            logger.error("无法回到任务目标城市，停止本次单战任务")
            return CustomAction.RunResult(success=False)

        success = start_task(context)
        if success:
            logger.debug("====== 单次战斗任务完成，已回到大地图 ======")
        else:
            logger.error("单次战斗任务失败，停止后续任务")
        return CustomAction.RunResult(success=success)


def _preprocess_accept_task(context: Context) -> bool:
    """
    前处理阶段：检测并接取任务

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 前处理成功返回 True
    """
    logger.debug("====== 接取任务 ======")
    yearly_progress.update("接取任务")

    if not ensure_at_bigmap(context):
        return False

    if ensure_task_accepted(context):
        return True

    if not open_city_task_panel(context):
        return False
    return _accept_new_task(context)


def _accept_new_task(context: Context) -> bool:
    """
    接取新任务 - 默认使用HUD动态识别模式

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 接取成功返回 True
    """
    max_swipe_times = 5

    # HUD动态识别器 - 默认筛选阈值 120级以下
    hud_recognizer = TaskHudRecognizer()
    hud_max_level = 120

    def scan_current_pool() -> bool:
        # 任务板会保留上一个月的滚动位置。如果上次扫到了列表底部，
        # 继续单向下滑只会在原地重复识别。每轮先有界地回到顶部，
        # 再按既有方向扫到底，保证 HUD 覆盖整个任务池。
        for _ in range(max_swipe_times):
            context.run_task("FindCityTask_SwipeUp")

        for swipe_count in range(max_swipe_times + 1):
            screenshot = screencap(context)
            best_task = hud_recognizer.recognize_and_get_best_task(
                context, screenshot, max_level=hud_max_level
            )

            if best_task and best_task.accept_button_box:
                accept_box = best_task.accept_button_box
                accept_x = accept_box[0] + accept_box[2] // 2
                accept_y = accept_box[1] + accept_box[3] // 2
                context.tasker.controller.post_click(accept_x, accept_y).wait()
                time.sleep(0.5)
                return True

            if swipe_count < max_swipe_times:
                # 连续刷新属于进度信息，用单行进度显示：终端上原地刷新同一行。
                progress.update(
                    f"HUD 任务板滑动刷新 ({swipe_count + 1}/{max_swipe_times})"
                )
                context.run_task("FindCityTask_SwipeDown")
        return False

    if scan_current_pool():
        return True

    # 整个列表只有黑名单、保护或非战斗任务时，使用页面提供的
    # 10 水晶刷新一次，然后从顶部重新扫描。单次调用最多消耗一次，
    # 年度层仍保留自己的两次月度上限，不会无界刷新。
    logger.warning("HUD扫完整个任务池仍无候选，尝试水晶刷新一次")
    refresh_result = context.run_task("FindCityTask_Refresh")
    if not _task_succeeded(refresh_result):
        logger.error("任务池刷新节点执行失败")
        return False
    if scan_current_pool():
        return True

    logger.error("刷新任务池后仍未检测到可接取的任务")
    return False


def _process_fight(context: Context) -> bool:
    """
    战斗阶段：寻找任务点并完成战斗

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 战斗成功返回 True，失败返回 False
    """
    logger.debug("====== 战斗阶段 ======")
    yearly_progress.update("战斗")

    if not _process_pre(context):
        logger.error("战斗前置流程失败，未进入战斗准备页")
        return False

    if not _process_fighting(context):
        logger.error("主动战斗未胜利，停止战后奖励流程")
        return False

    return _process_post(context)


def _process_pre(context: Context) -> bool:
    if not context.run_recognition(
        "UI_TaskPannelPageClose",
        screencap(context),
    ).hit:
        context.run_task("UI_TaskPannelPageOpen")

    recoDetail = context.run_recognition(
        "TaskQuickLocation",
        screencap(context),
    )
    if not recoDetail or not recoDetail.hit:
        logger.error("任务面板中未识别到快速定位按钮")
        return False

    rect = recoDetail.best_result.box
    rect_x, rect_y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    click_job = context.tasker.controller.post_click(rect_x, rect_y).wait()
    if not click_job.succeeded:
        logger.error("点击任务快速定位按钮失败")
        return False
    time.sleep(0.5)

    if not _task_succeeded(context.run_task("TaskDetailOpen")):
        logger.error("打开任务详情失败")
        return False
    if not _task_succeeded(context.run_task("TaskDetailFight")):
        logger.error("点击进入战斗失败")
        detail_img = screencap(context)
        if detail_img is not None and context.run_recognition(
            "TravelDialog", detail_img
        ).hit:
            logger.warning("任务定位打开了旅行弹窗，当前任务不是就地战斗任务")
            context.run_task("TravelDialog_Close")
            return False
        abandon = (
            context.run_recognition("TaskClaim", detail_img)
            if detail_img is not None
            else None
        )
        if abandon and abandon.hit:
            logger.warning("任务详情没有进入战斗，放弃当前非战斗/异常任务")
            if _task_succeeded(context.run_task("TaskClaim")):
                time.sleep(0.5)
                confirm_img = screencap(context)
                if (
                    confirm_img is not None
                    and context.run_recognition(
                        "PopUpWindowConfirm", confirm_img
                    ).hit
                ):
                    context.run_task("PopUpWindowConfirm")
                time.sleep(0.5)
        return False
    return True


def _process_fighting(context: Context) -> bool:
    """从战斗准备页或已开始的战场接管，并运行主动战斗处理器。"""
    logger.debug("====== 主动战斗 ======")
    yearly_progress.update("战斗中")

    initial_state = _wait_for_battle_state(
        context,
        {"start", "ready", "victory", "fail"},
        timeout=5.0,
    )
    battle_session_key = session_key(context)
    if initial_state == "victory":
        logger.info("进入主动战斗前已识别到胜利结算页")
        BattleSessionRegistry.end(battle_session_key)
        return True
    if initial_state == "fail":
        logger.error("进入主动战斗前已识别到失败结算页")
        BattleSessionRegistry.end(battle_session_key)
        return False
    entered_from_start = initial_state == "start"
    if initial_state == "start":
        initial_state = _start_battle_and_wait_ready(context)

    if initial_state == "victory":
        logger.info("战斗开始后直接进入胜利结算页")
        BattleSessionRegistry.end(battle_session_key)
        return True
    if initial_state == "fail":
        logger.error("战斗开始后直接进入失败结算页")
        BattleSessionRegistry.end(battle_session_key)
        return False
    if initial_state != "ready":
        logger.error("等待稳定战斗界面超时，未识别到结束回合按钮")
        return False

    session = BattleSessionRegistry.begin(
        battle_session_key,
        force_new=entered_from_start,
    )
    logger.debug(
        "稳定战斗界面已就绪，接入 AutoFightProcessor: "
        f"new={entered_from_start}, confirmed_rounds={session.confirmed_rounds}, "
        f"explored={len(session.world.observed)}"
    )
    auto_result = context.run_task("AutoFight_Start")

    # CustomAction 返回后仍以持久结算页为最终依据，避免仅凭节点被尝试过
    # 或控制器点击成功就把整场战斗判为成功。
    terminal_state = _wait_for_battle_state(
        context,
        {"victory", "fail"},
        timeout=5.0,
    )
    if terminal_state == "victory":
        logger.debug("AutoFightProcessor 已完成正式战斗并识别到胜利结算页")
        BattleSessionRegistry.end(battle_session_key)
        return True
    if terminal_state == "fail":
        logger.error("AutoFightProcessor 结束后识别到战斗失败结算页")
        BattleSessionRegistry.end(battle_session_key)
        return False
    if not _task_succeeded(auto_result):
        logger.error("AutoFightProcessor 执行失败，且未出现胜负结算页")
        return False

    logger.error("AutoFightProcessor 返回成功，但未识别到胜利结算页")
    return False


def _start_battle_and_wait_ready(context: Context, max_attempts: int = 3) -> str:
    """点击开战后必须观察到开始页消失，否则按整块按钮区域重试。"""
    for attempt in range(max_attempts):
        node = "FightStart" if attempt == 0 else "FightStartFallback"
        result = context.run_task(node)
        if not _task_succeeded(result):
            logger.warning(f"{node} 执行失败 ({attempt + 1}/{max_attempts})")

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if context.tasker.stopping:
                return "stopped"
            state = _detect_battle_state(context, screencap(context))
            if state in {"ready", "victory", "fail"}:
                return state
            time.sleep(0.2)

        logger.warning(
            f"开战点击后页面仍未进入战场 ({attempt + 1}/{max_attempts})"
        )

    return "unknown"


def _process_post(context: Context) -> bool:
    yearly_progress.update("战后处理")
    victory_img = screencap(context)
    if victory_img is not None and context.run_recognition(
        "FightVictory", victory_img
    ).hit:
        context.run_task("FightVictory")

    # 检测升级技能
    for _ in range(10):
        img = screencap(context)
        if img is None or not context.run_recognition(
            "FightResultLearnSkill", img
        ).hit:
            break
        context.run_task("FightResultLearnSkill")

    # 检测是否有弹窗
    popup_img = screencap(context)
    if popup_img is not None and context.run_recognition(
        "FightPopUp", popup_img
    ).hit:
        context.run_task("FightPopUp")

    # 结束确认
    context.run_task("FightResult_ReturnBigMap")
    if _wait_for_recognition(context, "UI_MainWindows", timeout=6.0):
        logger.debug("战后流程完成，已返回大地图")
        return True

    # 结算后可能又弹出随机事件（例如「表白失败」信纸），它会压住大地图。
    # 事件识别只有 detect_and_manage_event 一处，这里交给通用的
    # ensure_at_bigmap 统一处理，不再为战后事件单开一条恢复路径。
    if ensure_at_bigmap(context):
        logger.debug("战后流程完成，已返回大地图")
        return True

    logger.error("战后流程未能回到大地图")
    return False


def _detect_battle_state(context: Context, img: Any) -> str:
    if img is None:
        return "unknown"
    if context.run_recognition("FightFail", img).hit:
        return "fail"
    if context.run_recognition("FightVictory", img).hit:
        return "victory"
    if context.run_recognition("FightEndRound", img).hit:
        return "ready"
    if context.run_recognition("FightStart", img).hit:
        return "start"
    return "unknown"


def _wait_for_battle_state(
    context: Context, expected: Set[str], timeout: float
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return "stopped"
        state = _detect_battle_state(context, screencap(context))
        if state in expected:
            return state
        time.sleep(0.2)
    return "unknown"


def _wait_for_recognition(
    context: Context, nodes: str | tuple[str, ...], timeout: float
) -> bool:
    """有界等待给定节点（单个或一组）被识别到。"""
    node_list = (nodes,) if isinstance(nodes, str) else nodes
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = screencap(context)
        if img is not None and find_ready_node(context, node_list, img):
            return True
        time.sleep(0.2)
    return False


def _task_succeeded(result: Any) -> bool:
    status = getattr(result, "status", None)
    return bool(status is not None and getattr(status, "succeeded", False))
