from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
import time
from typing import Any, Optional, Set

from utils import logger
from action.zshg.task_hud_recognizer import TaskHudRecognizer


def Map_CheckCurrentMonth(context: Context) -> int:
    """
    将月份字符串转换为整数表示

    Args:
        month (str): 月份字符串，例如 "1月"、"2月" 等

    Returns:
        int: 月份的整数表示，范围为 1 到 12
    """

    for i in range(1, 13):
        if recoDetail := context.run_recognition(
            "Map_GetMonth",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={
                "Map_GetMonth": {
                    "template": f"UI/month/{i}.png",
                }
            },
        ).hit:
            logger.info(f"当前游戏月份为：{i}月")
            return i
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
    context.run_task("OpenCityTaskPanel")
    if context.run_recognition(
        "InTaskPannel", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        return _accept_new_task(context)
    else:
        return False


def _accept_new_task(context: Context) -> bool:
    """
    接取新任务 - 默认使用HUD动态识别模式

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 接取成功返回 True
    """
    max_swipe_times = 5
    swipe_count = 0

    # HUD动态识别器 - 默认筛选阈值 120级以下
    hud_recognizer = TaskHudRecognizer()
    hud_max_level = 120

    while swipe_count <= max_swipe_times:
        screenshot = context.tasker.controller.post_screencap().wait().get()

        # 优先使用HUD动态识别器识别任务
        best_task = hud_recognizer.recognize_and_get_best_task(
            context, screenshot, max_level=hud_max_level
        )

        if best_task and best_task.accept_button_box:
            accept_box = best_task.accept_button_box
            # accept_box 是 [x, y, w, h] 列表
            accept_x = accept_box[0] + accept_box[2] // 2
            accept_y = accept_box[1] + accept_box[3] // 2
            context.tasker.controller.post_click(accept_x, accept_y).wait()
            time.sleep(0.5)
            return True

        # HUD识别失败，滑动刷新
        if swipe_count < max_swipe_times:
            logger.info(
                f"HUD未识别到有效任务，正在滑动刷新... ({swipe_count + 1}/{max_swipe_times})"
            )
            context.run_task("FindCityTask_SwipeDown")
            swipe_count += 1
        else:
            logger.error("HUD识别多次失败，未检测到可接取的任务")
            return False

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
    if initial_state == "victory":
        logger.info("进入主动战斗前已识别到胜利结算页")
        return True
    if initial_state == "fail":
        logger.error("进入主动战斗前已识别到失败结算页")
        return False
    if initial_state == "start":
        initial_state = _start_battle_and_wait_ready(context)

    if initial_state == "victory":
        logger.info("战斗开始后直接进入胜利结算页")
        return True
    if initial_state == "fail":
        logger.error("战斗开始后直接进入失败结算页")
        return False
    if initial_state != "ready":
        logger.error("等待稳定战斗界面超时，未识别到结束回合按钮")
        return False

    logger.info("稳定战斗界面已就绪，接入 AutoFightProcessor")
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
        return True
    if terminal_state == "fail":
        logger.error("AutoFightProcessor 结束后识别到战斗失败结算页")
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
