from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
import time
from typing import Any, Optional, Set

from utils import logger
from action.zshg.battle_world_map import BattleSessionRegistry, session_key
from action.zshg.task_hud_recognizer import TaskHudRecognizer

# 进入主城任务板被模态弹窗挡住时，最多「清掉遮挡事件再重试」的次数。
TASK_PANEL_EVENT_RESCUES = 3
# 2026-09-27 00:42/00:43 实测：open_city_task_panel 偶发失败（7.5 秒内静默返回 False），
# 紧接着的月度重试用同样的流程 7.4 秒就成功了。与其把这个月判失败再整体重试，
# 不如在原地重新归中城市后重试。
TASK_PANEL_OPEN_ATTEMPTS = 3


def _handle_blocking_event(context: Context) -> bool:
    """识别并处理一层遮挡界面的模态事件，处理成功返回 True。

    2026-09-26 21:58 实测：佣兵加入 / 孩子成年 / 佣兵退休 / 通用提示这类弹窗会
    压在城市界面上，让 open_city_task_panel 的画面连续 3 次完全不变
    （日志：进入任务板状态连续无变化: open_city_list），旧逻辑直接放弃 →
    第 N/240 个月重试上限，整条 240 月归零。

    fight_processor 依赖本模块，这里用函数内延迟导入避免循环依赖。
    """
    img = _screencap(context)
    if img is None:
        return False
    try:
        from action.fight.fight_processor import detect_and_manage_event

        event_name = detect_and_manage_event(context, img)
    except Exception as exc:  # 事件处理异常不应打断接任务主流程
        logger.warning(f"处理遮挡事件异常: {exc}")
        return False
    if event_name:
        logger.info(f"任务板进入受阻，已处理遮挡事件: {event_name}")
        return True
    return False


def Map_CheckCurrentMonth(context: Context) -> int:
    """
    将月份字符串转换为整数表示

    Args:
        month (str): 月份字符串，例如 "1月"、"2月" 等

    Returns:
        int: 月份的整数表示，范围为 1 到 12
    """

    screenshot = context.tasker.controller.post_screencap().wait().get()
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
        logger.info(f"当前游戏月份为：{month}月 (模板得分={score:.3f})")
        return month
    logger.error("未识别到当前游戏月份")
    return -1


def _at_bigmap(context: Context, img: Any) -> bool:
    """
    多锚点判定当前是否在大地图。

    背景（2026-09-26 实测）：原本只用 UI_MainWindows（右下角「佣兵团」图标
    模板）判大地图，而该图标在部分界面状态下整块不渲染——maafw 日志里同一
    节点出现过 score=0.399650（阈值 0.700000）的失败，于是明明可行走的大地图
    也被判成「没回去」，ensure_at_bigmap 静默 8 次失败后整条年度任务归零。

    这里按「先便宜后可靠」的顺序补充大地图专属锚点：
      1. UI_MainWindows —— 模板匹配，命中只要 1~2ms，是常态快路径；
      2. UI_TeamPage     —— OCR 底部导航「佣兵团」；
      3. UI_CastlePage   —— OCR 底部导航「城堡」。
    2/3 的 ROI 都在底部导航栏内，只有大地图才有该导航栏，因此不会把城里、
    市场、联姻等页面误判成大地图。

    Args:
        context: MAA 上下文对象
        img: 已截好的画面（避免重复截图）

    Returns:
        bool: 命中任一锚点即返回 True
    """
    if context.run_recognition("UI_MainWindows", img).hit:
        return True
    if context.run_recognition("UI_TeamPage", img).hit:
        return True
    return context.run_recognition("UI_CastlePage", img).hit


def _wait_for_bigmap(
    context: Context, timeout: float, interval: float = 0.4
) -> bool:
    """
    有界等待回到大地图（多锚点版，用于替代只认 UI_MainWindows 的单点等待）。

    Args:
        context: MAA 上下文对象
        timeout: 最长等待秒数
        interval: 每次轮询间隔秒数

    Returns:
        bool: 超时前识别到大地图返回 True
    """
    deadline = time.time() + max(0.5, timeout)
    while True:
        img = _screencap(context)
        if img is not None and _at_bigmap(context, img):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def ensure_at_bigmap(
    context: Context, auto_return: bool = True, max_attempts: int = 8
) -> bool:
    """
    检测当前是否在大地图界面，不在的话尝试返回大地图

    Args:
        context: MAA 上下文对象
        auto_return: 是否自动尝试返回大地图，默认True

    Returns:
        bool: 成功在大地图返回True，否则返回False
    """
    attempts = max(1, max_attempts if auto_return else 1)
    for attempt in range(attempts):
        img = _screencap(context)
        if img is None:
            logger.error("ensure_at_bigmap: 截图失败，无法判断当前界面")
            return False
        if _at_bigmap(context, img):
            return True
        if not auto_return:
            return False

        # UI_ReturnBigMap 内含 JumpBack 循环，遇到未知弹窗时会无界等待。
        # 这里只执行可证明、有上限的单步恢复。
        if context.run_recognition("BackButton_500ms", img).hit:
            action = "右上返回"
            context.run_task("BackButton_500ms")
        else:
            action = "屏幕中心"
            context.run_task("ClickCenter_500ms")
        # 这条日志是关键诊断信息：旧版这里完全不打印，导致月度流程卡在
        # 「接取任务」上时日志里一个字都没有，只能靠 8 秒时长反推。
        logger.warning(
            f"ensure_at_bigmap: 第 {attempt + 1}/{attempts} 次尝试仍未回到大地图，"
            f"已点击{action}"
        )
        time.sleep(0.4)

    logger.error(f"ensure_at_bigmap: {attempts} 次尝试后仍未识别到大地图，放弃")
    return False


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
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        context.run_task("UI_TaskPannelPageOpen")

    if context.run_recognition(
        "TaskQuickLocation", context.tasker.controller.post_screencap().wait().get()
    ).hit:
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
    city_menu_swipes = 0

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
        img = _screencap(context)
        if img is None:
            logger.error("进入任务板前置失败：连续截图失败，无法判断当前画面")
            return False
        if context.run_recognition("InTaskPannel", img).hit:
            logger.info(f"主城任务板已就绪 ({step + 1}/{max_steps})")
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
                logger.error("点击城市菜单里的任务入口没成功，交给上层重试")
                return False
            continue

        if context.run_recognition("SwitchOuterCity", img).hit:
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
            if not _task_succeeded(context.run_task("EnterCity")):
                logger.error("进入城市失败，交给上层重试")
                return False
            continue

        # 城市外城/内城菜单是可滚动列表；任务入口可能位于当前视口下方。
        # 只有先识别到城市页签，才允许使用项目已有的有界上滑节点，避免
        # 在未知界面盲目滑动。每次动作后回到循环重新 OCR。
        if context.run_recognition("SwitchInnerCity", img).hit:
            if city_menu_swipes >= 3:
                logger.error("城市菜单已滚动 3 次仍未找到任务入口")
                return False
            city_menu_swipes += 1
            state = f"city_menu_scroll_{city_menu_swipes}"
            if not accept_state(state):
                return False
            # FindCityTask_OCR 的 roi 只覆盖 y 406–1227，而「任务」是菜单第一行：
            # 菜单被下滚过时它会跑到 roi 顶部之外（2026-09-26 23:52 实测：可见行
            # 从「聊天」y≈453 开始，「任务」≈y293 已在框外）。旧代码只会往一个方向
            # 滚（FindCityTask_SwipeDown 把下面的行拉上来），这时越滚越找不到。
            # 改为两个方向轮流：奇数步先把上面的行拉回来，偶数步再往下找。
            scroll_node = (
                "FindCityTask_SwipeUp"
                if city_menu_swipes % 2 == 1
                else "FindCityTask_SwipeDown"
            )
            logger.info(
                "城市菜单中暂未看到任务入口，滚动列表找任务入口 "
                f"({city_menu_swipes}/3, {scroll_node})"
            )
            if not _task_succeeded(context.run_task(scroll_node)):
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
    img = _screencap(context)
    if img is None:
        return False
    if not context.run_recognition("TaskQuickLocation", img).hit:
        return True

    logger.info("检测到已接取任务，进入主城任务板校验任务类型")
    if context.run_recognition("UI_TaskPannelPageClose", img).hit:
        if not _task_succeeded(context.run_task("UI_TaskPannelPageClose")):
            return False

    if not open_city_task_panel(context):
        logger.error("无法打开当前主城任务板校验已接取任务")
        return False

    recognizer = TaskHudRecognizer()
    for swipe_index in range(max_swipes + 1):
        board_img = _screencap(context)
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

            confirm_img = _screencap(context)
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
                logger.info("非战斗任务已直接放弃（当前版本无二次确认框）")
            tip_img = _screencap(context)
            if tip_img is not None and context.run_recognition(
                "PopUpWindowTip", tip_img
            ).hit:
                context.run_task("PopUpWindowTip")
            context.run_task("BackButton_500ms")
            return ensure_at_bigmap(context)

        if swipe_index < max_swipes:
            context.run_task("FindCityTask_SwipeDown")

    logger.info("当前已接取任务未命中采购/配送规则，保留并继续执行")
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
        logger.info("====== 单次战斗任务开始 ======")

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
            logger.info("====== 单次战斗任务完成，已回到大地图 ======")
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
    logger.info("====== 接取任务 ======")

    if not ensure_at_bigmap(context):
        # 旧版在这里静默返回 False，日志上只剩下「接取任务」一行，
        # 排查月度未确认进度时无从下手；补上失败原因。
        logger.error("接取任务前置失败：未能返回大地图")
        return False

    if ensure_task_accepted(context):
        return True

    # 左右滑动会快速锁定当前任务城市的主城。
    # 这是原项目用于避免城市入口靠近右侧商城按钮时误点的归中序列。
    panel_opened = False
    for panel_attempt in range(TASK_PANEL_OPEN_ATTEMPTS):
        if panel_attempt == 0:
            context.run_task("Map_MoveMainCityLeft")
            context.run_task("Map_MoveMainCityRight")
        else:
            logger.warning(
                f"第 {panel_attempt} 次未进入主城任务板，重新归中城市后重试 "
                f"({panel_attempt}/{TASK_PANEL_OPEN_ATTEMPTS - 1})"
            )
            ensure_at_bigmap(context)
            context.run_task("Map_MoveMainCityLeft")
            context.run_task("Map_MoveMainCityRight")

        if open_city_task_panel(context):
            panel_opened = True
            break

        # 2026-09-26 21:58 实测：模态弹窗挡在城市界面上时，open_city_task_panel
        # 的画面连续 3 次完全不变（日志：进入任务板状态连续无变化: open_city_list），
        # 旧逻辑直接 return False → 第 2/240 个月重试上限 → 整条 240 月归零。
        # 这里给最多 TASK_PANEL_EVENT_RESCUES 次「清掉遮挡事件再重试」的机会。
        for attempt in range(TASK_PANEL_EVENT_RESCUES):
            if not _handle_blocking_event(context):
                break
            logger.info(f"清掉遮挡事件后第 {attempt + 1} 次重试进入主城任务板")
            if open_city_task_panel(context):
                panel_opened = True
                break
        if panel_opened:
            break

    if not panel_opened:
        logger.error(
            f"接取任务前置失败：{TASK_PANEL_OPEN_ATTEMPTS} 次尝试（含清遮挡事件）"
            "后仍未进入主城任务板"
        )
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
            screenshot = context.tasker.controller.post_screencap().wait().get()
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
                logger.info(
                    f"HUD未识别到有效任务，正在滑动刷新... "
                    f"({swipe_count + 1}/{max_swipe_times})"
                )
                context.run_task("FindCityTask_SwipeDown")
        return False

    if scan_current_pool():
        return True

    # 不刷新任务池：FindCityTask_Refresh 会消耗水晶，年度流程禁止任何
    # 钻石/水晶消耗。没有合适任务时改为「休息跳月」：返回大地图 → 点左上角
    # 沙漏 → 弹窗点确定，让游戏时间继续推进（用户 2026-09-26 指定的方案）。
    # 旧行为是直接返回 False，一个月接不到任务就把整条年度任务归零。
    logger.warning("HUD扫完整个任务池仍无可接取任务，改走休息跳月")
    if _rest_to_skip_month(context):
        logger.info("休息跳月完成：已跳过一个月，游戏时间继续推进")
        return True
    logger.warning("休息跳月未成功，按失败路径交给上层处理")
    return False


# 本月是否已被「休息跳月」消耗掉。跳月本身就把游戏时间推进了一个月，
# 所以随后必须跳过战斗阶段，不能再去任务板上找一个根本不存在的任务。
# （上游 review 2026-10-06 指出：原先 _accept_new_task 空手返回 True，
#   会让上层把「已跳月」当成「已接任务」，接着在无任务状态下找目标并失败。）
_MONTH_SKIPPED_BY_REST = False


def _rest_to_skip_month(context: Context) -> bool:
    """没有可接任务时休息一个月：返回大地图 → 点沙漏 → 确定。

    实测坐标（2026-09-26，720×1280 归一化空间）：
    - 大地图左上角沙漏中心约 (77, 160)
    - 弹窗文案「休息会导致士气下降，确定进入休息吗？/ 今日剩余时长(月)：97/240」
    - 取消 [179,715,66,37]、确定 [481,714,68,37]
    休息会消耗当月时长并降低士气，但能让游戏时间继续推进。
    """
    global _MONTH_SKIPPED_BY_REST
    ok = _task_succeeded(context.run_task("SkipMonth_Rest"))
    if ok:
        # 跳月成功 = 本月已经过去了，标记一下让战斗阶段直接让路
        _MONTH_SKIPPED_BY_REST = True
    return ok


def _process_fight(context: Context) -> bool:
    """
    战斗阶段：寻找任务点并完成战斗

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 战斗成功返回 True，失败返回 False
    """
    global _MONTH_SKIPPED_BY_REST
    if _MONTH_SKIPPED_BY_REST:
        _MONTH_SKIPPED_BY_REST = False
        logger.info("本月已由「休息跳月」消耗，跳过战斗阶段直接进入下一个月")
        return True

    logger.info("====== 战斗阶段 ======")

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
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        context.run_task("UI_TaskPannelPageOpen")

    recoDetail = context.run_recognition(
        "TaskQuickLocation",
        context.tasker.controller.post_screencap().wait().get(),
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
        detail_img = _screencap(context)
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
                confirm_img = _screencap(context)
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


# 战斗内「设置 → 撤退 → 确定」三个点（720x1280），与
# auto_fight_processor.RETREAT_* 保持一致。撤退弹窗原文：
# 「撤退人员不会受伤，不会消耗月份」——所以撤退是零代价的安全出口。
BATTLE_RETREAT_POINTS = (
    (49, 1237, "战斗设置"),
    (360, 721, "撤退"),
    (516, 731, "撤退确认"),
)


def _retreat_from_stuck_battle(context: Context) -> bool:
    """战斗打不下去时主动撤退，让游戏真正离开战斗界面。

    2026-10-03 实测：第 27/60 个月战斗里「结束回合」连点两次都没生效，
    AutoFightProcessor 直接判失败，但**游戏仍停在战斗界面**；随后月度
    重试全部退化成「无法到达目标城市: 翠庭」，两次就把整条 60 个月停掉。
    撤退不受伤、不消耗月份，把「卡死在战斗里」变成「重试时是干净画面」。
    """
    for x, y, label in BATTLE_RETREAT_POINTS:
        try:
            job = context.tasker.controller.post_click(x, y)
            job.wait()
        except Exception as exc:  # noqa: BLE001 - 撤退失败交给上层恢复流程
            logger.warning(f"撤退兜底：{label} 点击异常: {exc}")
            return False
        time.sleep(1.2)
    logger.info("已从打不下去的战斗撤退（不受伤、不消耗月份），游戏应已回到大地图")
    return True


def _process_fighting(context: Context) -> bool:
    """从战斗准备页或已开始的战场接管，并运行主动战斗处理器。"""
    logger.info("====== 主动战斗 ======")

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
    logger.info(
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
        logger.info("AutoFightProcessor 已完成正式战斗并识别到胜利结算页")
        BattleSessionRegistry.end(battle_session_key)
        return True
    if terminal_state == "fail":
        logger.error("AutoFightProcessor 结束后识别到战斗失败结算页")
        BattleSessionRegistry.end(battle_session_key)
        return False
    if not _task_succeeded(auto_result):
        logger.error("AutoFightProcessor 执行失败，且未出现胜负结算页")
        # 这里最关键：不能让游戏留在战斗界面，否则月度重试全部变成
        # 「无法到达目标城市」并把整条年度任务停掉（2026-10-03 实测）。
        if _retreat_from_stuck_battle(context):
            logger.warning("本次战斗按撤退收尾，交给月度重试从干净画面继续")
        return False

    logger.error("AutoFightProcessor 返回成功，但未识别到胜利结算页")
    if _retreat_from_stuck_battle(context):
        logger.warning("本次战斗按撤退收尾，交给月度重试从干净画面继续")
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
            state = _detect_battle_state(context, _screencap(context))
            if state in {"ready", "victory", "fail"}:
                return state
            time.sleep(0.2)

        logger.warning(
            f"开战点击后页面仍未进入战场 ({attempt + 1}/{max_attempts})"
        )

    return "unknown"


def _process_post(context: Context) -> bool:
    victory_img = _screencap(context)
    if victory_img is not None and context.run_recognition(
        "FightVictory", victory_img
    ).hit:
        context.run_task("FightVictory")

    # 检测升级技能
    for _ in range(10):
        img = _screencap(context)
        if img is None or not context.run_recognition(
            "FightResultLearnSkill", img
        ).hit:
            break
        context.run_task("FightResultLearnSkill")

    # 检测是否有弹窗
    popup_img = _screencap(context)
    if popup_img is not None and context.run_recognition(
        "FightPopUp", popup_img
    ).hit:
        context.run_task("FightPopUp")

    # 结束确认
    context.run_task("FightResult_ReturnBigMap")
    if _wait_for_bigmap(context, timeout=6.0):
        logger.info("战后流程完成，已返回大地图")
        return True

    logger.warning("战后奖励流程结束后未直接识别到大地图，尝试处理战后中断事件")
    if _recover_post_battle_to_bigmap(context):
        logger.info("已处理战后中断事件并回到大地图")
        return True

    logger.error("战后流程未能回到大地图")
    return False


def _recover_post_battle_to_bigmap(context: Context, max_steps: int = 8) -> bool:
    """有界处理战斗结算后立即弹出的随机事件，并回到大地图。"""
    # 局部导入避免 fight_processor -> fight_utils 的模块加载环。
    from action.fight.fight_processor import detect_and_manage_event

    for step in range(max_steps):
        img = _screencap(context)
        if img is None:
            return False
        if _at_bigmap(context, img):
            return True

        event_name = detect_and_manage_event(context, img)
        if event_name:
            logger.info(f"战后中断事件已处理: {event_name} ({step + 1}/{max_steps})")
            time.sleep(0.5)
            continue

        # 非事件页面仅做一次有界恢复，下一轮必须重新识别状态。
        if context.run_recognition("BackButton_500ms", img).hit:
            context.run_task("BackButton_500ms")
        else:
            context.run_task("ClickCenter_500ms")
        time.sleep(0.5)

    return ensure_at_bigmap(context, max_attempts=2)


SCREENCAP_ATTEMPTS = 3


def _screencap(context: Context) -> Optional[Any]:
    """带重试的截图。

    2026-09-26 23:51 实测：``open_city_task_panel`` 里单次 ``post_screencap``
    失败（``img is None``——该分支原本一条日志都不打）会静默返回 False，
    月度判定随之失败，两次重试同因后整条年度任务归零。截图失败是可重试的
    瞬时故障，不该等于「流程失败」。
    """
    for attempt in range(SCREENCAP_ATTEMPTS):
        job = context.tasker.controller.post_screencap().wait()
        if job.succeeded:
            img = job.get()
            if img is not None:
                return img
        if attempt + 1 < SCREENCAP_ATTEMPTS:
            time.sleep(0.3)
    logger.warning(f"连续 {SCREENCAP_ATTEMPTS} 次截图失败，本步按未知画面处理")
    return None


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
        state = _detect_battle_state(context, _screencap(context))
        if state in expected:
            return state
        time.sleep(0.2)
    return "unknown"


def _wait_for_recognition(context: Context, node: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = _screencap(context)
        if img is not None and context.run_recognition(node, img).hit:
            return True
        time.sleep(0.2)
    return False


def _task_succeeded(result: Any) -> bool:
    status = getattr(result, "status", None)
    return bool(status is not None and getattr(status, "succeeded", False))
