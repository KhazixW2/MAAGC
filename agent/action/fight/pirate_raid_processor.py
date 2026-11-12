"""海盗袭击庄园事件处理器。

「海盗正在袭扰你的庄园」是跨地图事件：横幅出现在大地图大陆层，
玩家需要点击横幅 → 战斗准备页 → 进入战斗 → 航海确认 → 战斗画面
→ 战斗开始 → AutoFightProcessor 跑全场 → 结算收尾 → 切回大陆层。

整个流程已经被探明：探 1 点横幅、探 2 进入战斗、探 3 航海确认、
探 4 战斗开始、探 5 战斗胜利、探 6 切回大陆。所有步骤都拆成
event_utils.json 中的 pipeline 节点，本处理器只负责编排。
"""

import time

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from utils import logger
from utils.screen import screencap

import action.fight.fight_utils as fight_utils
from action.fight.fight_utils import (
    ARCHIPELAGO_READY_NODE,
    CONTINENT_READY_NODES,
    find_ready_node,
)


# 地图分层信号（群岛/大陆就绪节点）统一由 fight_utils 定义，
# 这里只放海盗事件自己的节点。
BATTLE_READY_NODE = "Event_PirateRaid_BattlePage"
ACTIVE_BATTLE_NODE = "FightEndRound"
# 横幅节点只用来判断“当前画面上有没有海盗事件”，不执行它的点击动作。
BANNER_NODE = "Event_PirateRaid"
# 战斗准备页（标题「海盗侵袭」+「进入战斗」按钮）。
PREPARE_PAGE_NODE = "Event_PirateRaid_EnterBattle"
LAYER_SWITCH_TIMEOUT = 10.0


def _dialog_visible(context: Context, img) -> bool:
    """旅行弹窗存在时，不能使用弹窗背后的地图按钮作为就绪信号。"""
    return bool(
        context.run_recognition("Event_PirateRaid_TravelDialog", img).hit
        or context.run_recognition("TravelDialog", img).hit
    )


def _wait_for_dialog_closed(context: Context, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        img = screencap(context)
        if img is not None and not _dialog_visible(context, img):
            return True
        time.sleep(0.25)
    logger.error("旅行弹窗点击后仍未关闭")
    return False


def _handle_visible_travel_dialog(context: Context, img) -> bool:
    """只处理当前截图已经确认存在的弹窗，并验证弹窗确实消失。"""
    if context.run_recognition("Event_PirateRaid_TravelDialog", img).hit:
        # 两行布局才有庄园传送；先识别选项再点击，不能把 task 的返回值
        # 误当成“庄园传送真实存在”。单行布局只能点击下方航海确认。
        if context.run_recognition("Event_PirateRaid_ChooseEstate", img).hit:
            node = "Event_PirateRaid_ChooseEstate"
            label = "庄园传送"
        else:
            node = "Event_PirateRaid_ChooseSailSingle"
            label = "单行航海"
        if not _tap(context, node, label):
            return False
        return _wait_for_dialog_closed(context)

    if context.run_recognition("TravelDialog", img).hit:
        if context.run_recognition("TravelDialog_ChooseFast", img).hit:
            node = "TravelDialog_ChooseFast"
            label = "快速传送"
        else:
            node = "TravelDialog_ChooseSlow"
            label = "步行/航海"
        if not _tap(context, node, label):
            return False
        return _wait_for_dialog_closed(context)

    return True


def _wait_for_layer_ready(
    context: Context,
    ready_nodes: tuple,
    timeout: float,
    label: str,
) -> bool:
    """轮询截图，先处理旅行弹窗，再接受无遮挡的地图就绪信号。"""
    deadline = time.monotonic() + timeout
    # 地图按钮会先于旅行弹窗出现，至少留出一小段弹窗生成时间。
    accept_ready_after = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        img = screencap(context)
        if img is None:
            time.sleep(0.2)
            continue
        if _dialog_visible(context, img):
            if not _handle_visible_travel_dialog(context, img):
                return False
            accept_ready_after = time.monotonic() + 0.5
            continue
        if time.monotonic() < accept_ready_after:
            time.sleep(0.25)
            continue
        hit_node = find_ready_node(context, ready_nodes, img)
        if hit_node:
            logger.info(f"{label}层就绪（识别命中 {hit_node}）")
            return True
        time.sleep(0.25)
    logger.error(f"等待{label}层就绪超时 ({timeout}s)")
    return False


def _tap(context: Context, node: str, label: str) -> bool:
    """点击节点并验证成功。"""
    result = context.run_task(node)
    if not fight_utils._task_succeeded(result):
        logger.error(f"{label} 节点 {node} 点击失败")
        return False
    logger.info(f"{label} 节点 {node} 点击成功")
    return True


def _wait_for_map_screen(context: Context, timeout: float) -> bool:
    """等待回到地图画面：结算面板消失且底部图层页签可读。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if context.tasker.stopping:
            return False
        img = screencap(context)
        if img is None:
            time.sleep(0.2)
            continue
        if context.run_recognition("Event_PirateRaid_VictoryConfirm", img).hit:
            time.sleep(0.3)
            continue
        if fight_utils.read_map_layer_name(context, img):
            return True
        time.sleep(0.3)
    return False


def _dismiss_random_popup(context: Context) -> bool:
    """处理挡在海盗事件链前面的随机事件弹窗。

    事件链的每一步之间游戏都可能弹出随机事件（生娃、佣兵加入等），
    弹窗会盖住「进入战斗」「战斗开始」这些按钮，不先关掉就会点空。
    返回 True 表示处理掉了一个弹窗。
    """
    from action.fight.fight_processor import detect_and_manage_event

    img = screencap(context)
    if img is None:
        return False
    event_name = detect_and_manage_event(context, img, skip_pirate_raid=True)
    if event_name:
        logger.info(f"海盗事件链中处理随机事件: {event_name}")
        return True
    return False


@AgentServer.custom_action("PirateRaidProcessor")
class PirateRaidProcessor(CustomAction):
    """执行海盗袭击庄园事件。"""

    def run(
        self, context: Context, _argv: CustomAction.RunArg
    ) -> CustomAction.RunResult:
        logger.info("=== 海盗袭击庄园事件开始 ===")

        # 0/1. 支持从大陆、已在群岛、旅行弹窗、战斗准备页或已开战状态续跑。
        initial_img = screencap(context)
        initial_dialog = bool(
            initial_img is not None and _dialog_visible(context, initial_img)
        )
        at_victory = bool(
            initial_img is not None
            and (
                context.run_recognition("FightVictory", initial_img).hit
                or context.run_recognition(
                    "Event_PirateRaid_VictoryConfirm", initial_img
                ).hit
            )
        )
        at_battle_page = bool(
            initial_img is not None
            and context.run_recognition(BATTLE_READY_NODE, initial_img).hit
        )
        in_active_battle = bool(
            initial_img is not None
            and context.run_recognition(ACTIVE_BATTLE_NODE, initial_img).hit
        )
        already_archipelago = bool(
            initial_img is not None
            and not initial_dialog
            and fight_utils.on_archipelago_layer(context, initial_img)
        )
        # 记下事件开始时所在的大陆图层名，事件跑完要切回去。
        fight_utils.remember_continent_layer(context, initial_img)
        # 横幅是否就在当前画面上。横幅在大陆层就能直接点开战斗准备页，
        # 不需要先切到群岛层（切层按钮「前往群岛」在新版 UI 上已不存在）。
        banner_visible = bool(
            initial_img is not None
            and context.run_recognition(BANNER_NODE, initial_img).hit
        )
        at_prepare_page = bool(
            initial_img is not None
            and context.run_recognition(PREPARE_PAGE_NODE, initial_img).hit
        )

        if initial_dialog:
            logger.info("海盗事件：当前在旅行弹窗，从航海确认继续")
            if not _handle_visible_travel_dialog(context, initial_img):
                return CustomAction.RunResult(success=False)
            if not _wait_for_layer_ready(
                context,
                (ARCHIPELAGO_READY_NODE, BATTLE_READY_NODE, ACTIVE_BATTLE_NODE),
                LAYER_SWITCH_TIMEOUT,
                "群岛/战斗准备",
            ):
                return CustomAction.RunResult(success=False)
            resumed_img = screencap(context)
            if resumed_img is None:
                return CustomAction.RunResult(success=False)
            at_battle_page = context.run_recognition(
                BATTLE_READY_NODE, resumed_img
            ).hit
            in_active_battle = context.run_recognition(
                ACTIVE_BATTLE_NODE, resumed_img
            ).hit
            already_archipelago = context.run_recognition(
                ARCHIPELAGO_READY_NODE, resumed_img
            ).hit

        if at_victory:
            logger.info("海盗事件：当前已在战斗胜利页，从结算继续")
        elif at_battle_page:
            logger.info("海盗事件：当前已在战斗准备页，从战斗开始继续")
        elif at_prepare_page:
            logger.info("海盗事件：当前已在海盗侵袭准备页，从进入战斗继续")
        elif in_active_battle:
            logger.info("海盗事件：当前已经开战，直接交给主动战斗处理器")
        elif already_archipelago:
            logger.info("海盗事件：当前已在群岛层，跳过重复切图")
        elif banner_visible:
            # 横幅就在当前画面（大陆层），点它会自动跳到群岛并打开准备页。
            logger.info("海盗事件：当前画面已有海盗横幅，直接点横幅进准备页")
        else:
            if not fight_utils.ensure_at_bigmap(context):
                logger.error("海盗事件：无法回到大地图，放弃")
                return CustomAction.RunResult(success=False)
            if not _tap(context, "ClickGoToArchipelago", "切群岛"):
                logger.error("海盗事件：无法切到群岛层，放弃")
                return CustomAction.RunResult(success=False)
            # 海盗袭击期间，单行航海确认后可能直接落到战斗准备页，
            # 也可能先落到群岛地图；二者都是合法的切层终态。
            if not _wait_for_layer_ready(
                context,
                (ARCHIPELAGO_READY_NODE, BATTLE_READY_NODE),
                LAYER_SWITCH_TIMEOUT,
                "群岛/战斗准备",
            ):
                return CustomAction.RunResult(success=False)
            switched_img = screencap(context)
            if switched_img is None:
                return CustomAction.RunResult(success=False)
            at_battle_page = context.run_recognition(
                BATTLE_READY_NODE, switched_img
            ).hit
            in_active_battle = context.run_recognition(
                ACTIVE_BATTLE_NODE, switched_img
            ).hit
            already_archipelago = context.run_recognition(
                ARCHIPELAGO_READY_NODE, switched_img
            ).hit
            at_prepare_page = context.run_recognition(
                PREPARE_PAGE_NODE, switched_img
            ).hit

        # 2. 走「点横幅 → 准备页 → 进入战斗 → 航海 → 战斗画面 →
        #    战斗开始 → AutoFightProcessor」的 pipeline 链。
        if not at_victory and not in_active_battle:
            if at_battle_page:
                nodes = ("Event_PirateRaid_BattleStart",)
            elif at_prepare_page:
                nodes = (
                    "Event_PirateRaid_EnterBattle",
                    "Event_PirateRaid_SailingConfirm",
                    "Event_PirateRaid_BattleStart",
                )
            else:
                nodes = (
                    "Event_PirateRaid_ClickBanner",
                    "Event_PirateRaid_EnterBattle",
                    "Event_PirateRaid_SailingConfirm",
                    "Event_PirateRaid_BattleStart",
                )
            for node in nodes:
                # 每一步之前先清掉可能盖住按钮的随机事件弹窗。
                _dismiss_random_popup(context)
                if not _tap(context, node, "海盗链"):
                    # 任一节点失败都尝试切回大陆，避免下一轮被卡。
                    self._recover_to_continent(context)
                    return CustomAction.RunResult(success=False)

        # 3. AutoFightProcessor 跑全场战斗，胜负检测由其内部完成。
        if not at_victory:
            result = context.run_task("AutoFight_Start")
            if not fight_utils._task_succeeded(result):
                logger.error("海盗事件：群岛层战斗未成功结束")
                self._recover_to_continent(context)
                return CustomAction.RunResult(success=False)

        # 4. 战斗胜利 → 战利品页「确定」+ 经验页「确定」。
        #    Event_PirateRaid_VictoryConfirm 节点里 next 循环指向自身，
        #    一直点到结算页消失为止。
        if not _tap(context, "Event_PirateRaid_VictoryConfirm", "结算"):
            logger.warning("海盗事件：战利品/经验结算未完成，但继续切回大陆")
        if not _wait_for_map_screen(context, 10.0):
            logger.warning("海盗事件：未确认回到地图，仍尝试切回大陆")

        # 5. 切回大陆层。
        if not self._recover_to_continent(context):
            logger.error("海盗事件：未能切回大陆层")
            return CustomAction.RunResult(success=False)

        logger.info("=== 海盗袭击庄园事件完成 ===")
        return CustomAction.RunResult(success=True)

    def _recover_to_continent(self, context: Context) -> bool:
        """切回大陆层。

        新版 UI 没有「前往大陆」按钮，只能靠底部地图页签的图层弹窗切换。
        大陆图层名在事件开始时（还在大陆层）已经记下。
        """
        target = fight_utils.get_continent_layer_name()
        if fight_utils.read_map_layer_name(context) and not fight_utils.on_archipelago_layer(context):
            logger.info("恢复大陆：当前已经在大陆层")
            return True
        # 大陆图层名没记住时（事件一开始就在群岛层），退回弹窗第一个选项。
        if not fight_utils.switch_map_layer(context, target, fallback_to_first=True):
            logger.error(f"恢复大陆：切换到 {target} 失败")
            return False
        logger.info(f"恢复大陆：已切回 {target or '弹窗第一个图层'}")
        return True
