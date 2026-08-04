from dataclasses import dataclass
from typing import Optional, List, Dict
from maa.define import Rect
from maa.context import Context
import os
import json
import re

from utils import logger


@dataclass
class HudTaskInfo:
    """HUD动态识别出的任务信息"""

    task_type: str = ""  # 任务图标类型：斩杀任务、生存任务等
    task_name: str = ""  # 任务名称
    task_level: str = ""  # 敌人等级
    task_description: str = ""  # 任务描述
    reward: str = ""  # 奖励
    accept_button_box: Optional[Rect] = None  # 接受按钮位置
    action_text: str = ""  # 接受按钮文字：接受/放弃
    icon_box: Optional[Rect] = None  # 图标位置


class TaskBlacklist:
    """任务黑名单单例类"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TaskBlacklist, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # 使用 getattr 避免 _initialized 不存在的 AttributeError
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        cwd_dir = os.getcwd()
        logger.debug(f"TaskBlacklist初始化，当前工作目录: {cwd_dir}")

        self.blacklist_file = os.path.join(cwd_dir, "table", "task_blacklist.json")
        self._blacklist_cache = self._load_blacklist()
        logger.debug(f"黑名单文件路径: {self.blacklist_file}")

    def _load_blacklist(self):
        try:
            if os.path.exists(self.blacklist_file):
                with open(self.blacklist_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    blacklist = set(data.get("blacklist", []))
                    logger.info(f"载入黑名单成功，共 {len(blacklist)} 个任务")
                    return blacklist
            else:
                logger.info(f"黑名单文件不存在")
                return set()
        except Exception as e:
            logger.error(f"载入黑名单失败: {e}")
            return set()

    def add_to_blacklist(self, task_names: str):
        """动态添加任务名到黑名单（同时持久化到文件）"""
        if not task_names:
            return
        names = [
            name.strip()
            for name in task_names.replace("，", ",").split(",")
            if name.strip()
        ]
        if not names:
            return

        for name in names:
            self._blacklist_cache.add(name)
        logger.info(
            f"动态添加黑名单任务: {names}，当前黑名单共 {len(self._blacklist_cache)} 个"
        )

        try:
            with open(self.blacklist_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"blacklist": list(self._blacklist_cache)},
                    f,
                    ensure_ascii=False,
                    indent=4,
                )
            logger.info(f"黑名单已保存到文件: {self.blacklist_file}")
        except Exception as e:
            logger.error(f"保存黑名单到文件失败: {e}")

    def is_in_blacklist(self, task_name: str) -> bool:
        """检查任务是否在黑名单中"""
        return task_name in self._blacklist_cache


class TaskHudRecognizer:
    """基于任务图标位置的动态HUD识别器"""

    # 任务类型黑名单（硬编码，不加入文件）
    TASK_TYPE_BLACKLIST = {"保护任务", "保护任务1"}
    # 部分“混战任务”图标实际是采购/送货委托，详情页没有“进入战斗”。
    # 只按描述排除这些非战斗任务，保留真正的混战关卡。
    NON_COMBAT_DESCRIPTION_KEYWORDS = {
        "购买",
        "杂货铺",
        "送往",
        "送到",
    }

    # 7个任务图标文件
    TASK_ICONS = [
        "Task/斩杀任务.png",
        "Task/生存任务.png",
        "Task/清场任务.png",
        "Task/混战任务.png",
        "Task/保护任务.png",
        "Task/拦截任务.png",
        "Task/保护任务1.png",
    ]

    # 4个核心区域的动态offset配置（相对于图标box的左上角）
    TASK_VALUE_OFFSETS = {
        "task_name": {"x_offset": 75, "y_offset": 0, "width": 350, "height": 50},
        "task_level": {"x_offset": 530, "y_offset": 0, "width": 140, "height": 90},
        "task_description": {
            "x_offset": 80,
            "y_offset": 100,
            "width": 570,
            "height": 110,
        },
        "reward": {"x_offset": 85, "y_offset": 255, "width": 350, "height": 120},
        "accept_button": {
            "x_offset": 480,
            "y_offset": 250,
            "width": 180,
            "height": 120,
        },
    }

    def __init__(self):
        self.blacklist = TaskBlacklist()

    def _calc_dynamic_roi(self, icon_box, offset_config: Dict[str, int]) -> List[int]:
        """根据图标box和offset配置计算动态ROI

        Args:
            icon_box: 图标的位置 [x, y, w, h] 或 Rect对象
            offset_config: offset配置，包含x_offset, y_offset, width, height

        Returns:
            [x, y, width, height] 格式的ROI
        """
        # 支持列表 [x, y, w, h] 或 Rect对象
        if isinstance(icon_box, (list, tuple)):
            bx, by_ = icon_box[0], icon_box[1]
        else:
            bx, by_ = icon_box.x, icon_box.y

        return [
            bx + offset_config["x_offset"],
            by_ + offset_config["y_offset"],
            offset_config["width"],
            offset_config["height"],
        ]

    def scan_task_icon(
        self, context: Context, screenshot, template_path: str
    ) -> Optional[Rect]:
        """在截图中扫描特定任务图标

        Args:
            context: MAA上下文
            screenshot: 截图
            template_path: 图标模板路径

        Returns:
            找到则返回图标box，否则返回None
        """
        reco_detail = context.run_recognition(
            "Hud_TaskIconMatch",
            screenshot,
            pipeline_override={
                "Hud_TaskIconMatch": {
                    "template": template_path,
                }
            },
        )

        if reco_detail.hit and reco_detail.best_result:
            return reco_detail.best_result.box
        return None

    def recognize_all_task_icons(self, context: Context, screenshot) -> List[tuple]:
        """遍历所有任务图标，返回匹配到的图标信息

        Returns:
            [(task_type, icon_box), ...] 匹配到的图标列表
        """
        matched = []
        for icon_template in self.TASK_ICONS:
            task_type = icon_template.replace("Task/", "").replace(".png", "")
            icon_box = self.scan_task_icon(context, screenshot, icon_template)
            if icon_box:
                matched.append((task_type, icon_box))
        return matched

    def recognize_tasks(self, context: Context, screenshot) -> List[HudTaskInfo]:
        """识别当前任务板内可见的全部任务，不做类型/等级过滤。"""
        task_list: List[HudTaskInfo] = []
        for task_type, icon_box in self.recognize_all_task_icons(context, screenshot):
            task_list.append(
                self.recognize_task_info_by_icon(
                    context, screenshot, task_type, icon_box
                )
            )
        return task_list

    @classmethod
    def non_combat_keyword(cls, task: HudTaskInfo) -> Optional[str]:
        """返回命中的非战斗描述关键词；没有命中时返回 None。"""
        return next(
            (
                keyword
                for keyword in cls.NON_COMBAT_DESCRIPTION_KEYWORDS
                if keyword in task.task_description
            ),
            None,
        )

    def recognize_task_info_by_icon(
        self, context: Context, screenshot, task_type: str, icon_box: Rect
    ) -> HudTaskInfo:
        """根据任务图标位置识别任务的5个核心区域信息

        Args:
            context: MAA上下文
            screenshot: 截图
            task_type: 任务类型名称
            icon_box: 图标位置

        Returns:
            HudTaskInfo对象
        """
        task_info = HudTaskInfo(task_type=task_type, icon_box=icon_box)

        # 识别任务名称
        roi = self._calc_dynamic_roi(icon_box, self.TASK_VALUE_OFFSETS["task_name"])
        reco = context.run_recognition(
            "Hud_TaskNameOcr",
            screenshot,
            pipeline_override={"Hud_TaskNameOcr": {"roi": roi}},
        )
        if reco.hit and reco.best_result:
            task_info.task_name = reco.best_result.text.strip()

        # 识别敌人等级
        roi = self._calc_dynamic_roi(icon_box, self.TASK_VALUE_OFFSETS["task_level"])
        reco = context.run_recognition(
            "Hud_TaskLevelOcr",
            screenshot,
            pipeline_override={
                "Hud_TaskLevelOcr": {
                    "roi": roi,
                }
            },
        )
        if reco.hit and reco.best_result:
            task_info.task_level = reco.best_result.text.strip()

        # 识别任务描述
        roi = self._calc_dynamic_roi(
            icon_box, self.TASK_VALUE_OFFSETS["task_description"]
        )
        reco = context.run_recognition(
            "Hud_TaskDescOcr",
            screenshot,
            pipeline_override={"Hud_TaskDescOcr": {"roi": roi}},
        )
        if reco.hit and reco.all_results:
            # 合并多行描述
            texts = [r.text.strip() for r in reco.all_results]
            task_info.task_description = "".join(texts)

        # 识别接受按钮位置
        roi = self._calc_dynamic_roi(icon_box, self.TASK_VALUE_OFFSETS["accept_button"])
        reco = context.run_recognition(
            "Hud_AcceptButtonOcr",
            screenshot,
            pipeline_override={
                "Hud_AcceptButtonOcr": {
                    "roi": roi,
                }
            },
        )
        if reco.hit and reco.best_result:
            task_info.accept_button_box = reco.best_result.box
            task_info.action_text = reco.best_result.text.strip()

        return task_info

    def recognize_and_get_best_task(
        self, context: Context, screenshot, max_level: int = None
    ) -> Optional[HudTaskInfo]:
        """识别所有任务，返回符合筛选条件的最佳任务

        Args:
            context: MAA上下文
            screenshot: 截图
            max_level: 最大敌人等级阈值（低于此值才考虑）

        Returns:
            符合条件且评分最高的任务，或None
        """
        # 1. 扫描所有任务图标
        task_list = self.recognize_tasks(context, screenshot)
        if not task_list:
            logger.info("未找到任何任务图标")
            return None

        # 3. 记录所有任务信息（调试用）
        for task in task_list:
            level = self._parse_level(task.task_level)
            logger.debug(
                f"任务: {task.task_name} | {task.task_type} | {level}级 | {task.task_description}"
            )

        # 4. 筛选任务（只按等级筛选）
        filtered = self._filter_tasks(task_list, max_level)
        if not filtered:
            return None

        # 5. 返回等级最高的符合条件的任务
        best = max(filtered, key=lambda t: self._parse_level(t.task_level))
        level = self._parse_level(best.task_level)
        logger.info(f"选择任务: {best.task_name} | {best.task_type} | {level}级 | {best.task_description}")
        return best

    def _filter_tasks(
        self, task_list: List[HudTaskInfo], max_level: int
    ) -> List[HudTaskInfo]:
        """根据等级阈值和黑名单筛选任务"""
        filtered = []
        for task in task_list:
            # 检查任务名称是否为空
            if not task.task_name:
                logger.debug(f"任务名称为空，跳过")
                continue

            # 检查任务类型黑名单（硬编码规则）
            if task.task_type in self.TASK_TYPE_BLACKLIST:
                logger.debug(f"任务类型 {task.task_type} 在黑名单中，跳过")
                continue

            matched_keyword = self.non_combat_keyword(task)
            if matched_keyword is not None:
                logger.debug(
                    f"任务 {task.task_name} 描述含非战斗关键词 "
                    f"{matched_keyword}，跳过"
                )
                continue

            # 检查任务名称黑名单（从文件加载）
            if self.blacklist.is_in_blacklist(task.task_name):
                logger.debug(f"任务 {task.task_name} 在黑名单中，跳过")
                continue

            # 解析等级
            level = self._parse_level(task.task_level)
            if max_level and level > max_level:
                logger.info(
                    f"任务 {task.task_name} 等级 {level} 超过阈值 {max_level}，跳过"
                )
                continue

            filtered.append(task)
        return filtered

    def _parse_level(self, level_str: str) -> int:
        """从等级字符串中提取数字"""
        if not level_str:
            return 0
        # "敌人等级：75" -> 75

        match = re.search(r"\d+", level_str)
        return int(match.group()) if match else 0
