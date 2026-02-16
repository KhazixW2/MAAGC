from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import time

import action.fight.fight_utils as fight_utils


@AgentServer.custom_action("TaskProcessor")
class TaskProcessor(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:

        # 1. 先确定已经在游戏界面了
        if not context.run_recognition(
            "UI_MainWindows", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.error("未在游戏界面")
            return CustomAction.RunResult(success=False)
        else:
            logger.info("团长大人, 您回来了！")

        # 1.1 获取当前游戏信息，如当前的游戏月份， 检查当前的佣兵团配置（先默认紫砂小盾）
        current_month = fight_utils.Map_CheckCurrentMonth(context)
        logger.info(f"当前游戏月份为：{current_month}")

        # 2. 前处理：检测并处理随机事件
        if not self._preprocess_events(context):
            logger.warning("前处理事件未全部处理完毕")

        # 3. 检查当前月份
        detected_month = self._check_current_month(context)
        if detected_month is None:
            logger.error("无法检测到当前月份，流程结束")
            return CustomAction.RunResult(success=False)

        logger.info(f"检测到当前月份为：{detected_month}月")

        # 根据月份判断是否有过节需求
        festival_result = self._handle_festival_by_month(context, detected_month)
        if not festival_result:
            logger.warning(f"{detected_month}月节日处理未完成")

        # 4. 过完节之后检查当前的主城，然后接取主城任务
        logger.info(f" 接取主城任务")
        logger.info(f"  进入主城并接取任务...")
        fight_utils.start_task(context)
        logger.info(f"  主城任务流程执行完成")

        return CustomAction.RunResult(success=True)

    def _preprocess_events(self, context: Context) -> bool:
        """前处理：检测并处理随机事件（佣兵加入、生娃、退休等）"""
        logger.info("开始前处理：检测随机事件...")

        max_iterations = 10  # 最多处理10次随机事件，防止死循环
        for i in range(max_iterations):
            logger.info(f"  第 {i + 1} 次检测随机事件...")

            # 截取屏幕
            screenshot = context.tasker.controller.post_screencap().wait().get()

            # 检测是否有事件弹窗
            event_type = self._detectAndManage_event(context, screenshot)

            if event_type is None:
                logger.info("  ✓ 未检测到随机事件，前处理完成")
                return True

        logger.warning("  ⚠ 达到最大迭代次数，前处理结束")
        return True

    def _detectAndManage_event(self, context: Context, screenshot) -> str:
        """检测事件类型"""
        # TODO: 根据实际图片识别事件类型
        # 1. 检测是否有佣兵加入事件
        if context.run_recognition("Event_MercenaryJoin", screenshot).hit:
            logger.info("  ✓ 检测到佣兵加入事件")
            context.run_task("Event_MercenaryJoin")
            return "mercenary_join"
        # 2. 检测是否有佣兵生娃事件
        elif context.run_recognition("Event_MercenaryBaby", screenshot).hit:
            logger.info("  ✓ 检测到佣兵生娃事件")
            context.run_task("Event_MercenaryBaby")
            return "mercenary_baby"
        # # 3. 检测是否有退休事件
        # elif context.run_recognition("Event_Retire", screenshot).hit:
        #     logger.info("  ✓ 检测到退休事件")
        #     # context.run_task("Event_Retire")
        #     return "retire"
        # 4. 无事件
        else:
            logger.info("  ✓ 未检测到随机事件")
            return None

    def _check_current_month(self, context: Context) -> int:
        """检查当前月份，循环12次尝试识别"""
        logger.info("开始检查当前月份...")

        for month in range(1, 13):
            template_name = f"UI/month/{month}.png"
            logger.info(f"  尝试检测{month}月...")

            # 使用TemplateMatch识别当前月份
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

    def _handle_festival_by_month(self, context: Context, month: int) -> bool:
        """根据月份处理节日"""

        # 节日列表
        # 2月：祈灵日，暂时不过
        # 3月：启航节，必过，需要检查当前在什么城市，然后进入对应的启航节城市过
        # 5月：春林节，不过
        # 6月：铸魂节，不过
        # 8月：丰收节，必过
        # 10月：勇士节，顺路才过，如果在佩尔国家和xx国家才过
        # 11月：亡人节，不过
        # 12月：创元节，不过

        if month == 2:
            logger.info(f"  {month}月：祈灵日，不过")
            return True
        elif month == 3:
            logger.info(f"  {month}月：启航节，必过")
            return self._handle_sailing_festival(context)
        elif month == 5:
            logger.info(f"  {month}月：春林节，不过")
            return True
        elif month == 6:
            logger.info(f"  {month}月：铸魂节，不过")
            return True
        elif month == 8:
            logger.info(f"  {month}月：丰收节，必过")
            return self._handle_harvest_festival(context)
        elif month == 10:
            logger.info(f"  {month}月：勇士节，顺路才过")
            return self._handle_warrior_festival(context)
        elif month == 11:
            logger.info(f"  {month}月：亡人节，不过")
            return True
        elif month == 12:
            logger.info(f"  {month}月：创元节，不过")
            return True
        else:
            logger.info(f"  {month}月：无节日")
            return True

    def _handle_sailing_festival(self, context: Context) -> bool:
        """处理启航节（3月）"""
        # 0. 检查当前月份是否是3月
        current_month = self._check_current_month(context)
        if current_month != 3:
            logger.warning(f"当前月份不是3月，而是{current_month}月，跳过启航节")
            return True

        # 1. 检查是否有启航节，进入对应启航节城市
        if not context.run_recognition(
            "Event_Launch", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.info("  ✓ 启航节已经过了")
            return False
        logger.info("  ✓ 检测到启航节")

        # 2. 检查是否能够抵达启航节城市
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

        # 3. 进入城市启航节页面
        if context.run_recognition(
            "Event_LaunchPage", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.info("  ✓ 检测到城市启航节页面")
            context.run_task("Event_LaunchPage")
        else:
            logger.error("  ✗ 无法进入城市启航节页面")
            return False

        # 4. 检测有几个商品
        recoDetail = context.run_recognition(
            "Event_LaunchGoods", context.tasker.controller.post_screencap().wait().get()
        )

        if recoDetail.hit:
            logger.info(f"  ✓ 检测到有{len(recoDetail.filtered_results)}件商品")
            for good in recoDetail.filtered_results:
                # 1. 点击商品
                box = good.box
                rect_x, rect_y = (
                    box[0] + box[2] // 2,
                    box[1] + box[3] // 2,
                )
                logger.info(f"  ✓ 点击商品：{good.text}，坐标：({rect_x}, {rect_y})")
                context.tasker.controller.post_click(rect_x, rect_y).wait()
                time.sleep(0.5)

                # 2. 点击购买
                context.run_task("Event_LaunchGoodsBuy")

                # 3. 检测是否存在最大商品图标
                if context.run_recognition(
                    "Event_LaunchGoodsBuyMax",
                    context.tasker.controller.post_screencap().wait().get(),
                ).hit:
                    logger.info("  ✓ 检测到最大商品图标")
                    context.run_task("Event_LaunchGoodsBuyMax")

                # 4. 确定购买
                context.run_task("Event_LaunchGoodsBuyConfirm")
                logger.info("  ✓ 确认购买")

        else:
            logger.info("  ✓ 检测到没有商品")

        # 5. 节日完成后，返回大地图
        context.run_task("UI_ReturnBigMap")
        return True

    def _handle_harvest_festival(self, context: Context) -> bool:
        """处理丰收节（8月）"""
        logger.info("    处理丰收节：必过")
        # TODO: 执行丰收节流程
        return True

    def _handle_warrior_festival(self, context: Context) -> bool:
        """处理勇士节（10月）"""
        logger.info("    处理勇士节：顺路才过，需要检查是否在佩尔国家或xx国家")
        # TODO: 检查当前国家，如果在佩尔国家或xx国家则过
        return True


@AgentServer.custom_action("FightTestFunc")
class FightTestFunc(CustomAction):
    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        fight_utils.start_task(context)
        return CustomAction.RunResult(success=True)
