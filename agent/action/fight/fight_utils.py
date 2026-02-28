from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
import time

from utils import logger
from action.zshg.task_extractor import TaskExtractor


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


def ensure_at_bigmap(context: Context, auto_return: bool = True) -> bool:
    """
    检测当前是否在大地图界面，不在的话尝试返回大地图

    Args:
        context: MAA 上下文对象
        auto_return: 是否自动尝试返回大地图，默认True

    Returns:
        bool: 成功在大地图返回True，否则返回False
    """
    if context.run_recognition(
        "UI_MainWindows",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        return True

    if not auto_return:
        return False

    context.run_task("UI_ReturnBigMap")

    if context.run_recognition(
        "UI_MainWindows",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        return True

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
    logger.info("=== 开始执行任务流程 ===")

    if not _preprocess_accept_task(context):
        return False

    _process_fight(context)

    logger.info("=== 任务流程执行结束 ===")
    return True


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

    context.run_task("Map_MoveMainCityLeft")
    context.run_task("Map_MoveMainCityRight")

    context.run_task("OpenCityTaskPanel")
    if context.run_recognition(
        "InTaskPannel", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        return _accept_new_task(context)
    else:
        return False


def _accept_new_task(context: Context) -> bool:
    """
    接取新任务

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 接取成功返回 True
    """
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

    tasks = []
    if reco_detail.hit:
        extractor = TaskExtractor(roi=[15, 382, 697, 841])
        tasks = extractor.extract_tasks(reco_detail.all_results)
        extractor.print_task_details(tasks)

    if tasks:
        accept_task = tasks[0]
        accept_task_rect = accept_task.accept_button_box
        if accept_task_rect:
            accept_task_rect_x, accept_task_rect_y = (
                accept_task_rect.x + accept_task_rect.w // 2,
                accept_task_rect.y + accept_task_rect.h // 2,
            )
            context.tasker.controller.post_click(
                accept_task_rect_x, accept_task_rect_y
            ).wait()
            time.sleep(0.5)

    return True


def _process_fight(context: Context) -> bool:
    """
    战斗阶段：寻找任务点并完成战斗

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 战斗成功返回 True，失败返回 False
    """
    logger.info("====== 战斗阶段 ======")

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
        return False

    rect = recoDetail.best_result.box
    rect_x, rect_y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    context.tasker.controller.post_click(rect_x, rect_y).wait()
    time.sleep(0.5)

    context.run_task("TaskDetailOpen")
    context.run_task("TaskDetailFight")

    context.run_task("FightStart")
    round_count = 0
    while True:
        img = context.tasker.controller.post_screencap().wait().get()
        if context.tasker.stopping:
            logger.info(f"\n战斗中，已停止")
            break
        if context.run_recognition("FightFail", img).hit:
            context.run_task("FightFail")
            logger.info("\n战斗失败")
            break
        if context.run_recognition("FightVictory", img).hit:
            context.run_task("FightVictory")
            logger.info(f"\n战斗胜利（{round_count}回合）")
            break

        context.run_task("FightEndRound")
        round_count += 1
        print(f"\r[info]战斗中，当前{round_count}回合...", flush=True, end="")

    logger.info(f"战斗结束，共{round_count}回合")

    # 检测升级技能
    while context.run_recognition(
        "FightResultLearnSkill",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        context.run_task("FightResultLearnSkill")

    # 检测是否有弹窗
    if context.run_recognition(
        "FightPopUp",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        context.run_task("FightPopUp")

    # 结束确认
    context.run_task("FightResultConfirm")

    return True
