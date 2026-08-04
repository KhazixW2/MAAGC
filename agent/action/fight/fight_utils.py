from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
import time
from typing import Any, Optional, Set

from utils import logger
from action.zshg.battle_world_map import BattleSessionRegistry, session_key
from action.zshg.task_hud_recognizer import TaskHudRecognizer


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
    for _ in range(attempts):
        img = _screencap(context)
        if img is None:
            return False
        if context.run_recognition("UI_MainWindows", img).hit:
            return True
        if not auto_return:
            return False

        # UI_ReturnBigMap 内含 JumpBack 循环，遇到未知弹窗时会无界等待。
        # 这里只执行可证明、有上限的单步恢复。
        if context.run_recognition("BackButton_500ms", img).hit:
            context.run_task("BackButton_500ms")
        else:
            context.run_task("ClickCenter_500ms")
        time.sleep(0.4)

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
        return False

    if ensure_task_accepted(context):
        return True

    # 左右滑动会快速锁定当前任务城市的主城
    # context.run_task("Map_MoveMainCityLeft")
    # context.run_task("Map_MoveMainCityRight")
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
    if _wait_for_recognition(context, "UI_MainWindows", timeout=6.0):
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
        if context.run_recognition("UI_MainWindows", img).hit:
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


def _screencap(context: Context) -> Optional[Any]:
    job = context.tasker.controller.post_screencap().wait()
    if not job.succeeded:
        return None
    return job.get()


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
