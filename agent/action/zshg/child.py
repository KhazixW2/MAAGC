from dataclasses import dataclass
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger

import re
import time

# 血脉面板识别配置
BLOODLINE_CONFIG = {
    "name_offset_x": -10,
    "name_offset_y": -10,
    "name_width": 127,
    "percent_offset_x": 548,
    "percent_offset_y": 50,
    "percent_width": 73,
    "percent_height_reduction": 50,  # 减去一些余量
}

# 修正后的属性成长区间表（基于原描述）
ATTRIBUTE_GROWTH_RANGES = {
    "E": (-float("inf"), 0.10),
    "D": (0.10, 0.20),
    "C": (0.20, 0.35),
    "B": (0.35, 0.55),
    "A": (0.55, 0.74),
    "S": (0.74, 0.93),
    "SS": (0.93, float("inf")),
}

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


def get_attribute_grade(value: float) -> str:
    """
    根据属性值返回等级（E, D, C, B, A, S, SS）
    """
    for grade, (min_val, max_val) in ATTRIBUTE_GROWTH_RANGES.items():
        if min_val <= value < max_val:
            return grade
    return "E"


def generate_child_name(
    potential,
    bloodline,
    features: list,
    highest_title: str,
) -> str:
    """
    生成子孙命名
    格式：最高属性 + 次高属性 + 特性 + 爵位
    例如：力 ss 技 ss 太科公.2
    """
    # 1. 找出最高和次高属性
    attributes = []
    for attr_name, attr_value in potential.values.items():
        grade = get_attribute_grade(attr_value)
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

    # 3. 爵位取第一个汉字
    title_short = highest_title[0] if highest_title else ""

    # 4. 组合命名
    name = f"{attr_part}{feature_part}{title_short}"

    return name


# 天赋面板相对坐标
PanelPropertyTable = {
    "力量": {"attr_offset": [115, 58], "val_offset": [100, 88]},
    "体质": {"attr_offset": [350, 58], "val_offset": [365, 88]},
    "技巧": {"attr_offset": [55, 169], "val_offset": [60, 198]},
    "感知": {"attr_offset": [410, 173], "val_offset": [410, 198]},
    "敏捷": {"attr_offset": [115, 281], "val_offset": [120, 310]},
    "意志": {"attr_offset": [350, 279], "val_offset": [350, 310]},
}

# 爵位等级
title_rank: dict = {
    "公爵": 4,
    "伯爵": 3,
    "男爵": 2,
    "骑士": 1,
    "无爵位": 0,
}

# 修正后的属性成长区间表（基于原描述）
ATTRIBUTE_GROWTH_RANGES_CORRECTED = {
    # 基于搜索结果整理，与游戏内潜力图对应
    "E": (-float("inf"), 0.10, "负成长或极低成长"),
    "D": (0.10, 0.20, "基础属性成长"),
    "C": (0.20, 0.35, "常见普通属性"),
    "B": (0.35, 0.55, "良好属性"),  # 您要的B属性范围是0.41-0.55
    "A": (0.55, 0.74, "优秀属性"),
    "S": (0.74, 0.93, "稀有属性"),
    "SS": (0.93, float("inf"), "非常稀有"),
    "SSS": (1.20, float("inf"), "极其稀有"),  # 实际与SS相同，可能有更高的未知上限
}

# 属性等级颜色映射（通常游戏UI使用）
ATTRIBUTE_COLORS = {
    "SSS": "#FF4500",  # 橙红色
    "SS": "#FF8C00",  # 橙色
    "S": "#FFD700",  # 金色
    "A": "#1E90FF",  # 亮蓝色
    "B": "#32CD32",  # 亮绿色
    "C": "#228B22",  # 绿色
    "D": "#808080",  # 灰色
    "E": "#696969",  # 深灰色
}


@dataclass
class Potential:
    """
    潜力结构体，用于存储子项的六维属性
    """

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
    """
    血脉结构体，用于存储子项的血统信息
    """

    bloodlines: dict = None  # 血统名 -> 百分比，如 {"瓦诺遗族": 80, "高阶精灵": 20}

    def __post_init__(self):
        if self.bloodlines is None:
            self.bloodlines = {}


@dataclass
class Feature:
    """
    特性结构体，用于存储子项的特性信息
    """

    name: str = ""  # 特性名称
    description: str = ""  # 特性描述


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

    def __init__(self) -> None:
        """ """
        super().__init__()
        self.potential = None
        self.bloodline = None
        self.features = []

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

        # logger.info(
        #     f"{parent_type}信息：姓名={parent_info.name}, 爵位={parent_info.title}, "
        #     f"佣兵团={parent_info.mercenary_group}"
        # )

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

    def extract_attributes(self, context: Context) -> bool:
        """
        提取子项属性
        """
        # 2.1 初始化天赋面板锚点位置
        AnChorRect = {}
        RecoDetail = context.run_recognition(
            "PanelPropertyInit", context.tasker.controller.post_screencap().wait().get()
        )
        if RecoDetail.hit:
            # logger.info("初始化天赋面板锚点位置成功")
            AnChorRect = RecoDetail.box  # 获取锚点的边界框 [x, y, width, height]
        else:
            logger.error("初始化天赋面板锚点位置失败")
            return False

        # 2.2 根据天赋面板锚点位置识别子项属性
        result_attributes: Potential = Potential()  # 用于存储最终结果
        for attr_name, offsets in PanelPropertyTable.items():
            # 计算属性文本和其对应值的精确坐标
            anchor_x, anchor_y = AnChorRect[0], AnChorRect[1]

            # 属性文本的坐标 (左上角)
            attr_x = anchor_x + offsets["attr_offset"][0]
            attr_y = anchor_y + offsets["attr_offset"][1]

            # 属性值的坐标 (左上角)
            val_x = anchor_x + offsets["val_offset"][0]
            val_y = anchor_y + offsets["val_offset"][1]

            # 定义裁剪区域 (roi) [x, y, width, height]
            attr_roi = [attr_x, attr_y, 120, 35]  # 属性文本的识别区域
            val_roi = [val_x, val_y, 120, 35]  # 属性值的识别区域

            # 运行识别任务，并传入裁剪区域
            reco_attr = context.run_recognition(
                "PanelPropertyItemCheck",
                context.tasker.controller.post_screencap().wait().get(),
                pipeline_override={
                    "PanelPropertyItemCheck": {
                        "expected": [attr_name, attr_name[0]],  # 匹配属性全称或第一个字
                        "roi": attr_roi,
                    }
                },
            )

            reco_val = context.run_recognition(
                "PanelPropertyNumCheck",
                context.tasker.controller.post_screencap().wait().get(),
                pipeline_override={"PanelPropertyNumCheck": {"roi": val_roi}},
            )

            # 检查结果并存储
            if reco_attr.hit and reco_val.hit:
                # 提取纯属性名（去掉等级 A,B,C,D,E,S）
                clean_attr_name = re.sub(
                    r"[A-E S]", "", reco_attr.best_result.text
                ).strip()

                # 存储结果
                result_attributes.values[clean_attr_name] = float(
                    reco_val.best_result.text
                )
        print(result_attributes)
        # 保存潜力对象
        self.potential = result_attributes
        # 5. 返回成功结果，并附上解析出的数据
        return True

    def extract_bloodlines(self, context: Context) -> bool:
        """
        提取子项血脉信息
        """
        # 3.1 初始化血脉面板锚点位置
        blood_anchor_rect = None
        reco_blood = context.run_recognition(
            "PanelBloodInit", context.tasker.controller.post_screencap().wait().get()
        )
        if reco_blood.hit:
            blood_anchor_rect = reco_blood.box
        else:
            logger.error("初始化血脉面板锚点位置失败")
            return False

        # 3.2 初始化特性面板锚点位置（用于计算血脉区域高度）
        feature_anchor_rect = None
        reco_feature = context.run_recognition(
            "PanelFeatureInit", context.tasker.controller.post_screencap().wait().get()
        )
        if reco_feature.hit:
            feature_anchor_rect = reco_feature.box
        else:
            logger.error("初始化特性面板锚点位置失败")
            return False

        # 3.3 计算血脉面板区域（血脉锚点到特性锚点之间的区域）
        blood_anchor_x, blood_anchor_y = blood_anchor_rect[0], blood_anchor_rect[1]
        feature_anchor_y = feature_anchor_rect[1]

        # 血脉面板高度 = 特性锚点 Y - 血脉锚点 Y
        bloodline_panel_height = feature_anchor_y - blood_anchor_y

        # 3.4 计算血脉名称识别区域（动态高度）
        name_roi = [
            blood_anchor_x + BLOODLINE_CONFIG["name_offset_x"],
            blood_anchor_y + BLOODLINE_CONFIG["name_offset_y"],
            BLOODLINE_CONFIG["name_width"],
            bloodline_panel_height,
        ]

        # 3.5 计算血脉浓度识别区域（动态高度）
        percent_roi = [
            blood_anchor_x + BLOODLINE_CONFIG["percent_offset_x"],
            blood_anchor_y + BLOODLINE_CONFIG["percent_offset_y"],
            BLOODLINE_CONFIG["percent_width"],
            bloodline_panel_height - BLOODLINE_CONFIG["percent_height_reduction"],
        ]

        # 3.6 识别血脉名称
        reco_names = context.run_recognition(
            "PanelBloodNameCheck",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={"PanelBloodNameCheck": {"roi": name_roi}},
        )

        # 3.7 识别血脉浓度
        reco_percents = context.run_recognition(
            "PanelBloodPercentCheck",
            context.tasker.controller.post_screencap().wait().get(),
            pipeline_override={"PanelBloodPercentCheck": {"roi": percent_roi}},
        )

        if reco_names.hit and reco_percents.hit:
            bloodline_info: Bloodline = Bloodline()

            # 获取所有识别结果（已按 Vertical 排序）
            name_results = reco_names.all_results
            percent_results = reco_percents.all_results

            # 过滤低置信度结果和无效文本
            name_results = [
                item
                for item in name_results
                if item.score > 0.9
                and item.text.strip()
                and item.text.strip() != "血统"
            ]
            percent_results = [
                item
                for item in percent_results
                if item.score > 0.9 and item.text.strip()
            ]

            # 一一对应匹配（因为都是 Vertical 排序）
            for i in range(min(len(name_results), len(percent_results))):
                name_text = name_results[i].text.strip()
                pct_text = percent_results[i].text.strip().replace("%", "")

                try:
                    pct_value = float(pct_text)
                    bloodline_info.bloodlines[name_text] = pct_value
                    logger.info(f"识别血脉 {name_text} 成功：{pct_value}%")
                except ValueError:
                    logger.error(f"解析百分比失败：{pct_text}")

            logger.info(f"血脉识别结果：{bloodline_info.bloodlines}")
            # 保存血脉对象
            self.bloodline = bloodline_info
            return True
        else:
            logger.error("识别血脉信息失败")
            return False

    def extract_features(self, context: Context) -> list[Feature]:
        """
        提取子项特性信息
        """
        # 4.1 初始化特性面板锚点位置
        feature_anchor_rect = None
        reco_feature = context.run_recognition(
            "PanelFeatureInit", context.tasker.controller.post_screencap().wait().get()
        )
        if reco_feature.hit:
            feature_anchor_rect = reco_feature.box
        else:
            logger.error("初始化特性面板锚点位置失败")
            return []

        feature_anchor_x, feature_anchor_y = (
            feature_anchor_rect[0],
            feature_anchor_rect[1],
        )

        # 4.2 定义特性识别区域
        feature_roi_first = [
            feature_anchor_x,
            feature_anchor_y + feature_anchor_rect[3],
            600,
            500,
        ]

        # 4.3 循环识别特性，直到连续两次下滑没有找到新特性
        all_features: list[Feature] = []
        consecutive_failures = 0
        max_consecutive_failures = 2
        swipe_count = 0

        while consecutive_failures < max_consecutive_failures:
            # 第一次使用动态 ROI，第二次及以后使用固定 ROI
            feature_roi = feature_roi_first

            # 识别当前页面的特性
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

                # 特性名称：通常是 4-8 个汉字的短语
                if (
                    all("\u4e00" <= c <= "\u9fff" or c in "·" for c in text)
                    and 2 <= len(text) <= 10
                    and not any(
                        keyword in text
                        for keyword in [
                            "的",
                            "了",
                            "后",
                            "一",
                            "能",
                            "+",
                            "%",
                            "，",
                            "。",
                        ]
                    )
                ):
                    # 新特性名称
                    if current_feature_name:
                        page_features.append(current_feature_name)
                    current_feature_name = Feature(name=text)
                elif current_feature_name:
                    # 特性描述
                    current_feature_name.description += text

            if current_feature_name:
                page_features.append(current_feature_name)

            # 检查是否有新特性
            new_features_count = 0
            for feature in page_features:
                # 检查是否已存在
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

        logger.info(
            f"特性识别完成，共识别到 {len(all_features)} 个特性 {[f.name for f in all_features]}"
        )
        # 保存特性列表
        self.features = all_features
        return all_features

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        # 0.佣兵生娃

        # 1.识别父母信息
        father_info = self.extract_parent_info(context, is_father=True)
        mother_info = self.extract_parent_info(context, is_father=False)

        highest_title, count = self.compare_parent_titles(father_info, mother_info)
        logger.info(f"最高爵位是{highest_title}，最高爵位有{count}个")

        # 2.提取天赋属性
        context.run_task("PannelChildInfoButton")
        if not self.extract_attributes(context):
            logger.error("提取子项属性失败")
            return CustomAction.RunResult(success=False)

        # 3. 提取血脉信息
        context.run_task("PropertyPanelSwipeDown")
        if not self.extract_bloodlines(context):
            logger.error("提取血脉信息失败")
            return CustomAction.RunResult(success=False)

        # 4. 提取特性信息
        features = self.extract_features(context)
        if not features:
            logger.warning("未识别到任何特性")

        # 5. 生成子孙命名
        # 注意：这里需要在 extract_attributes 中保存 potential 对象
        # 暂时使用空对象作为示例
        child_name = generate_child_name(
            self.potential, self.bloodline, features, highest_title
        )
        logger.info(f"子孙命名：{child_name}")

        # 6. 输入子孙命名
        context.run_task("BackButton_500ms")
        context.run_task(
            "PannelChildSetName",
            pipeline_override={"PannelChildSetNameCopy": {"input_text": child_name}},
        )

        return CustomAction.RunResult(success=True)
