"""
佣兵爵位识别与招募决策

流程：
  1. OCR扫描详情页，识别爵位文字
  2. 日志输出："该佣兵是[公爵/伯爵/男爵/骑士]爵位" 或 "该佣兵爵位未知"
  3. 根据用户配置决定招募或取消
"""
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import time


@AgentServer.custom_action("MercenaryRecruitDecider")
class MercenaryRecruitDecider(CustomAction):

    def run(self, context: Context, args: dict) -> bool:
        # ============================================================
        # 1. 读取用户配置的允许爵位
        # ============================================================
        allow_knight = args.get("allow_knight", True)
        allow_baron = args.get("allow_baron", True)
        allow_count = args.get("allow_count", True)
        allow_duke = args.get("allow_duke", True)

        allowed_titles = []
        if allow_knight: allowed_titles.append("骑士")
        if allow_baron:  allowed_titles.append("男爵")
        if allow_count:  allowed_titles.append("伯爵")
        if allow_duke:   allowed_titles.append("公爵")

        # ============================================================
        # 2. OCR识别详情页中的爵位文字
        # ============================================================
        reco = context.run_recognition(
            "MercenaryTitleScan",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={
                "MercenaryTitleScan": {
                    "recognition": "OCR",
                    "expected": ["[\u4E00-\u9FFF]+"],
                    "roi": [0, 200, 640, 300],
                }
            },
        )

        # 遍历OCR结果，查找爵位关键字
        detected_title = ""
        if reco.hit and reco.all_results:
            for r in reco.all_results:
                text = r.text.strip()
                if not text:
                    continue
                for t in ["公爵", "伯爵", "男爵", "骑士"]:
                    if t in text:
                        detected_title = t
                        break
                if detected_title:
                    break

        # ============================================================
        # 3. 日志输出
        # ============================================================
        if detected_title:
            logger.info(f"[招募筛选] 该佣兵是【{detected_title}】爵位")
        else:
            logger.info("[招募筛选] 该佣兵爵位未知")

        # ============================================================
        # 4. 判断招募或取消
        # ============================================================
        if detected_title in allowed_titles:
            logger.info(f"[招募筛选] {detected_title} 在允许列表中，招募！")
            self._click_recruit(context)
        else:
            logger.info(f"[招募筛选] 不满足招募条件，取消")
            self._click_cancel(context)

        return True

    def _click_recruit(self, context: Context) -> None:
        context.tasker.controller.post_click(368, 859).wait()
        time.sleep(0.5)

    def _click_cancel(self, context: Context) -> None:
        context.tasker.controller.post_click(250, 859).wait()
        time.sleep(0.5)
