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


def check_task_accepted_in_list(context: Context) -> bool:
    """
    检测任务列表中是否已接取任务（通过快速定位图标判断）

    Args:
        context: MAA 上下文对象

    Returns:
        bool: True 表示已接取任务，False 表示未接取
    """
    screenshot = context.tasker.controller.post_screencap().wait().get()

    if context.run_recognition("TaskQuickLocation", screenshot).hit:
        logger.info("✓ 任务列表中检测到快速定位图标，已接取任务")
        return True

    logger.info("任务列表中未检测到快速定位图标，未接取任务")
    return False


def start_task(context: Context) -> bool:
    """
    开始执行任务流程

    整体流程分为三个阶段：
    1. 前处理：接取任务（检测是否已接取，未接取则接取）
    2. 战斗部分：处理任务（寻找任务点、战斗）
    3. 后处理：战后结算

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 任务执行成功返回 True，否则返回 False
    """

    logger.info("=== 开始执行任务流程 ===")

    # ========== 阶段 1: 前处理 - 接取任务 ==========
    if not _preprocess_accept_task(context):
        logger.error("前处理阶段失败")
        return False

    # ========== 阶段 2: 战斗部分 ==========
    if not _process_fight(context):
        logger.error("战斗阶段失败，非紫砂小盾请团长大人检查")

    # ========== 阶段 3: 后处理 - 战后结算 ==========
    _postprocess_battle(context)

    logger.info("=== 任务流程执行结束 ===")
    return True


def _preprocess_accept_task(context: Context) -> bool:
    """
    前处理阶段：检测并接取任务

    步骤：
    1. 检测当前是否在大地图界面
    2. 移动到当前区域主城
    3. 打开任务面板
    4. 检测是否已接取任务
       - 已接取：直接返回，后续执行战斗
       - 未接取：接取任务后返回

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 前处理成功返回 True
    """
    logger.info("====== 阶段 1: 前处理 - 接取任务 ======")

    # 1.1 检测当前是否在大地图界面
    logger.info("步骤 1.1: 检测当前是否在大地图界面")
    if not context.run_recognition(
        "UI_MainWindows",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        logger.error("当前不在大地图界面")
        return False
    logger.info("✓ 已在大地图界面")

    # 1.2 检测是否已接取任务（通过任务列表中的快速定位图标）
    if check_task_accepted_in_list(context):
        logger.info("已接取任务，跳过接取步骤")
        return True

    logger.info("未接取任务，开始接取任务...")

    # 1.3 移动到当前所在区域的主城
    logger.info("步骤 1.2: 移动到当前所在区域的主城")
    # context.run_task("Map_MoveMainCityNow")
    context.run_task("Map_MoveMainCityLeft")
    context.run_task("Map_MoveMainCityRight")
    logger.info("✓ 已执行移动到主城")

    # 1.4 打开当前主城的任务面板
    logger.info("步骤 1.3: 打开当前主城的任务面板")
    context.run_task("OpenCityTaskPanel")
    if context.run_recognition(
        "InTaskPannel", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        logger.info("✓ 获取城市任务成功")
    else:
        logger.error("✗ 获取城市任务失败")
        return False

    return _accept_new_task(context)


def _accept_new_task(context: Context) -> bool:
    """
    接取新任务

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 接取成功返回 True
    """
    logger.info("步骤 1.5: 识别并接取新任务")

    # 识别任务详情
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
    if reco_detail and reco_detail.all_results:
        logger.info("✓ 获取到OCR结果，开始提取任务信息")
        extractor = TaskExtractor(roi=[15, 382, 697, 841])
        tasks = extractor.extract_tasks(reco_detail.all_results)
        extractor.print_task_details(tasks)
    else:
        logger.warning("⚠ 未获取到OCR结果")

    # 领取第一个任务
    if tasks:
        accept_task = tasks[0]
        accept_task_rect = accept_task.accept_button_box
        if accept_task_rect:
            accept_task_rect_x, accept_task_rect_y = (
                accept_task_rect.x + accept_task_rect.w // 2,
                accept_task_rect.y + accept_task_rect.h // 2,
            )
            logger.info(f"点击接受按钮：({accept_task_rect_x}, {accept_task_rect_y})")
            context.tasker.controller.post_click(
                accept_task_rect_x, accept_task_rect_y
            ).wait()
            time.sleep(0.5)
            logger.info("✓ 任务已接受")
        else:
            logger.warning("该任务没有接受按钮，可能已经接受或不是可接受状态")

    return True


def _process_fight(context: Context) -> bool:
    """
    战斗阶段：寻找任务点并完成战斗

    步骤：
    1. 打开任务列表
    2. 快速定位任务点
    3. 前往任务点
    4. 开始战斗
    5. 战斗循环（等待胜利/失败）

    Args:
        context: MAA 上下文对象

    Returns:
        bool: 战斗成功返回 True，失败返回 False
    """
    logger.info("====== 阶段 2: 战斗部分 ======")

    # 2.1 打开任务列表并快速定位任务点
    logger.info("步骤 2.1: 打开任务列表")
    if not context.run_recognition(
        "UI_TaskPannelPageClose",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        logger.info("任务列表未打开，点击打开")
        context.run_task("UI_TaskPannelPageOpen")

    # 2.2 快速定位任务点
    logger.info("步骤 2.2: 快速定位任务点")
    recoDetail = context.run_recognition(
        "TaskQuickLocation",
        context.tasker.controller.post_screencap().wait().get(),
    )
    if not recoDetail or not recoDetail.hit:
        logger.error("任务列表打开失败")
        return False

    rect = recoDetail.best_result.box
    rect_x, rect_y = (
        rect[0] + rect[2] // 2,
        rect[1] + rect[3] // 2,
    )
    context.tasker.controller.post_click(rect_x, rect_y).wait()
    time.sleep(0.5)
    logger.info("✓ 已点击任务点")

    # 2.3 前往任务点并开始战斗
    logger.info("步骤 2.3: 前往任务点")
    context.run_task("TaskDetailOpen")
    context.run_task("TaskDetailFight")

    # 2.4 战斗循环
    logger.info("步骤 2.4: 执行战斗")
    context.run_task("FightStart")

    battle_result = None
    while True:
        img = context.tasker.controller.post_screencap().wait().get()
        if context.tasker.stopping:
            logger.info("战斗过程中收到停止信号，退出战斗循环")
            break
        if context.run_recognition("FightFail", img).hit:
            logger.error("战斗失败")
            battle_result = False
            context.run_task("FightFail")
            break
        if context.run_recognition("FightVictory", img).hit:
            logger.info("战斗胜利")
            battle_result = True
            context.run_task("FightVictory")
            break

        context.run_task("FightEndRound")
        logger.info("战斗中，一键结束当前回合")

    return battle_result if battle_result is not None else False


def _postprocess_battle(context: Context):
    """
    后处理阶段：战斗结算

    处理战斗胜利和失败的两种情况

    Args:
        context: MAA 上下文对象
    """
    logger.info("====== 阶段 3: 后处理 - 战后结算 ======")

    logger.info("步骤 3.1: 确认战斗结果")
    context.run_task("FightResultConfirm")
    logger.info("✓ 已确认战斗结果")

    # TODO: 根据战斗结果（胜利/失败）执行不同逻辑
    # 例如：胜利后获取奖励，失败后记录日志等
