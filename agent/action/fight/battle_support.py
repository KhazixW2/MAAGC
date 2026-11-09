"""战斗能力 Mixin 的公共基类。

存放跨能力共享的常量、状态声明与少量小工具，避免各 Mixin 互相
复制。所有能力类最终都由 :class:`AutoFightProcessor` 组合使用。
"""

from typing import Any, Optional, Set, Tuple

from .battle_grid import Cell, GridScanner
from .battle_world_map import BattleSessionState


class BattleSupport:
    """战斗能力 Mixin 的公共基类。"""

    # 最外侧约 90px 虽然还能点到状态条，但单位会挡住视野外的敌人，
    # 黄色攻击框也容易被裁切；先平移镜头回到中部再做局部建图。
    ACTION_SAFE_X = (90, 630)
    # 战斗区域实际延伸到 y=1160；地图位于下边界时，状态条会停在
    # y≈1150，仍在 HUD 上沿之外，可以安全点击。
    ACTION_SAFE_Y = (100, 1155)

    # 动作标签约定的两种语义：攻击点按敌人格中心点击，移动点按蓝色格点击。
    ACTION_KIND_ATTACK = "attack"
    ACTION_KIND_MOVE = "move"

    # 由 AutoFightProcessor 初始化；各 Mixin 通过 self 访问共享状态。
    scanner: GridScanner
    _session: Optional[BattleSessionState]
    _coarse_threat_direction: Optional[Tuple[int, int]]
    _last_confirmed_move_direction: Optional[Tuple[int, int]]
    _threat_overlay_safe: bool

    @classmethod
    def _action_kind(cls, label: str) -> str:
        """按动作标签判定动作语义。

        标签带“攻击”按攻击处理：点攻击角标中心，用目标血条变化确认；
        其余按移动处理：点蓝色移动格，用选中态菱形行动点确认。
        点击方式与结果校验共用这一处约定，避免两处各写一套判断。
        """
        if "攻击" in label:
            return cls.ACTION_KIND_ATTACK
        return cls.ACTION_KIND_MOVE

    @classmethod
    def _ally_is_actionable(cls, cell: Cell) -> bool:
        """我方单位是否落在可安全点击的屏幕范围内。"""
        x, y = cell.safe_click_point()
        return (
            cls.ACTION_SAFE_X[0] <= x <= cls.ACTION_SAFE_X[1]
            and cls.ACTION_SAFE_Y[0] <= y <= cls.ACTION_SAFE_Y[1]
        )

    @staticmethod
    def _task_result_has_hit(result: Any, names: Set[str]) -> bool:
        """判断一次任务执行是否命中了指定节点。"""
        if not result or not result.nodes:
            return False
        for node in result.nodes:
            if getattr(node, "name", None) not in names:
                continue
            if getattr(node, "completed", False):
                return True
            recognition = getattr(node, "recognition", None)
            if recognition and getattr(recognition, "hit", False):
                return True
        return False
