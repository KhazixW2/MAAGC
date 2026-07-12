from dataclasses import dataclass
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger

import re
import time
import json
import os

from .role_utils import (
    extract_potential,
    extract_bloodlines,
    extract_features,
    get_highest_bloodline,
    get_potential_grade,
    extract_all_role_info,
    Potential,
    Bloodline,
    Feature,
)

# 属性等级优先级（用于排序）
ATTRIBUTE_RANK = {
    "SS": 7,
    "S": 6,
    "A": 5,
    "B": 4,
    "C": 3,
    "D": 2,
    "E": 1,
}

# 好苗子条件配置
CHILD_ALERT_CONDITIONS = None


def load_child_alert_conditions():
    """加载好苗子条件配置"""
    global CHILD_ALERT_CONDITIONS
    if CHILD_ALERT_CONDITIONS is None:
        try:
            cwd_dir = os.getcwd()
            config_file = os.path.join(cwd_dir, "table", "child_alert_conditions.json")

            if os.path.exists(config_file):
                with open(config_file, "r", encoding="utf-8") as f:
                    CHILD_ALERT_CONDITIONS = json.load(f)
                logger.info(
                    f"好苗子条件配置加载成功，共 {len(CHILD_ALERT_CONDITIONS.get('conditions', []))} 个条件"
                )
            else:
                logger.warning(f"好苗子条件配置文件不存在: {config_file}")
                CHILD_ALERT_CONDITIONS = {"conditions": []}
        except Exception as e:
            logger.error(f"加载好苗子条件配置失败: {e}")
            CHILD_ALERT_CONDITIONS = {"conditions": []}

    return CHILD_ALERT_CONDITIONS


def check_potential_condition(potential_values: dict, condition: dict) -> tuple:
    """
    检查潜力属性是否满足指定条件
    Args:
        potential_values: 潜力属性字典 {"技巧": 0.9, "力量": 0.8, ...}
        condition: 条件配置字典
    Returns:
        (是否满足条件, 满足的属性信息)
    """
    potential_config = condition.get("potential", {})
    if not potential_config:
        return True, {}

    # 检查必需的属性
    required_attrs = {}
    or_attributes = None

    for attr_name, attr_requirement in potential_config.items():
        if attr_name == "or_attributes":
            or_attributes = attr_requirement
            continue
        grade = attr_requirement.get("grade")
        min_value = attr_requirement.get("min_value")

        current_value = potential_values.get(attr_name, 0)
        current_grade = get_potential_grade(current_value)

        # 检查等级
        if grade:
            grade_rank = ATTRIBUTE_RANK.get(grade, 0)
            current_rank = ATTRIBUTE_RANK.get(current_grade, 0)
            if current_rank < grade_rank:
                return False, {}

        # 检查最小值
        if min_value is not None and current_value < min_value:
            return False, {}

        required_attrs[attr_name] = (current_value, current_grade)

    # 检查or_attributes条件
    if or_attributes:
        or_satisfied = False
        for or_group in or_attributes:
            group_satisfied = True
            for attr_name, attr_requirement in or_group.items():
                min_value = attr_requirement.get("min_value")
                current_value = potential_values.get(attr_name, 0)

                if min_value is not None and current_value < min_value:
                    group_satisfied = False
                    break
                else:
                    current_grade = get_potential_grade(current_value)
                    required_attrs[attr_name] = (current_value, current_grade)

            if group_satisfied:
                or_satisfied = True
                break

        if not or_satisfied:
            return False, {}

    return True, required_attrs


def check_feature_condition(features: list, condition: dict) -> tuple:
    """
    检查特性是否满足指定条件
    Args:
        features: Feature对象列表
        condition: 条件配置字典
    Returns:
        (是否满足条件, 满足的特性列表)
    """
    feature_config = condition.get("features", [])
    if not feature_config:
        return True, []

    feature_names = [f.name for f in features]
    found_features = []

    for required_feature in feature_config:
        for feature_name in feature_names:
            if required_feature in feature_name:
                found_features.append(feature_name)
                break

    return len(found_features) >= len(feature_config), found_features


def generate_child_name(
    potential,
    bloodline,
    features: list,
    highest_title: str,
    child_index: int = 1,
) -> str:
    """
    生成子孙命名
    格式：最高属性 + 次高属性 + 特性 + 爵位
    例如：力 ss 技 ss 太科公.2
    """
    # 1. 找出最高和次高属性
    attributes = []
    for attr_name, attr_value in potential.values.items():
        grade = get_potential_grade(attr_value)
        attributes.append((attr_name, attr_value, grade))

    # 按属性值排序
    attributes.sort(key=lambda x: x[1], reverse=True)

    # 获取最高和次高属性
    if len(attributes) >= 2:
        top_attr = attributes[0]
        second_attr = attributes[1]

        # 属性名称取第一个汉字
        top_attr_short = top_attr[0][0]  # 如"力"
        second_attr_short = second_attr[0][0]  # 如"技"

        # 等级转小写
        top_grade = top_attr[2].lower()  # 如"ss"
        second_grade = second_attr[2].lower()  # 如"ss"

        attr_part = f"{top_attr_short}{top_grade}{second_attr_short}{second_grade}"
    else:
        attr_part = ""

    # 2. 提取特性（跳过隐藏特性）
    feature_chars = []
    for feature in features:
        if "隐藏" not in feature.name:
            # 取特性名称第一个汉字
            feature_chars.append(feature.name[0])

    feature_part = "".join(feature_chars[:3])  # 最多取 3 个特性

    # 3. 爵位处理：第三个及以后出生的孩子使用"骑"
    if child_index >= 3:
        title_short = "骑"
    else:
        title_short = highest_title[0] if highest_title else ""

    # 4. 组合命名
    name = f"{attr_part}{feature_part}{title_short}"

    return name


def evaluate_potential(potential) -> tuple:
    """
    评估潜力属性，判断是否满足好苗子条件
    条件：技巧SS + 力量S + 意志或敏捷有一个0.3以上
    Args:
        potential: Potential 对象
    Returns:
        (是否为好苗子, 属性详情信息)
    """
    values = potential.values

    ji_qiao = values.get("技巧", 0)
    li_liang = values.get("力量", 0)
    yi_zhi = values.get("意志", 0)
    min_jie = values.get("敏捷", 0)

    ji_qiao_grade = get_potential_grade(ji_qiao)
    li_liang_grade = get_potential_grade(li_liang)
    yi_zhi_grade = get_potential_grade(yi_zhi)
    min_jie_grade = get_potential_grade(min_jie)

    # 检查是否满足条件：技巧SS + 力量S + 意志或敏捷有一个0.3以上
    is_good = (
        ji_qiao_grade == "SS"
        and li_liang_grade == "S"
        and (yi_zhi >= 0.3 or min_jie >= 0.3)
    )

    detail_info = {
        "技巧": (ji_qiao, ji_qiao_grade),
        "力量": (li_liang, li_liang_grade),
        "意志": (yi_zhi, yi_zhi_grade),
        "敏捷": (min_jie, min_jie_grade),
    }

    return is_good, detail_info


def evaluate_with_config(potential, features, enabled_condition_ids=None) -> tuple:
    """
    根据配置文件评估是否为好苗子
    支持多种条件配置，满足任一条件即触发提醒
    Args:
        potential: Potential 对象
        features: Feature 对象列表
        enabled_condition_ids: 只检查指定的条件 ID 列表，如果为 None 则检查所有启用条件
    Returns:
        (是否为好苗子, 满足的条件信息, 匹配到的条件名称)
    """
    config = load_child_alert_conditions()
    conditions = config.get("conditions", [])

    potential_values = potential.values

    for condition in conditions:
        condition_id = condition.get("id", "unknown")

        if enabled_condition_ids is not None:
            if condition_id not in enabled_condition_ids:
                continue
        else:
            if not condition.get("enabled", True):
                continue

        condition_name = condition.get("name", condition_id)

        potential_ok, potential_detail = check_potential_condition(
            potential_values, condition
        )

        feature_ok, found_features = check_feature_condition(features, condition)

        if potential_ok and feature_ok:
            detail_info = {}
            for attr_name, (attr_value, grade) in potential_detail.items():
                detail_info[attr_name] = (attr_value, grade)

            logger.info(f"好苗子条件匹配成功: {condition_name}")
            return (
                True,
                {
                    "potential": detail_info,
                    "features": found_features,
                    "condition_name": condition_name,
                },
                condition_name,
            )

    return False, {}, ""


# 爵位等级
title_rank: dict = {
    "公爵": 4,
    "伯爵": 3,
    "男爵": 2,
    "骑士": 1,
    "无爵位": 0,
}


@dataclass
class ParentInfo:
    """
    父母信息结构体
    """

    name: str = ""  # 姓名
    title: str = ""  # 爵位（男爵、伯爵、公爵、骑士、无爵位）
    mercenary_group: str = ""  # 佣兵团


@AgentServer.custom_action("ChildRec")
class ChildRec(CustomAction):
    """
    识别子项信息
    """

    def __init__(self, child_alert_enabled: bool = True) -> None:
        """
        Args:
            child_alert_enabled: 是否启用好苗子提醒功能，默认关闭
        """
        super().__init__()
        self.potential = None
        self.bloodline = None
        self.features = []
        # 好苗子提醒开关
        self.child_alert_enabled = child_alert_enabled

    def extract_parent_info(
        self, context: Context, is_father: bool = True
    ) -> ParentInfo:
        """
        提取父母信息
        Args:
            context: MAA 上下文
            is_father: True 为父亲，False 为母亲
        """
        parent_info = ParentInfo()

        # 选择识别任务
        title_task = "PannelFatherTitle" if is_father else "PannelMotherTitle"
        parent_type = "父亲" if is_father else "母亲"

        # 识别父母信息
        reco_result = context.run_recognition(
            title_task,
            context.tasker.controller.post_screencap().wait().get(),
        )

        if not reco_result.hit:
            logger.error(f"识别{parent_type}信息失败")
            return parent_info

        # 解析 OCR 结果
        ocr_results = reco_result.all_results
        filtered_results = [
            item for item in ocr_results if item.score > 0.5 and item.text.strip()
        ]

        # 提取姓名、爵位、佣兵团
        name_candidates = []
        for item in filtered_results:
            text = item.text.strip()

            # 判断爵位
            if any(title in text for title in ["男爵", "伯爵", "公爵", "骑士"]):
                if "公爵" in text:
                    parent_info.title = "公爵"
                elif "伯爵" in text:
                    parent_info.title = "伯爵"
                elif "男爵" in text:
                    parent_info.title = "男爵"
                elif "骑士" in text:
                    parent_info.title = "骑士"
                # 同时提取姓名（如"慧根女公爵"中的"慧根女"）
                name_part = (
                    text.replace("公爵", "")
                    .replace("伯爵", "")
                    .replace("男爵", "")
                    .replace("骑士", "")
                )
                if name_part:
                    name_candidates.append(name_part)
            # 佣兵团：通常是英文或短文本
            elif len(text) <= 10 and any(c.isascii() for c in text):
                parent_info.mercenary_group = text
            # 姓名：剩余的汉字文本
            elif all("\u4e00" <= c <= "\u9fff" for c in text) and 2 <= len(text) <= 10:
                name_candidates.append(text)

        # 选择最合适的姓名
        if name_candidates:
            parent_info.name = name_candidates[0]

        # 如果没有识别到爵位，设置为"无爵位"
        if not parent_info.title:
            parent_info.title = "无爵位"

        return parent_info

    def compare_parent_titles(
        self, father_info: ParentInfo, mother_info: ParentInfo
    ) -> tuple:
        """
        比较父母爵位等级
        Returns:
            (最高爵位，数量)
        """
        title_rank = {
            "公爵": 4,
            "伯爵": 3,
            "男爵": 2,
            "骑士": 1,
            "无爵位": 0,
        }

        father_rank = title_rank.get(father_info.title, 0)
        mother_rank = title_rank.get(mother_info.title, 0)

        if father_rank > mother_rank:
            return father_info.title, 1
        elif mother_rank > father_rank:
            return mother_info.title, 1
        else:
            return father_info.title, 2

    def _get_enabled_condition_ids(self, context: Context) -> list:
        """
        从 child_info.json 中获取所有启用状态为 true 的检测项的 expected 值
        这些值对应对 child_alert_conditions.json 中的条件 ID
        Args:
            context: MAA 上下文
        Returns:
            启用的条件 ID 列表
        """
        enabled_ids = []

        detection_keys = ["检测_科内塔之怒", "检测_太阳+科内塔之怒"]

        for key in detection_keys:
            node_data = context.get_node_data(key)
            if node_data and node_data.get("enabled", False):
                expected = (
                    node_data.get("recognition", {})
                    .get("param", {})
                    .get("expected", [])
                )
                if expected:
                    enabled_ids.append(expected[0])
                    logger.info(f"好苗子检测项启用: {key} -> {expected[0]}")

        if not enabled_ids:
            logger.info("未启用任何好苗子检测项")

        return enabled_ids

    def _check_and_show_good_child_alert(
        self,
        context: Context,
        child_name: str,
        highest_title: str,
        child_index: int,
    ) -> bool:
        """
        检查是否为好苗子并处理
        Args:
            context: MAA 上下文
            child_name: 建议的孩子名字
            highest_title: 最高爵位
            child_index: 孩子序号
        Returns:
            True - 是好苗子（阻塞等待用户处理）
            False - 不是好苗子（调用方应自动命名）
        """
        enabled_condition_ids = self._get_enabled_condition_ids(context)

        is_good, match_info, condition_name = evaluate_with_config(
            self.potential, self.features, enabled_condition_ids
        )

        if not is_good:
            return False

        potential_detail = match_info.get("potential", {})
        good_feature_list = match_info.get("features", [])

        s_count = sum(
            1 for val, grade in potential_detail.values() if grade in ["S", "SS"]
        )

        alert_content = f"🎉 发现好苗子！\n\n"

        if condition_name:
            alert_content += f"【匹配条件】{condition_name}\n\n"

        alert_content += "【潜力属性】\n"
        for attr_name, (attr_value, grade) in potential_detail.items():
            alert_content += f"{attr_name}: {grade} ({attr_value:.4f})\n"
        alert_content += f"\nS及以上属性个数: {s_count}\n"

        alert_content += "\n【特性】\n"
        if good_feature_list:
            alert_content += "好特性：" + ", ".join(good_feature_list) + "\n"
        else:
            alert_content += "无好特性\n"

        alert_content += "\n【血脉】\n"
        if self.bloodline.bloodlines:
            for blood_name, blood_percent in self.bloodline.bloodlines.items():
                alert_content += f"{blood_name}: {blood_percent}%\n"
        else:
            alert_content += "无血脉信息\n"

        alert_content += f"\n【其他信息】\n"
        alert_content += f"推荐名字: {child_name}\n"
        alert_content += f"最高爵位: {highest_title}\n"
        alert_content += f"孩子序号: 第{child_index}个\n\n"
        alert_content += "请在游戏中手动为好苗子命名！"

        context.run_task(
            "UI_PopInform",
            pipeline_override={"Node.Action.Succeeded": {"content": alert_content}},
        )
        logger.info("好苗子，弹出弹窗，等待用户手动处理")
        context.tasker.post_stop()
        return True

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        # 0.佣兵生娃

        # 1.识别父母信息
        father_info = self.extract_parent_info(context, is_father=True)
        mother_info = self.extract_parent_info(context, is_father=False)

        highest_title, count = self.compare_parent_titles(father_info, mother_info)
        logger.info(f"最高爵位是{highest_title}，最高爵位有{count}个")
        # 1.1 识别是第几个出生的孩子，只有前两个才有公爵，后面的都没有公爵了
        child_index = context.run_recognition(
            "PannelGetChildIndex",
            context.tasker.controller.post_screencap().wait().get(),
        )
        if not child_index.hit:
            logger.error("识别第几个出生的孩子失败")
            return CustomAction.RunResult(success=False)

        # 提取数字（使用正则表达式）
        ocr_text = child_index.best_result.text.strip()
        match = re.search(r"第(\d+)个孩子", ocr_text)
        if not match:
            logger.error(f"无法从识别结果中提取数字：{ocr_text}")
            return CustomAction.RunResult(success=False)
        child_index = int(match.group(1))
        logger.info(f"是第{child_index}个出生")

        # 2. 提取天赋属性、血脉、特性（完整识别流程）
        context.run_task("PannelChildInfoButton")
        self.potential, self.bloodline, self.features = extract_all_role_info(context)

        if not self.potential.values or all(
            v == 0.0 for v in self.potential.values.values()
        ):
            logger.error("提取子项属性失败")
            return CustomAction.RunResult(success=False)

        if not self.bloodline.bloodlines:
            logger.error("提取血脉信息失败")
            return CustomAction.RunResult(success=False)

        # 5. 生成子孙命名
        # 注意：这里需要在 extract_attributes 中保存 potential 对象
        child_name = generate_child_name(
            self.potential, self.bloodline, self.features, highest_title, child_index
        )
        logger.info(f"子孙命名：{child_name}")

        # 6. 检查是否为好苗子，如果是则弹窗或记录
        if self._check_and_show_good_child_alert(
            context, child_name, highest_title, child_index
        ):
            return CustomAction.RunResult(success=True)

        # 7. 非好苗子或好苗子但关闭弹窗，自动输入命名
        context.run_task("BackButton_500ms")
        context.run_task(
            "PannelChildSetName",
            pipeline_override={"PannelChildSetNameCopy": {"input_text": child_name}},
        )

        # 8.识别是否无效
        if context.run_recognition("CheckNameInvalid",context.tasker.controller.post_screencap().wait().get()):
            context.run_task("CheckNameInvalid")

        return CustomAction.RunResult(success=True)
