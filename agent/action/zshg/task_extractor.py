from dataclasses import dataclass
from typing import List, Optional, Dict, Union
from maa.define import OCRResult, Rect

from utils import logger


@dataclass
class TaskInfo:
    task_name: str
    task_description: str
    reward: Optional[str] = None
    time_limit: Optional[str] = None
    enemy_level: Optional[str] = None
    accept_button_box: Optional[Rect] = None


class TaskExtractor:
    def __init__(self, roi: List[int] = None):
        self.roi = roi or [0, 0, 1920, 1080]
        self.roi_rect = Rect(*self.roi)
        # 新增：任务名称列表（精准匹配，避免误判）
        self.TASK_NAMES = {
            "侠盗",
            "外来者",
            "讨伐",
            "探索",
            "护送",
            "收集",
            "消灭",
            "击败",
        }
        # 新增：接受按钮缓存（用于后续匹配）
        self.accept_buttons = []

    def extract_tasks(self, ocr_results: List) -> List[TaskInfo]:
        if not ocr_results:
            return []

        # 预处理：先缓存所有接受按钮，同时过滤无效文本
        filtered_results = []
        self.accept_buttons = []
        for res in ocr_results:
            text = self._get_text(res).strip()
            if not text:
                continue
            if text == "接受":
                self.accept_buttons.append(
                    (
                        self._get_box_y(self._get_box_from_result(res)),
                        self._get_box_from_result(res),
                    )
                )
            else:
                filtered_results.append(res)

        # 按Y坐标排序（还原视觉顺序）
        sorted_results = sorted(
            filtered_results,
            key=lambda r: self._get_box_y(self._get_box_from_result(r)),
        )

        # 重新分组：以任务名称为分隔符，精准拆分任务
        task_groups = self._group_by_task_name(sorted_results)

        tasks = []
        for group in task_groups:
            task = self._extract_single_task(group)
            if task:
                tasks.append(task)

        return tasks

    def _get_box_y(self, box) -> int:
        if isinstance(box, Rect):
            return box.y
        elif isinstance(box, (list, tuple)) and len(box) >= 2:
            return box[1]
        elif hasattr(box, "y"):
            return box.y
        return 0

    def _get_box(self, box) -> Rect:
        if isinstance(box, Rect):
            return box
        elif isinstance(box, (list, tuple)) and len(box) >= 4:
            return Rect(box[0], box[1], box[2], box[3])
        return Rect(0, 0, 0, 0)

    def _get_text(self, result) -> str:
        if hasattr(result, "text"):
            return result.text
        elif isinstance(result, dict):
            return result.get("text", "")
        return ""

    def _get_box_from_result(self, result):
        if hasattr(result, "box"):
            return result.box
        elif isinstance(result, dict):
            return result.get("box", [0, 0, 0, 0])
        return [0, 0, 0, 0]

    def _group_by_task_name(self, sorted_results: List) -> List[List]:
        """以任务名称为分隔符，拆分不同任务的OCR结果"""
        groups = []
        current_group = []
        for res in sorted_results:
            text = self._get_text(res).strip()
            if text in self.TASK_NAMES:
                if current_group:
                    groups.append(current_group)
                current_group = [res]
            else:
                current_group.append(res)
        if current_group:
            groups.append(current_group)
        return groups

    def _extract_single_task(self, ocr_results: List) -> Optional[TaskInfo]:
        task_name = None
        task_description = ""
        reward = []
        time_limit = None
        enemy_level = None
        accept_button_box = None

        # 提取当前任务的Y范围（用于匹配对应接受按钮）
        group_min_y = self._get_box_y(self._get_box_from_result(ocr_results[0]))
        group_max_y = self._get_box_y(self._get_box_from_result(ocr_results[-1]))

        for result in ocr_results:
            text = self._get_text(result).strip()
            box = self._get_box_from_result(result)

            # 提取任务名称
            if text in self.TASK_NAMES:
                task_name = text
            # 提取任务描述
            elif self._is_description(text):
                task_description += text
            # 提取任务时限
            elif text.startswith("任务时限："):
                time_limit = text.replace("任务时限：", "").strip()
            # 提取敌人等级
            elif text.startswith("敌人等级："):
                enemy_level = text.replace("敌人等级：", "").strip()
            # 提取奖励（匹配x开头的数值）
            elif text.startswith(("x", "X")) and text[1:].isdigit():
                reward.append(text)
            # 提取奖励数值（纯数字）
            elif text.isdigit():
                reward.append(text)

        # 匹配当前任务对应的接受按钮（Y轴在任务范围内）
        for btn_y, btn_box in self.accept_buttons:
            if group_min_y <= btn_y <= group_max_y:
                accept_button_box = self._get_box(btn_box)
                break

        if not task_name:
            return None

        # 整理奖励格式
        reward_str = " + ".join(reward) if reward else None

        return TaskInfo(
            task_name=task_name,
            task_description=task_description,
            reward=reward_str,
            time_limit=time_limit,
            enemy_level=enemy_level,
            accept_button_box=accept_button_box,
        )

    def _is_description(self, text: str) -> bool:
        """精准匹配描述的开头关键词，避免混入其他文本"""
        description_starts = ["有一名", "还声称", "一支从", "必须", "委托", "尽快"]
        return any(text.startswith(pattern) for pattern in description_starts)

    def print_task_details(self, tasks: List[TaskInfo]):
        """输出任务详情"""

        for task in tasks:
            logger.info(f"任务名称: {task.task_name}")
            logger.info(f"任务描述: {task.task_description}")
            if task.reward:
                logger.info(f"奖励: {task.reward}")
            if task.time_limit:
                logger.info(f"时限: {task.time_limit}")
            if task.enemy_level:
                logger.info(f"敌人等级: {task.enemy_level}")
            if task.accept_button_box:
                logger.info(f"接受按钮位置: {task.accept_button_box}")
            logger.info("-" * 50)
