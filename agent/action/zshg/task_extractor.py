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
    abandon_button_box: Optional[Rect] = None


class TaskExtractor:
    def __init__(self, roi: List[int] = None):
        self.roi = roi or [0, 0, 1920, 1080]
        self.roi_rect = Rect(*self.roi)
        self.accept_buttons = []
        self.task_names_file = "assets/task_names.json"
        self.known_task_names = self._load_task_names()
        self.task_blacklist_file = "assets/task_blacklist.txt"
        self.task_blacklist = self._load_task_blacklist()

    def _load_task_blacklist(self):
        try:
            import os

            if os.path.exists(self.task_blacklist_file):
                with open(self.task_blacklist_file, "r", encoding="utf-8") as f:
                    return set(line.strip() for line in f if line.strip())
            return set()
        except Exception:
            return set()

    def extract_tasks(self, ocr_results: List) -> List[TaskInfo]:
        if not ocr_results:
            return []

        # 预处理：先缓存所有接受和放弃按钮，同时过滤无效文本
        filtered_results = []
        self.accept_buttons = []
        self.abandon_buttons = []
        for res in ocr_results:
            text = self._get_text(res).strip()
            if not text:
                continue
            if "接受" in text:
                self.accept_buttons.append(
                    (
                        self._get_box_y(self._get_box_from_result(res)),
                        self._get_box_from_result(res),
                    )
                )
            elif "放弃" in text:
                self.abandon_buttons.append(
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
            if task and task.task_name not in self.task_blacklist:
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

    def _load_task_names(self):
        """从文件加载已识别的任务名称"""
        try:
            import os
            import json

            # 检查文件是否存在
            if os.path.exists(self.task_names_file):
                with open(self.task_names_file, "r", encoding="utf-8") as f:
                    task_names = json.load(f)
                    return set(task_names)
            else:
                # 文件不存在，创建空文件
                os.makedirs(os.path.dirname(self.task_names_file), exist_ok=True)
                with open(self.task_names_file, "w", encoding="utf-8") as f:
                    json.dump([], f, ensure_ascii=False, indent=2)
                return set()
        except Exception:
            return set()

    def _save_task_names(self):
        """保存已识别的任务名称到文件"""
        try:
            import os
            import json

            # 确保目录存在
            os.makedirs(os.path.dirname(self.task_names_file), exist_ok=True)

            # 保存任务名称
            task_names_list = list(self.known_task_names)
            with open(self.task_names_file, "w", encoding="utf-8") as f:
                json.dump(task_names_list, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _is_task_name_candidate(self, text: str, y_pos: int) -> bool:
        """判断是否为任务名称候选"""
        # 文本长度判断：任务名称通常较短
        if len(text) < 2 or len(text) > 10:
            return False

        # 排除明显不是任务名称的文本
        exclude_patterns = [
            "奖励",
            "任务时限",
            "敌人等级",
            "接受",
            "放弃",
            "当前任务",
            "失败：",
            "有一",
            "一名",
            "一支",
            "一位",
            "这个",
            "委托",
            "有-",
            "-300",
            "x",
            "X",
            "×",
        ]
        for pattern in exclude_patterns:
            if pattern in text:
                return False

        # 排除纯数字和带有数字前缀的文本（如"x328"、"400"等）
        if text.isdigit():
            return False
        if text.startswith(("x", "X", "×")) and len(text) > 1 and text[1:].isdigit():
            return False

        # 检查是否为已知任务名称
        if text in self.known_task_names:
            return True

        # 基于位置和上下文的启发式判断
        # 这里可以根据实际情况调整位置阈值
        if y_pos < 300 or y_pos > 1200:
            return False

        return True

    def _group_by_task_name(self, sorted_results: List) -> List[List]:
        """以任务名称为分隔符，精准拆分任务"""
        groups = []
        current_group = []

        for i, res in enumerate(sorted_results):
            text = self._get_text(res).strip()
            box = self._get_box_from_result(res)
            box_y = self._get_box_y(box)

            # 检查是否为有效的任务名称候选
            if self._is_task_name_candidate(text, box_y):
                # 检查是否与前一个元素有较大的垂直间距（任务分隔）
                if current_group:
                    prev_res = sorted_results[i - 1]
                    prev_box = self._get_box_from_result(prev_res)
                    prev_box_y = self._get_box_y(prev_box)

                    # 如果垂直间距大于阈值，认为是新任务
                    if box_y - prev_box_y > 50:
                        # 将新识别的任务名称添加到已知列表
                        self.known_task_names.add(text)
                        # 保存到文件
                        self._save_task_names()

                        groups.append(current_group)
                        current_group = [res]
                    else:
                        # 否则认为是当前任务的一部分
                        current_group.append(res)
                else:
                    # 第一个元素，直接作为新任务的开始
                    self.known_task_names.add(text)
                    self._save_task_names()
                    current_group = [res]
            else:
                # 非任务名称，添加到当前组
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
        abandon_button_box = None

        # 提取当前任务的Y范围（用于匹配对应接受按钮）
        group_min_y = self._get_box_y(self._get_box_from_result(ocr_results[0]))
        group_max_y = self._get_box_y(self._get_box_from_result(ocr_results[-1]))

        # 存储每个元素的位置信息，用于更好地判断上下文
        elements = []
        for result in ocr_results:
            text = self._get_text(result).strip()
            box = self._get_box_from_result(result)
            box_y = self._get_box_y(box)
            elements.append((text, box_y, box))

        # 按Y坐标排序元素
        elements.sort(key=lambda x: x[1])

        # 提取任务信息
        for i, (text, box_y, box) in enumerate(elements):
            # 提取任务名称（通常是第一个有效的候选）
            if not task_name and self._is_task_name_candidate(text, box_y):
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
            # 提取奖励（匹配x开头的数值或纯数字，通常在"奖励："附近）
            elif (
                text.startswith(("x", "X", "×"))
                and len(text) > 1
                and text[1:].isdigit()
            ) or text.isdigit():
                # 检查是否在"奖励："附近
                has_reward_label = False
                for j in range(max(0, i - 5), min(len(elements), i + 5)):
                    if "奖励：" in elements[j][0]:
                        has_reward_label = True
                        break
                if has_reward_label:
                    reward.append(text)

        # 匹配当前任务对应的接受按钮（Y轴在任务范围内）
        for btn_y, btn_box in self.accept_buttons:
            if group_min_y <= btn_y <= group_max_y:
                accept_button_box = self._get_box(btn_box)
                break

        # 匹配当前任务对应的放弃按钮（Y轴在任务范围内）
        for btn_y, btn_box in self.abandon_buttons:
            if group_min_y <= btn_y <= group_max_y:
                abandon_button_box = self._get_box(btn_box)
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
            abandon_button_box=abandon_button_box,
        )

    def _is_description(self, text: str) -> bool:
        """判断是否为任务描述"""
        description_starts = [
            "有一名",
            "有一",
            "一名",
            "一支",
            "必须",
            "委托",
            "尽快",
            "一位",
            "这个",
        ]
        for pattern in description_starts:
            if text.startswith(pattern):
                return True
        return len(text) >= 15

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
            if task.abandon_button_box:
                logger.info(f"放弃按钮位置: {task.abandon_button_box}")
            logger.info("-" * 50)
