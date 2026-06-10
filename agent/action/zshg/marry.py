from dataclasses import dataclass, field
from math import log
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from maa.define import NeuralNetworkResult
from utils import logger

import re
import time
import json
import os
import cv2
from pathlib import Path

from .role_utils import (
    extract_potential,
    extract_bloodlines,
    extract_features,
    get_highest_bloodline,
    extract_all_role_info,
    Bloodline,
)

from .matchMarryProcessor import (
    ChatCandidateProfile,
    ChatMatchingDecider,
    MatchmakerMessage,
)


@AgentServer.custom_action("MarryProcessor")
class MarryProcessor(CustomAction):
    """
    处理联姻相关任务
    """

    def __init__(self) -> None:
        super().__init__()
        self.all_boxes = []
        self.blood_names = {}  # 存储各国姓名表 {国家名：{男：[...], 女：[...]}}
        self.race_country_mapping = {
            "祖扎尔达王族": "加尔提斯商会",
            "瓦诺遗族": "加尔提斯商会",
            "萨尼德罕": "加尔提斯商会",
            "宏朝贵胄": "加尔提斯商会",
            "高阶精灵": "森之祈愿",
            "法拉希尔血裔": "森之祈愿",
            "弗莱德里王族": "弗莱德里王族",
            "古特雅尔": "北地自由民",
            "切瓦利王族": "切瓦利王族",
            "佩尔弗因王族": "佩尔弗因王族",
            "希尔王族": "希尔王族",
            "塞宁王族": "塞宁王族",
            "玛夏贵族": "玛夏审判军",
            "瑞格王室": "瑞格王室",
            "黑暗精灵": "黑暗精灵",
        }
        self.fuzzy_mapping = {
            "祖扎": ("祖扎尔达王族", "加尔提斯商会"),
            "瓦诺": ("瓦诺遗族", "加尔提斯商会"),
            "萨尼": ("萨尼德罕", "加尔提斯商会"),
            "宏朝": ("宏朝贵胄", "加尔提斯商会"),
            "精灵": ("高阶精灵", "森之祈愿"),
            "法拉": ("法拉希尔血裔", "森之祈愿"),
            "弗莱": ("弗莱德里王族", "弗莱德里王族"),
            "古特": ("古特雅尔", "北地自由民"),
            "切瓦利": ("切瓦利王族", "切瓦利王族"),
            "佩尔": ("佩尔弗因王族", "佩尔弗因王族"),
            "希尔": ("希尔王族", "希尔王族"),
            "塞宁": ("塞宁王族", "塞宁王族"),
            "玛夏": ("玛夏贵族", "玛夏审判军"),
            "瑞格": ("瑞格王室", "瑞格王室"),
            "黑暗": ("黑暗精灵", "黑暗精灵"),
        }
        # 联姻状态属性
        self._mode: str = "high_blood"  # 联姻模式
        self._objects_count: int = 0  # 相亲对象总数量
        self._email_current: int = 0  # 当前已约会人数（已接受）
        self._email_total: int = 0  # 最大约会人数
        self._processed_count: int = 0  # 已处理的数量
        self._current_race: str = ""  # 当前相亲对象种族
        self._target_race: str = ""  # 目标相亲对象种族
        self._target_country: str = ""  # 目标相亲对象国家
        self._current_max_attempts: int = 5  # 当前候选人的最大尝试次数
        self._total_candidates: int = 0  # 候选对象总人数
        self._candidate_index: int = 0  # 当前处理到第几个候选人（0开始）
        self._reset_state()
        self._init_boxes()
        self._load_blood_names()

    def _reset_state(self) -> None:
        """重置联姻状态（每次运行前调用）"""
        self._objects_count = 0
        self._email_current = 0
        self._email_total = 0
        self._processed_count = 0
        self._current_race = ""
        self._current_max_attempts = 5
        self._total_candidates = 0
        self._candidate_index = 0

    def _load_blood_names(self) -> None:
        """
        加载高阶血统姓名表
        从 cwd_dir/table/high_blood_names.json 读取
        """
        try:
            # 使用当前工作目录作为基础路径
            cwd_dir = Path(os.getcwd())
            names_file = cwd_dir / "table" / "high_blood_names.json"

            with open(names_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            # 简化结构：直接用种族名作为 key，value 是该种族所有名字的列表
            self.blood_names = {}
            for race, genders in raw_data.items():
                all_names = []
                for gender_names in genders.values():
                    all_names.extend(gender_names)
                self.blood_names[race] = all_names

            logger.info(f"成功加载 {len(self.blood_names)} 个种族的姓名表")
            for race, names in self.blood_names.items():
                logger.info(f"  - {race}: 共{len(names)}个名字")
        except Exception as e:
            logger.error(f"从{names_file}加载姓名表失败：{e}")
            self.blood_names = {}

    def _init_boxes(self) -> None:
        """
        初始化 3 行 5 列的 box 网格
        """
        # 基准 box：第一行第一列 [x, y, width, height]
        base_box = [30, 700, 120, 120]

        # 计算 3 行 5 列的 box 网格
        base_x, base_y, box_width, box_height = base_box
        x_gap = 15  # X 轴间距
        y_gap = 15  # Y 轴间距
        cols = 5  # 每行 5 列
        rows = 3  # 共 3 行

        # 生成二维列表：[row][col] -> box
        self.all_boxes = []
        for row in range(rows):
            row_boxes = []
            for col in range(cols):
                x = base_x + col * (box_width + x_gap)
                y = base_y + row * (box_height + y_gap)
                row_boxes.append([x, y, box_width, box_height])
            self.all_boxes.append(row_boxes)

        logger.info(f"生成 {rows}x{cols} 的 box 网格，共{rows * cols}个 box")

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        """处理联姻任务主流程"""

        # 检测是否是多角色相亲，还是单角色相亲
        self._match_scope = self._get_selected_match_scope(context)
        if self._match_scope:
            execute_result = self._multi_plan(context)
        else:
            execute_result = self._single_plan(context)
        return execute_result

    def _multi_plan(self, context: Context) -> CustomAction.RunResult:
        """
        执行主流程
        """
        self._reset_state()  # 重置状态

        # 获取并保存用户选择的模式
        self._mode = self._get_selected_mode(context)

        # 前处理：检查联姻资格
        candidates = self._pre_process(context)
        if candidates is None:
            return CustomAction.RunResult(success=False)

        # 正式处理阶段
        self._main_process(context, candidates)

        # 后处理：记录完成日志
        self._post_process()
        return CustomAction.RunResult(success=True)

    def _single_plan(self, context: Context) -> CustomAction.RunResult:
        """
        执行单个人的相亲流程
        """
        logger.info("开始执行单个人的相亲流程")
        self._reset_state()  # 重置状态

        # 获取并保存用户选择的模式
        self._mode = self._get_selected_mode(context)

        # 检查联姻资格（界面进入、对象数量、回信数量）
        if not self._check_marriage_eligibility(context):
            return None

        # 正式处理阶段
        self._current_max_attempts = self._objects_count

        # 获取用户选择的相亲种族
        self._target_race = self._get_selected_race(context)
        self._target_country = self.race_country_mapping.get(self._target_race, "未知")
        self._current_race = self._target_race  # 用于面部特征识别

        logger.info(
            f"选择的相亲种族: {self._target_race}，对应的国家: {self._target_country}"
        )

        # 手动执行：点确定 → 滑动 → OCR搜索国家 → 点击
        self._select_country_manual(context, self._target_country)

        # 执行基于血统的匹配或特征匹配
        if self._mode == "trait":
            logger.info("开始执行特征匹配")
            self._execute_chat_matching(context)
        else:
            logger.info(f"开始执行血统匹配，目标种族: {self._target_race}")
            self._execute_high_blood_matching(context, self._target_race)

        # 后处理：记录完成日志
        self._post_process()
        return CustomAction.RunResult(success=True)

    def _get_selected_mode(self, context: Context) -> str:
        """
        获取用户选择的联姻模式

        通过 option select 类型获取用户选择，
        使用 context.get_node_data() 从 pipeline 节点中读取 expected 值
        """
        mode_data = context.get_node_data("MarryModeSelect")
        if mode_data is None:
            logger.warning("MarryModeSelect 节点未定义，使用默认模式 high_blood")
            return "high_blood"

        selected_mode = (
            mode_data.get("recognition", {})
            .get("param", {})
            .get("expected", ["high_blood"])[0]
        )
        logger.info(f"选择的联姻模式: {selected_mode}")
        return selected_mode

    def _get_selected_race(self, context: Context) -> str:
        """
        获取用户选择的联姻种族

        通过 option select 类型获取用户选择，
        使用 context.get_node_data() 从 pipeline 节点中读取 expected 值
        """
        race_data = context.get_node_data("MarryRaceSelect")
        if race_data is None:
            logger.warning("MarryRaceSelect 节点未定义，使用默认种族")
            return "佩尔弗因王族"

        selected_race = (
            race_data.get("recognition", {})
            .get("param", {})
            .get("expected", ["佩尔弗因王族"])[0]
        )
        logger.info(f"选择的联姻种族: {selected_race}")
        return selected_race

    def _get_selected_match_scope(self, context: Context) -> bool:
        """
        检测用户选择的是多角色相亲还是单角色相亲

        通过 option select 类型获取用户选择，
        使用 context.get_node_data() 从 pipeline 节点中读取 expected 值
        """
        multi_data = context.get_node_data("MarryMultiSelect")
        if multi_data is None:
            logger.warning("MarryMultiSelect 节点未定义，使用默认多角色相亲")
            return True

        selected = (
            multi_data.get("recognition", {})
            .get("param", {})
            .get("expected", ["multi"])[0]
        )
        is_multi = selected == "multi"
        logger.info(f"选择的相亲范围: {'多角色相亲' if is_multi else '单角色相亲'}")
        return is_multi

    # ==================== 前处理阶段 ====================

    def _pre_process(self, context: Context) -> list:
        """
        前处理阶段：进入联姻界面，检查资格，查找可相亲对象
        Returns:
            可相亲对象列表，若失败返回 None
        """
        # 1. 检查联姻资格（界面进入、对象数量、回信数量）
        if not self._check_marriage_eligibility(context):
            return None

        # 2. 寻找可相亲对象
        available_boxes = self._find_available_candidates(context)
        if not available_boxes:
            logger.info("未发现可相亲对象")
            return []

        return available_boxes

    def _check_marriage_eligibility(self, context: Context) -> bool:
        """检查联姻资格：进入联姻界面、检查对象数量、回信数量"""
        context.run_task("CastleHall")

        if not context.run_recognition(
            "CastleMarryWindow", context.tasker.controller.post_screencap().wait().get()
        ).hit:
            logger.error("联姻界面未进入,可能存在Bug")
            return False

        logger.info("联姻界面已显示")
        img = context.tasker.controller.post_screencap().wait().get()

        if not self._check_objects_count(context, img):
            return False

        if not self._check_email_count(context, img):
            return False

        return True

    def _check_objects_count(self, context: Context, img) -> bool:
        """检查相亲对象数量"""
        RecoDetail = context.run_recognition("CastleMarryObjectsCheck", img)

        if RecoDetail.hit:
            text = RecoDetail.best_result.text
            logger.info(f"识别到的对象数量文本: {text}")
            match = re.search(r"对象数量[：:](\d{1,2})", text)
            if match:
                self._objects_count = int(match.group(1))
                if self._objects_count > 0:
                    logger.info(f"当前有 {self._objects_count} 个联姻对象")
                    return True
                else:
                    logger.info(
                        f"当前联姻对象数量为{self._objects_count}，无法进行联姻"
                    )
                    return False
            else:
                logger.error(f"未识别到有效的对象数量文本: {text}")
                return False
        else:
            logger.info("未识别到联姻对象数量")
            return False

    def _check_email_count(self, context: Context, img) -> bool:
        """检查回信数量"""
        RecoEmail = context.run_recognition("CastleMarryEmailsCheck", img)

        if RecoEmail.hit:
            text = RecoEmail.best_result.text
            match = re.search(r"回信数量[：:](\d+)/(\d+)", text)
            if match:
                logger.info(f"识别到的回信数量文本: {text}")
                self._email_current = int(match.group(1))
                self._email_total = int(match.group(2))

                if self._email_current < self._email_total:
                    logger.info(
                        f"当前回信数量：{self._email_current}/{self._email_total} 还可以进行联姻"
                    )
                    return True
                else:
                    logger.info(
                        f"当前回信数量：{self._email_current}/{self._email_total} 回信已满，无法继续联姻"
                    )
                    return False
            else:
                logger.error(f"未识别到有效的回信数量文本: {text}")
                return False
        else:
            logger.info("未识别到回信数量信息")
            return False

    def _find_available_candidates(self, context: Context) -> list:
        """扫描所有格子，找出可相亲的对象"""
        available_boxes = []
        img = context.tasker.controller.post_screencap().wait().get()

        for row_idx, row_boxes in enumerate(self.all_boxes):
            for col_idx, box in enumerate(row_boxes):
                # 跳过正在相亲（约会中）的格子
                if context.run_recognition(
                    "CastleMarryingCheck",
                    img,
                    pipeline_override={"CastleMarryingCheck": {"roi": box}},
                ).hit:
                    logger.debug(f"({row_idx + 1}, {col_idx + 1}) 正在相亲")
                    continue

                # 检查是否有爵位（可相亲对象）
                if context.run_recognition(
                    "CastleMarryTitleCheck",
                    img,
                    pipeline_override={"CastleMarryTitleCheck": {"roi": box}},
                ).hit:
                    available_boxes.append((row_idx, col_idx, box))
                    logger.info(f"发现可相亲对象：({row_idx + 1}, {col_idx + 1})")

        logger.info(f"可相亲队列大小：{len(available_boxes)}")
        return available_boxes

    # ==================== 正式处理阶段 ====================

    def _main_process(self, context: Context, candidates: list) -> None:
        """正式处理阶段：逐个处理相亲对象"""
        logger.info(
            f"开始正式处理，可相亲对象: {len(candidates)} 个，"
            f"当前约会: {self._email_current}/{self._email_total}，"
            f"对象总数: {self._objects_count}"
        )

        self._total_candidates = len(candidates)
        self._candidate_index = 0

        for row_idx, col_idx, box in candidates:
            if context.tasker.stopping:
                logger.info("相亲任务已停止")
                break

            # 检查是否应该继续处理
            if not self._should_continue():
                logger.info("检测到联姻条件不满足，停止处理")
                break

            self._process_single_candidate(context, box)

    def _should_continue(self) -> bool:
        """检查是否应该继续处理下一个相亲对象"""
        # 检查约会人数是否已满
        if self._email_current >= self._email_total:
            logger.info(
                f"约会人数已满 ({self._email_current}/{self._email_total})，无法继续"
            )
            return False

        return True

    def _process_single_candidate(self, context: Context, box: list) -> None:
        """处理单个相亲对象"""
        roleBoxCenter = box[0] + box[2] // 2, box[1] + box[3] // 2
        self._processed_count += 1
        logger.info(f"开始处理第{self._processed_count}个角色")

        # 3.1 点击选中角色并长按进入详情
        self._enter_role_details(context, roleBoxCenter)

        # 3.2 检查年龄，大于等于45岁则跳过（不占用处理名额）
        if not self._check_and_filter_by_age(context):
            return

        # 通过年龄检查，占用处理名额
        self._processed_count += 1

        # 3.3 提取血统信息并确定联姻目标
        target_race, target_country = self._evaluate_bloodline_and_get_target(context)
        self._current_race = target_race  # 记录当前种族，供后续面部特征识别使用
        if not target_country or target_country == "未知":
            logger.warning(f"未识别到联姻国家")
            return

        # 计算当前候选人的最大尝试次数（向上取整均分）
        # 例：15次 / 4人 = 每人4次
        self._current_max_attempts = max(
            1,
            (self._objects_count + self._total_candidates - 1)
            // self._total_candidates,
        )
        logger.debug(
            f"候选人序号: {self._candidate_index + 1}/{self._total_candidates}，"
            f"对象总数: {self._objects_count}，本次最大尝试次数: {self._current_max_attempts}"
        )

        # 3.4 进入正式相亲页面进行匹配
        self._execute_marriage_matching(context, target_race, target_country)

        logger.info(f"完成第{self._processed_count}个角色的处理")
        self._candidate_index += 1

    def _enter_role_details(self, context: Context, roleBoxCenter: tuple) -> None:
        """点击角色并长按进入详情界面"""
        context.tasker.controller.post_click(roleBoxCenter[0], roleBoxCenter[1]).wait()
        context.run_task(
            "LongPressRole",
            pipeline_override={"LongPressRole": {"target": roleBoxCenter}},
        )

    def _check_and_filter_by_age(self, context: Context) -> bool:
        """检查年龄并过滤，大于等于45岁则跳过"""
        RecoEmail = context.run_recognition(
            "CastleMarry_AgeCheck",
            context.tasker.controller.post_screencap().wait().get(),
        )
        if RecoEmail.hit:
            age_text = RecoEmail.best_result.text
            logger.debug(f"识别到的年龄文本: {age_text}")

            match = re.search(r"\d+", age_text)
            if match:
                age = int(match.group())
                if age >= 45:
                    logger.info(f"年龄 {age} 大于等于45岁，不考虑")
                    context.run_task("BackButton_500ms")
                    return False
                else:
                    logger.info(f"年龄 {age} 小于45岁，考虑")
        else:
            logger.info("未识别到年龄信息, 跳过")

        return True

    def _evaluate_bloodline_and_get_target(self, context: Context) -> tuple:
        """进入血统面板，评估血统并确定联姻国家和种族。
        规则：只识别当前画面一次，识别不到（未知）就保持原状不滑屏，
        避免无脑滑动把"潜力/血脉/特性"标题滑出可视区。
        """
        context.run_task("RolePanel_BloodPage")
        _, bloodline, _ = extract_all_role_info(context)
        highest_bloodline = self._get_highest_bloodline(bloodline)

        if highest_bloodline == "未知":
            logger.warning(
                "当前画面未识别到血脉信息，保持原状不滑屏，使用默认兜底"
            )
        else:
            logger.info(f"血脉识别成功：{highest_bloodline}")

        target_race, target_country = self._get_marriage_info(highest_bloodline)
        logger.info(f"联姻国家：{target_country}，联姻种族：{target_race}")

        return target_race, target_country

    def _select_country_manual(self, context: Context, country: str) -> None:
        """手动选择联姻国家：先搜 → 搜不到则滑动再搜 → 点击"""
        # 1. 点击确定按钮进入国家选择
        context.tasker.controller.post_click(363, 1223).wait()
        time.sleep(1)

        # 2. 先尝试搜索一次
        if self._try_find_and_click_country(context, country):
            return

        # 3. 没搜到，滑动后再搜
        logger.info(f"首次搜索未找到 [{country}]，滑动后再试")
        context.tasker.controller.post_swipe(380, 600, 380, 500, 300).wait()
        time.sleep(0.5)

        if self._try_find_and_click_country(context, country):
            return

        logger.error(f"滑动后仍未找到国家 [{country}]")

    def _try_find_and_click_country(self, context: Context, country: str) -> bool:
        """OCR搜索国家名称并点击，返回是否成功"""
        img = context.tasker.controller.post_screencap().wait().get()
        reco = context.run_recognition(
            "CastleMarrySelectCountry",
            img,
            pipeline_override={
                "CastleMarrySelectCountry": {"expected": [country]}
            },
        )
        if reco.hit and reco.best_result:
            box = reco.best_result.box
            cx = box[0] + box[2] // 2
            cy = box[1] + box[3] // 2
            logger.info(f"找到国家 [{country}]，点击位置: ({cx}, {cy})")
            context.tasker.controller.post_click(cx, cy).wait()
            time.sleep(0.5)
            context.run_task("CastleMarrySelectCountryConfirm")
            return True
        return False

    def _execute_marriage_matching(
        self,
        context: Context,
        target_race: str,
        target_country: str,
    ) -> None:
        """执行联姻匹配流程（分支点）"""
        context.run_task("BackButton_500ms")
        self._select_country_manual(context, target_country)

        if self._mode == "trait":
            self._execute_chat_matching(context, target_race)
        else:
            self._execute_high_blood_matching(context, target_race)

    def _execute_chat_matching(self, context: Context, target_race: str) -> None:
        """聊天看相模式：血统匹配目标种族就联姻（不需要橙色特征）"""

        max_attempts = self._current_max_attempts

        for attempt in range(1, max_attempts + 1):
            if context.tasker.stopping:
                return

            profile = ChatCandidateProfile()
            profile.name = ""  # 每次尝试重置

            logger.info(f"第 {attempt}/{max_attempts} 次尝试")

            # 聊天循环：收集信息，遇到照片则看血统
            for round_num in range(10):
                if context.tasker.stopping:
                    return

                text = self._recognize_matchmaker_speech(context)
                if text:
                    profile.chat_messages.append(
                        MatchmakerMessage(round_num + 1, text, time.time())
                    )
                    decider = ChatMatchingDecider()
                    decider.extract_trait_from_message(
                        text, profile, self.race_country_mapping
                    )
                    logger.info(text[:50])

                state = self._detect_chat_state_from_text(text)
                if state == "photo":
                    # 检查血统是否匹配目标种族
                    race_matched = any(
                        target_race in bl or bl in target_race
                        for bl in profile.bloodlines
                    )
                    if race_matched:
                        logger.info(
                            f"第 {attempt} 次尝试，血统{list(profile.bloodlines.keys())}"
                            f"匹配目标种族 [{target_race}]，接受该相亲对象: "
                            f"姓名={profile.name}, 爵位={profile.title}"
                        )
                        context.run_task("CastleMarryJustThisButton")
                        context.run_task("PopUpWindowConfirm")
                        self._email_current += 1
                        return
                    else:
                        # 血统不匹配，尝试下一位
                        logger.info(
                            f"第 {attempt} 次尝试，血统{list(profile.bloodlines.keys())}"
                            f"不匹配目标种族 [{target_race}]，尝试下一位: "
                            f"姓名={profile.name}"
                        )
                        break
                elif state == "end":
                    # 未识别到照片就到了结束，直接尝试下一位
                    logger.info(f"第 {attempt} 次尝试未识别到照片，尝试下一位")
                    break

                context.run_task("CastleMarryGetInfoButton")

            # 当前尝试结束，尝试下一位
            if attempt < max_attempts:
                context.run_task("CastleMarryNextOneButton")

        # 5次都失败，拒绝并离开
        logger.info(f"经过 {max_attempts} 次尝试均无匹配种族 [{target_race}]，拒绝该候选人")
        context.run_task("CastleMarryLeave")

    def _execute_high_blood_matching(self, context: Context, target_race: str) -> None:
        """High blood 模式：循环匹配姓名"""
        target_names = self.blood_names.get(target_race, [])
        if not target_names:
            logger.warning(f"{target_race} 的姓名表为空")
            context.run_task("PopUpWindowCancel")
            context.run_task("CastleMarryLeave")
            return

        match_found = self._match_name_in_loop(context, target_names, target_race)

        if not match_found:
            logger.warning(
                f"经过 {self._current_max_attempts} 次尝试，仍未找到匹配的姓名"
            )
            context.run_task("PopUpWindowCancel")
            context.run_task("CastleMarryLeave")

    def _recognize_matchmaker_speech(self, context: Context) -> str:
        """识别媒人说话内容"""
        reco = context.run_recognition(
            "CastleMarryMatchmakerSpeech",
            context.tasker.controller.post_screencap().wait().get(),
        )
        return reco.best_result.text if reco.hit else ""

    def _save_face_screenshot(self, screenshot, race: str = None) -> str:
        """保存面部特征识别截图到本地，按种族分文件夹

        Args:
            screenshot: 截图数组（numpy array，BGR 格式）
            race: 种族名称，默认使用 self._current_race
        """
        # 前置校验：截图为 None 或维度异常时显式 error，不再静默吞错
        if screenshot is None:
            logger.error("[debug_faces] 截图数据为 None，跳过保存")
            return ""
        if not hasattr(screenshot, "ndim") or screenshot.ndim != 3 or screenshot.shape[2] not in (3, 4):
            logger.error(
                f"[debug_faces] 截图维度异常 shape={getattr(screenshot, 'shape', None)}，跳过保存"
            )
            return ""

        try:
            # Windows 兼容的时间戳格式
            timestamp = (
                time.strftime("%Y%m%d_%H%M%S")
                + f"_{int(time.time() * 1000) % 1000:03d}"
            )

            race_folder = (
                race
                if race
                else (self._current_race if self._current_race else "unknown")
            )
            save_dir = (Path("debug_faces") / race_folder).resolve()
            save_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[debug_faces] 写入目录: {save_dir}")
            filename = f"{timestamp}.png"
            filepath = save_dir / filename

            # 1) 修正非连续视图：img[y:y+h, x:x+w] 产生带 stride 的视图，
            #    cv2 编码器对非连续数组在某些版本上会软失败
            if not screenshot.flags["C_CONTIGUOUS"]:
                screenshot = screenshot.copy()
            # 2) 修正非 uint8 dtype：MaaFramework 偶尔返回 float32 / int16
            if screenshot.dtype != "uint8":
                screenshot = screenshot.clip(0, 255).astype("uint8")
            # 3) 修正非 ASCII 路径在 Windows 上的 cv2.imwrite 软失败：
            #    改用 cv2.imencode + Python open("wb") 走 UTF-8 路径，
            #    避免 fopen 系统代码页陷阱
            success, buf = cv2.imencode(".png", screenshot)
            if not success:
                logger.error(
                    f"[debug_faces] cv2.imencode 失败: {filepath} "
                    f"shape={screenshot.shape} dtype={screenshot.dtype}"
                )
                return ""
            filepath.write_bytes(buf.tobytes())
            size = filepath.stat().st_size if filepath.exists() else 0
            logger.info(f"[debug_faces] 已保存: {filepath} ({size} bytes)")
            return str(filepath)
        except Exception as e:
            logger.exception(f"[debug_faces] 保存失败 path={filepath}: {e}")
            return ""

    def _recognize_face_rating(self, context: Context, profile: "ChatCandidateProfile"):
        """识别面部特征，设置橙色特征标记

        crop_roi: [x, y, width, height] = [336, 139, 330, 240]
        """
        img = context.tasker.controller.post_screencap().wait().get()

        # 裁剪面部区域 [x, y, width, height] 图片
        crop_roi = [336, 139, 330, 240]
        x, y, w, h = crop_roi
        crop_img = img[y : y + h, x : x + w]
        # 总是保存截图，用于训练数据收集
        self._save_face_screenshot(crop_img, self._current_race)

        # 只有配置了面部特征节点的种族才进行特征识别
        facial_feature_node = self._get_facial_feature_node(self._current_race)
        if facial_feature_node:
            feature_reco = context.run_recognition(
                facial_feature_node,
                img,
            )
            if feature_reco and feature_reco.all_results:
                for i, result in enumerate(feature_reco.all_results):
                    if isinstance(result, NeuralNetworkResult):
                        logger.info(
                            f"result[{i}]: label={result.label}, score={result.score}"
                        )
                    else:
                        logger.info(
                            f"result[{i}]: box={result.box}, count={result.count}"
                        )
            if feature_reco and feature_reco.hit:
                logger.info(
                    f"识别到{self._current_race}面部特征: {facial_feature_node}"
                )
                # 橙色特征 = 直接接受
                profile.has_orange_feature = True
                return True
            else:
                logger.info(f"面部特征识别未命中")
        else:
            logger.info(f"当前种族 {self._current_race} 没有配置面部特征识别节点")
        return False

    def _get_facial_feature_node(self, race: str) -> str | None:
        """获取种族对应的面部特征识别节点"""
        mapping = {
            "佩尔弗因王族": "佩尔面部特征_科内塔之怒",
            "塞宁王族": "塞宁面部特征_太阳之子",
            "高阶精灵": "精灵面部特征_专注之瞳",
            # 后续添加更多种族...
        }
        return mapping.get(race)

    def _detect_chat_state_from_text(self, text: str) -> str:
        """
        根据媒人说话内容判断聊天状态

        新4种状态：
        - start: 开始阶段（"你想要知道什么信息"）
        - ongoing: 过程中（正常信息 + 照片信息，默认）
        - end: 结束阶段（"我能告诉你的就这么多了"）
        - unknown: 无法识别（默认返回 ongoing）
        """
        if not text:
            return "unknown"

        # 开始阶段
        if "想要知道什么信息" in text:
            return "start"

        # 结束阶段
        if "我能告诉你的就这么多了" in text:
            return "end"

        # 照片阶段（过程中的照片信息）
        photo_keywords = ("芳容", "相貌如此", "面庞")
        if any(kw in text for kw in photo_keywords):
            return "photo"

        # 默认：正常信息阶段（过程中）
        return "ongoing"

    def _match_name_in_loop(
        self, context: Context, target_names: list, target_race: str
    ) -> bool:
        """循环匹配姓名，直到找到匹配的或达到最大次数"""
        max_attempts = self._current_max_attempts

        for attempt in range(max_attempts):
            logger.info(f"第 {attempt + 1}/{max_attempts} 次尝试匹配姓名")

            context.run_task("CastleMarryJustThisButton")

            reco_result = context.run_recognition(
                "CastleMarryJustThisReadName",
                context.tasker.controller.post_screencap().wait().get(),
            )

            if not reco_result.hit:
                logger.warning(f"识别姓名失败, 请检查是否显示了姓名")
                return False

            detected_name = self._extract_name_from_ocr(reco_result.best_result.text)
            if not detected_name:
                return False

            if detected_name in target_names:
                logger.info(
                    f"姓名匹配成功：{detected_name} 在 {target_race} 的高血名单中"
                )
                context.run_task("PopUpWindowConfirm")
                self._email_current += 1  # 约会人数 +1
                return True
            else:
                logger.info(f"姓名不匹配：{detected_name} 不在高血名单中，尝试下一个")
                context.run_task("PopUpWindowCancel")
                if attempt < max_attempts - 1:
                    context.run_task("CastleMarryNextOneButton")

        return False

    def _extract_name_from_ocr(self, ocr_text: str) -> str:
        """从OCR文本中提取姓名"""
        name_match = re.search(r"向([\u4e00-\u9fa5]{1,5})发送", ocr_text)
        if not name_match:
            logger.warning(f"无法从 OCR 结果中提取姓名：{ocr_text}")
            return ""

        return name_match.group(1)

    # ==================== 后处理阶段 ====================

    def _post_process(self) -> None:
        """后处理阶段：记录完成日志"""
        logger.info("联姻任务执行完成")

    def _get_marriage_info(self, bloodline: str) -> tuple[str, str]:
        """
        根据血统确定联姻种族和国家（支持模糊匹配）
        Args:
            bloodline: 血统名称
        Returns:
            (种族名称, 国家名称) 元组
        """

        if bloodline in self.race_country_mapping:
            race = bloodline
            country = self.race_country_mapping[bloodline]
            return race, country

        for keyword, (race, country) in self.fuzzy_mapping.items():
            if keyword in bloodline:
                return race, country

        return bloodline, "未知"

    def _get_highest_bloodline(self, bloodline: Bloodline) -> str:
        """
        从高血种族中获取最高血统
        Args:
            bloodline: Bloodline 对象
        Returns:
            最高血统名称（仅限高血种族）
        """
        if not bloodline.bloodlines:
            return "未知"

        high_blood_races = set(self.race_country_mapping.keys())

        high_blood_bloodlines = {
            name: percentage
            for name, percentage in bloodline.bloodlines.items()
            if name in high_blood_races
        }

        if high_blood_bloodlines:
            sorted_bloodlines = sorted(
                high_blood_bloodlines.items(), key=lambda x: x[1], reverse=True
            )
            return sorted_bloodlines[0][0]

        # 模糊匹配 fallback：OCR 可能有字形错误，如"宏朝贵胃"应为"宏朝贵胄"
        for ocr_name, _ in bloodline.bloodlines.items():
            for keyword in self.fuzzy_mapping.keys():
                if keyword in ocr_name:
                    return ocr_name  # 返回原始OCR名称，让 _get_marriage_info 处理

        return "未知"


@AgentServer.custom_action("WeddingProcessor")
class WeddingProcessor(CustomAction):
    """
    处理婚礼相关任务
    """

    # 爵位优先级映射
    title_rank = {
        "公爵": 4,
        "伯爵": 3,
        "男爵": 2,
        "骑士": 1,
        "无爵位": 0,
    }

    def __init__(self) -> None:
        super().__init__()

    def _extract_title_from_ocr(self, reco_result) -> str:
        """
        从 OCR 结果中提取爵位
        Args:
            reco_result: OCR 识别结果
        Returns:
            爵位名称
        """
        if not reco_result.hit:
            return "无爵位"

        # 遍历所有 OCR 结果，找爵位关键词
        ocr_results = reco_result.all_results
        highest_title = "无爵位"
        highest_rank = 0

        for item in ocr_results:
            text = item.text.strip()
            # 检查是否包含爵位关键词
            for title, rank in self.title_rank.items():
                if title in text and rank > highest_rank:
                    highest_title = title
                    highest_rank = rank

        return highest_title

    def run(
        self, context: Context, argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        """
        处理婚礼事件
        """
        # 1. 先检查是否在婚礼界面
        if not context.run_recognition(
            "Event_WeddingPage",
            context.tasker.controller.post_screencap().wait().get(),
        ).hit:
            logger.error("婚礼界面未进入,可能存在Bug")
            return CustomAction.RunResult(success=False)

        logger.info("婚礼界面已进入")

        # 2. 处理婚礼事件
        # 2.1 检测当前双方最高爵位
        left_title = context.run_recognition(
            "Event_WeddingTitleCheckLeft",
            context.tasker.controller.post_screencap().wait().get(),
        )
        right_title = context.run_recognition(
            "Event_WeddingTitleCheckRight",
            context.tasker.controller.post_screencap().wait().get(),
        )

        # 提取左右两侧的爵位
        left_highest = self._extract_title_from_ocr(left_title)
        right_highest = self._extract_title_from_ocr(right_title)

        # 比较爵位等级，取最高的
        highest_title = self._compare_titles(left_highest, right_highest)

        if not highest_title or highest_title == "无爵位":
            logger.info("未识别到双方最高爵位")
            return CustomAction.RunResult(success=False)

        logger.info(f"当前双方最高爵位：{highest_title}")

        # 2.2 计算当前用什么档位来结婚
        target_banquet = self._get_target_banquet(highest_title)
        logger.info(f"选择宴会档位：{target_banquet}")

        # 3. 找到目标宴会的 button
        context.run_task(
            "Event_WeddingTitleButton",
            pipeline_override={
                "Event_WeddingTitleEntry": {"expected": [target_banquet[:2]]}
            },
        )
        context.run_task("PopUpWindowConfirm")
        # 3.1 确认婚礼 点击四次确认
        time.sleep(2)
        for _ in range(4):
            context.run_task("ClickCenter_500ms")
        return CustomAction.RunResult(success=True)

    def _compare_titles(self, left_title: str, right_title: str) -> str:
        """
        比较两个爵位，返回最高的爵位
        Args:
            left_title: 左侧爵位
            right_title: 右侧爵位
        Returns:
            最高的爵位
        """
        left_rank = self.title_rank.get(left_title, 0)
        right_rank = self.title_rank.get(right_title, 0)

        if left_rank > right_rank:
            return left_title
        elif right_rank > left_rank:
            return right_title
        else:
            return left_title

    def _get_target_banquet(self, title: str) -> str:
        """
        根据爵位获取目标宴会档位
        Args:
            title: 爵位名称
        Returns:
            宴会档位名称
        """
        if title not in self.title_rank:
            logger.warning(f"未识别的爵位：{title}，使用默认档位：乡村宴会")
            return "乡村宴会"

        title_level = self.title_rank[title]
        logger.info(f"爵位等级：{title} (等级{title_level})")

        # 公爵 (等级 4) 使用乡村宴会，其他使用祝福婚宴
        if title_level >= 4:
            return "乡村宴会"
        else:
            return "祝福婚宴"
