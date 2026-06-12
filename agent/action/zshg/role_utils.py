"""
角色信息识别工具模块
用于识别角色的潜力、血脉、特性等信息
"""

from dataclasses import dataclass
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
import re
import time
import json
import os


@dataclass
class Potential:
    """潜力结构体，用于存储角色的六维属性"""

    values: dict = None  # 属性名 -> 数值，如 {"力量": -0.1874, "体质": 0.1811}

    def __post_init__(self):
        if self.values is None:
            self.values = {
                "力量": 0.0,
                "体质": 0.0,
                "技巧": 0.0,
                "感知": 0.0,
                "敏捷": 0.0,
                "意志": 0.0,
            }


@dataclass
class Bloodline:
    """血脉结构体，用于存储角色的血统信息"""

    bloodlines: dict = None  # 血统名 -> 百分比，如 {"瓦诺遗族": 80, "高阶精灵": 20}

    def __post_init__(self):
        if self.bloodlines is None:
            self.bloodlines = {}


@dataclass
class Feature:
    """特性结构体，用于存储角色的特性信息"""

    name: str = ""  # 特性名称
    description: str = ""  # 特性描述


# 天赋面板相对坐标
PanelPropertyTable = {
    "力量": {"attr_offset": [115, 58], "val_offset": [100, 88]},
    "体质": {"attr_offset": [350, 58], "val_offset": [365, 88]},
    "技巧": {"attr_offset": [55, 169], "val_offset": [60, 198]},
    "感知": {"attr_offset": [410, 173], "val_offset": [410, 198]},
    "敏捷": {"attr_offset": [115, 281], "val_offset": [120, 310]},
    "意志": {"attr_offset": [350, 279], "val_offset": [350, 310]},
}

# 血脉面板识别配置
BLOODLINE_CONFIG = {
    "name_offset_x": -10,
    "name_offset_y": -10,
    "name_width": 200,
    "percent_offset_x": 548,
    "percent_offset_y": 50,
    "percent_width": 73,
    "percent_height_reduction": 50,
}


def extract_potential(context: Context) -> Potential:
    """
    提取角色潜力属性
    Args:
        context: MAA 上下文
    Returns:
        Potential 对象，包含六维属性
    """
    # 1. 初始化天赋面板锚点位置
    RecoDetail = context.run_recognition(
        "PanelPropertyInit", context.tasker.controller.post_screencap().wait().get()
    )
    if not RecoDetail.hit:
        logger.error("初始化天赋面板锚点位置失败")
        return Potential()

    anchor_rect = RecoDetail.box
    anchor_x, anchor_y = anchor_rect[0], anchor_rect[1]

    # 2. 识别六维属性
    result_attributes = Potential()
    for attr_name, offsets in PanelPropertyTable.items():
        # 计算属性文本和值的坐标
        attr_x = anchor_x + offsets["attr_offset"][0]
        attr_y = anchor_y + offsets["attr_offset"][1]
        val_x = anchor_x + offsets["val_offset"][0]
        val_y = anchor_y + offsets["val_offset"][1]

        # 定义 ROI (左边扩大 20 像素)
        attr_roi = [attr_x - 20, attr_y, 160, 35]  # 宽度增加 20 像素
        val_roi = [val_x - 20, val_y, 160, 35]  # 宽度增加 20 像素

        # 识别属性名
        reco_attr = context.run_recognition(
            "PanelPropertyItemCheck",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={
                "PanelPropertyItemCheck": {
                    "expected": [attr_name, attr_name[0]],
                    "roi": attr_roi,
                }
            },
        )

        # 识别属性值
        reco_val = context.run_recognition(
            "PanelPropertyNumCheck",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={"PanelPropertyNumCheck": {"roi": val_roi}},
        )

        # 存储结果
        if reco_attr.hit and reco_val.hit:
            clean_attr_name = re.sub(r"[A-E S]", "", reco_attr.best_result.text).strip()
            try:
                result_attributes.values[clean_attr_name] = float(
                    reco_val.best_result.text
                )
                logger.debug(f"识别属性 {clean_attr_name}: {reco_val.best_result.text}")
            except ValueError as e:
                logger.error(f"解析属性值失败：{reco_val.best_result.text}, {e}")

    logger.info(
        f"潜力识别完成：{[(k, f'{v:.4f}') for k, v in result_attributes.values.items()]}"
    )
    return result_attributes


def extract_bloodlines(context: Context) -> Bloodline:
    """
    提取角色血脉信息
    Args:
        context: MAA 上下文
    Returns:
        Bloodline 对象，包含血统信息
    """
    # 1. 初始化血脉面板锚点位置
    reco_blood = context.run_recognition(
        "PanelBloodInit", context.tasker.controller.post_screencap().wait().get()
    )
    if not reco_blood.hit:
        logger.error("初始化血脉面板锚点位置失败")
        return Bloodline()

    blood_anchor_rect = reco_blood.box
    blood_anchor_x, blood_anchor_y = blood_anchor_rect[0], blood_anchor_rect[1]

    # 2. 初始化特性面板锚点位置（用于计算血脉区域高度）
    reco_feature = context.run_recognition(
        "PanelFeatureInit", context.tasker.controller.post_screencap().wait().get()
    )
    if not reco_feature.hit:
        logger.error("初始化特性面板锚点位置失败")
        return Bloodline()

    feature_anchor_rect = reco_feature.box
    feature_anchor_y = feature_anchor_rect[1]

    # 3. 计算血脉面板区域
    bloodline_panel_height = feature_anchor_y - blood_anchor_y

    # 4. 计算识别区域
    name_roi = [
        blood_anchor_x + BLOODLINE_CONFIG["name_offset_x"],
        blood_anchor_y + BLOODLINE_CONFIG["name_offset_y"],
        BLOODLINE_CONFIG["name_width"],
        bloodline_panel_height,
    ]

    percent_roi = [
        blood_anchor_x + BLOODLINE_CONFIG["percent_offset_x"],
        blood_anchor_y + BLOODLINE_CONFIG["percent_offset_y"],
        BLOODLINE_CONFIG["percent_width"],
        bloodline_panel_height - BLOODLINE_CONFIG["percent_height_reduction"],
    ]

    # 5. 识别血脉名称和浓度
    reco_names = context.run_recognition(
        "PanelBloodNameCheck",
        context.tasker.controller.post_screencap().wait().get(),
        pipeline_override={"PanelBloodNameCheck": {"roi": name_roi}},
    )

    reco_percents = context.run_recognition(
        "PanelBloodPercentCheck",
        context.tasker.controller.post_screencap().wait().get(),
        pipeline_override={"PanelBloodPercentCheck": {"roi": percent_roi}},
    )

    if not (reco_names.hit and reco_percents.hit):
        logger.error("识别血脉信息失败")
        return Bloodline()

    # 6. 解析结果
    bloodline_info = Bloodline()
    name_results = [
        item
        for item in reco_names.all_results
        if item.score > 0.6 and item.text.strip() and item.text.strip() != "血统"
    ]
    percent_results = [
        item
        for item in reco_percents.all_results
        if item.score > 0.6 and item.text.strip()
    ]

    for i in range(min(len(name_results), len(percent_results))):
        name_text = name_results[i].text.strip()
        pct_text = percent_results[i].text.strip().replace("%", "")
        try:
            pct_value = float(pct_text)
            bloodline_info.bloodlines[name_text] = pct_value
            logger.debug(f"识别血脉 {name_text}: {pct_value}%")
        except ValueError:
            logger.error(f"解析百分比失败：{pct_text}")

    logger.info(f"血脉识别完成：{bloodline_info.bloodlines}")
    return bloodline_info


def extract_features(context: Context, max_swipe_count: int = 5) -> list:
    """
    提取角色特性信息
    Args:
        context: MAA 上下文
        max_swipe_count: 最大滑动次数
    Returns:
        Feature 对象列表
    """
    # 1. 初始化特性面板锚点位置
    reco_feature = context.run_recognition(
        "PanelFeatureInit", context.tasker.controller.post_screencap().wait().get()
    )
    if not reco_feature.hit:
        logger.error("初始化特性面板锚点位置失败")
        return []

    feature_anchor_rect = reco_feature.box
    feature_anchor_x, feature_anchor_y = (
        feature_anchor_rect[0],
        feature_anchor_rect[1],
    )

    # 2. 定义特性识别区域
    feature_roi = [
        feature_anchor_x,
        feature_anchor_y + feature_anchor_rect[3],
        600,
        500,
    ]

    # 3. 循环识别特性
    all_features = []
    consecutive_failures = 0
    max_consecutive_failures = 2
    swipe_count = 0

    while (
        consecutive_failures < max_consecutive_failures
        and swipe_count < max_swipe_count
    ):
        # 识别当前页特性
        reco_features = context.run_recognition(
            "PanelFeatureCheck",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={"PanelFeatureCheck": {"roi": feature_roi}},
        )

        if not reco_features.hit:
            consecutive_failures += 1
            continue

        # 解析 OCR 结果
        ocr_results = reco_features.all_results
        filtered_results = [
            item for item in ocr_results if item.score > 0.9 and item.text.strip()
        ]

        # 提取特性名称和描述
        page_features = []
        current_feature_name = None

        for item in filtered_results:
            text = item.text.strip()
            box_y = item.box[1]

            # 跳过锚点文本
            if text == "特性":
                continue

            # 特性名称：2-10 个汉字
            if (
                all("\u4e00" <= c <= "\u9fff" or c in "·" for c in text)
                and 2 <= len(text) <= 10
                and not any(
                    keyword in text
                    for keyword in ["的", "了", "后", "一", "能", "+", "%", "，", "。"]
                )
            ):
                current_feature_name = text
                page_features.append(Feature(name=current_feature_name))
            elif current_feature_name and len(text) > 10:
                # 特性描述
                for feature in page_features:
                    if not feature.description:
                        feature.description = text
                        break

        # 合并新特性
        new_features_count = 0
        for feature in page_features:
            is_new = True
            for existing in all_features:
                if existing.name == feature.name:
                    is_new = False
                    break
            if is_new:
                all_features.append(feature)
                new_features_count += 1

        if new_features_count == 0:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
            # 下滑页面
            context.run_task("PropertyPanelSwipeDown")
            time.sleep(0.5)
            swipe_count += 1

    logger.info(f"特性识别完成，共识别到 {len(all_features)} 个特性")
    logger.debug(f"特性列表：{all_features}")
    return all_features


def get_highest_bloodline(bloodline: Bloodline) -> str:
    """
    获取最高血统
    Args:
        bloodline: Bloodline 对象
    Returns:
        最高血统名称
    """
    if not bloodline.bloodlines:
        return "未知"

    # 按百分比排序，返回最高的
    sorted_bloodlines = sorted(
        bloodline.bloodlines.items(), key=lambda x: x[1], reverse=True
    )
    return sorted_bloodlines[0][0]


def get_potential_grade(value: float) -> str:
    """
    根据属性值返回等级
    Args:
        value: 属性值
    Returns:
        等级（E/D/C/B/A/S/SS）
    """
    ranges = {
        "E": (-float("inf"), 0.10),
        "D": (0.10, 0.20),
        "C": (0.20, 0.35),
        "B": (0.35, 0.55),
        "A": (0.55, 0.74),
        "S": (0.74, 0.93),
        "SS": (0.93, float("inf")),
    }

    for grade, (min_val, max_val) in ranges.items():
        if min_val <= value < max_val:
            return grade
    return "E"


def extract_all_role_info(context: Context) -> tuple:
    """
    完整识别角色信息（潜力 + 血脉 + 特性）
    这是一个通用的识别流程，适用于孩子检测和相亲苗子检测

    Args:
        context: MAA 上下文

    Returns:
        (potential, bloodline, features) 三元组
        - potential: Potential 对象
        - bloodline: Bloodline 对象
        - features: Feature 对象列表
    """

    # 1. 识别潜力属性
    logger.info("开始识别潜力属性...")
    potential = extract_potential(context)
    if not potential.values or all(v == 0.0 for v in potential.values.values()):
        logger.warning("潜力属性识别结果为空或全为 0")

    # 2. 先识别血脉信息（不先下滑）
    logger.info("开始识别血脉信息...")
    bloodline = Bloodline()
    max_retry = 3

    # 先尝试识别3次（不下滑），识别不到直接去识别特性
    for retry in range(max_retry):
        bloodline = extract_bloodlines(context)
        if bloodline.bloodlines:
            break
        logger.warning(f"血脉信息识别失败，重试 {retry + 1}/{max_retry}")
        time.sleep(0.5)

    if not bloodline.bloodlines:
        logger.warning("识别血脉信息失败，跳过，继续识别特性")

    # 3. 识别特性信息（特性面板在血脉面板下方）
    logger.info("开始识别特性信息...")
    features = extract_features(context)
    if not features:
        logger.info("未识别到特性")

    logger.info("识别完成")

    return potential, bloodline, features