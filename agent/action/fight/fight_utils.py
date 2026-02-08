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


def start_task(context: Context) -> bool:
    """
    开始执行指定的任务

    Args:
        context (Context): MAA 上下文对象
        task_name (str): 要执行的任务名称

    Returns:
        bool: 如果任务执行成功则返回 True，否则返回 False
    """

    tasks = []

    logger.info("=== 开始执行任务流程 ===")

    # 0. 先检测当前是否在大地图界面
    logger.info("步骤 0: 检测当前是否在大地图界面")
    if not context.run_recognition(
        "UI_MainWindows",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        logger.error("当前不在大地图界面")
        return False
    logger.info("✓ 已在大地图界面")

    # 1. 先找到当前所在区域的主城
    logger.info("步骤 1: 移动到当前所在区域的主城")
    context.run_task("Map_MoveMainCityNow")
    logger.info("✓ 已执行移动到主城任务")

    # 2. 打开当前主城的任务面板
    logger.info("步骤 2: 打开当前主城的任务面板")
    context.run_task("OpenCityTaskPanel")
    if context.run_recognition(
        "InTaskPannel", context.tasker.controller.post_screencap().wait().get()
    ).hit:
        logger.info("✓ 获取城市任务成功")
    else:
        logger.error("✗ 获取城市任务失败")

    logger.info("步骤 3: 识别城市任务详情")
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

    if reco_detail and reco_detail.all_results:
        logger.info("✓ 获取到OCR结果，开始提取任务信息")
        extractor = TaskExtractor(roi=[15, 382, 697, 841])
        tasks = extractor.extract_tasks(reco_detail.all_results)
        extractor.print_task_details(tasks)
    else:
        logger.warning("⚠ 未获取到OCR结果")

    # 3. 领取一个普通任务/紧急任务, 从任务列表中选择第一个，根据任务接受坐标 点击接受
    if tasks != []:
        logger.info(f"步骤 4: 领取任务（待实现），选择第一个任务：{tasks[0].task_name}")
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
        else:
            logger.warning("该任务没有接受按钮，可能已经接受或不是可接受状态")

    # 4. 根据领取的任务名称，在区域里寻找任务点
    logger.info("步骤 5: 寻找任务点（待实现）")
    # 4.1 检测任务列表打开没有，打开
    if not context.run_recognition(
        "UI_TaskPannelPageClose",
        context.tasker.controller.post_screencap().wait().get(),
    ).hit:
        logger.info("任务列表未打开，点击打开")
        context.run_task("UI_TaskPannelPageOpen")

    # 4.2 执行任务列表打开，快速定位任务点
    if recoDetail := context.run_recognition(
        "TaskQuickLocation",
        context.tasker.controller.post_screencap().wait().get(),
    ):
        if recoDetail.hit:
            logger.info("任务列表已打开，快速定位任务点")
        else:
            logger.error("任务列表打开失败")
            return False
        logger.info("任务列表已打开，快速定位任务点")
        rect = recoDetail.best_result.box
        rect_x, rect_y = (
            rect[0] + rect[2] // 2,
            rect[1] + rect[3] // 2,
        )
        context.tasker.controller.post_click(rect_x, rect_y).wait()
        time.sleep(0.5)
        logger.info("✓ 已点击任务点")

    # 5. 前往任务点完成任务
    logger.info("步骤 6: 前往任务点完成任务（待实现）")
    context.run_task("TaskDetailOpen")
    context.run_task("TaskDetailFight")

    # 6. 战斗过程，目前暂时是紫砂小盾，无限结束回合
    logger.info("步骤 7: 战斗过程（待实现）")
    context.run_task("FightStart")
    while True:
        img = context.tasker.controller.post_screencap().wait().get()
        if context.run_recognition("FightFail", img).hit:
            logger.error("战斗失败")
            context.run_task("FightFail")
            break
        if context.run_recognition("FightVictory", img).hit:
            logger.info("战斗胜利")
            context.run_task("FightVictory")
            break

        # 6.1 战斗中，一键跳过当前回合
        context.run_task("FightEndRound")
        logger.info("战斗中，一键结束当前回合")

    # 7. 战斗结算
    logger.info("步骤 8: 战斗结算（待实现）")
    context.run_task("FightResultConfirm")
    logger.info("✓ 已确认战斗结果")

    logger.info("=== 任务流程执行结束 ===")
    return True
