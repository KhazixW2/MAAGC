"""危险覆盖层（威胁层）能力。

负责开关底部“危险”开关、在覆盖层画面中定位隐匿敌人的“锯齿中心”，
并把结果归一化成追击方向。只产出方向提示，绝不冒充敌人坐标。
"""

import time
from typing import Any, List, Optional, Tuple

from maa.context import Context

from utils import logger
from utils.screen import screencap

from . import battle_vision
from .battle_geometry import ally_centroid, sign_direction
from .battle_grid import CELL_HEIGHT, CELL_WIDTH, BattleGrid, Cell
from .battle_support import BattleSupport


class BattleThreatMixin(BattleSupport):
    """危险覆盖层识别与方向推断。"""

    # 方向死区：小于该像素差视为抖动，避免方向来回翻转。
    THREAT_DIRECTION_DEAD_ZONE = 60

    def _set_threat_overlay(self, context: Context, enabled: bool) -> bool:
        """把危险覆盖层切换到目标状态，并确认动画已经落稳。"""
        for attempt in range(1, 3):
            img = screencap(context)
            if img is None:
                continue
            current = context.run_recognition(
                "Battle_ThreatToggleOn", img
            ).hit
            if current == enabled:
                return True
            result = context.run_task("Battle_ToggleThreat")
            clicked = self._task_result_has_hit(
                result, {"Battle_ToggleThreat"}
            )
            logger.debug(
                f"危险覆盖层切换为 {'开启' if enabled else '关闭'} "
                f"({attempt}/2): click={clicked}"
            )
            if not clicked:
                continue

            # 开关有淡入淡出动画。点击后轮询到目标状态再决定是否重试，
            # 避免 300ms 内识别到旧画面后再次点击，把刚关闭的红层重新打开。
            deadline = time.monotonic() + 1.4
            while time.monotonic() < deadline:
                time.sleep(0.15)
                settled_img = screencap(context)
                if settled_img is None:
                    continue
                settled = context.run_recognition(
                    "Battle_ThreatToggleOn", settled_img
                ).hit
                if settled == enabled:
                    return True

        final_img = screencap(context)
        verified = bool(
            final_img is not None
            and context.run_recognition(
                "Battle_ThreatToggleOn", final_img
            ).hit
            == enabled
        )
        if not verified:
            logger.warning(
                f"危险覆盖层未能确认切换为 {'开启' if enabled else '关闭'}"
            )
        return verified

    def _direction_from_screen_target(
        self, allies: List[Cell], target_x: int, target_y: int
    ) -> Optional[Tuple[int, int]]:
        """目标相对我方中心的粗粒度八方向。"""
        if not allies:
            return None
        ally_x, ally_y = ally_centroid(allies)
        direction_x, direction_y = sign_direction(
            target_x - ally_x,
            target_y - ally_y,
            self.THREAT_DIRECTION_DEAD_ZONE,
        )
        if not direction_x and not direction_y:
            return None
        return direction_x, direction_y

    def _merge_open_threat_overlay(
        self, context: Context, img: Any, grid: BattleGrid, round_seen: int
    ) -> List[Tuple[int, int]]:
        """Map hidden enemies while the danger overlay is already enabled."""
        if self._session is None or img is None:
            return []
        allies = [
            cell for cell in grid.self_units if self._ally_is_actionable(cell)
        ]
        local_points: List[Tuple[int, int]] = []
        confidence = 0.95
        self._coarse_threat_direction = None

        hidden_cell = battle_vision.threat_enemy_cell_from_mask(img, allies)
        if hidden_cell is not None:
            row, col, _ = hidden_cell
            local_points.append((row, col))
            self._coarse_threat_direction = self._direction_from_screen_target(
                allies,
                col * CELL_WIDTH + CELL_WIDTH // 2,
                row * CELL_HEIGHT + CELL_HEIGHT // 2,
            )
        else:
            # A continuous hollow or the overall ColorMatch box may still give
            # a useful eight-direction hint, but neither is an enemy cell.  In
            # particular, never persist the bounding-box centre as an A* goal.
            origin = battle_vision.threat_origin_from_mask(img, allies)
            if origin is not None:
                origin_x, origin_y, _ = origin
                self._coarse_threat_direction = self._direction_from_screen_target(
                    allies, origin_x, origin_y
                )
            else:
                regions = battle_vision.recognize_threat_regions(
                    context, img
                )
                if regions:
                    x, y, width, height = max(
                        regions, key=battle_vision.largest_region_key
                    )
                    self._coarse_threat_direction = self._direction_from_screen_target(
                        allies,
                        x + width // 2,
                        y + height // 2,
                    )

        # Calling this with an empty list deliberately removes older threat
        # cells from the current viewport.  Coarse direction hints never enter
        # ``world.units`` and therefore cannot accumulate into phantom targets.
        world_points = self._session.world.merge_threat_cells(
            local_points,
            round_seen,
            confidence=confidence,
        )
        if local_points:
            logger.debug(
                "确认锯齿空洞敌人格: "
                f"local={local_points}, world={world_points}"
            )
        elif self._coarse_threat_direction is not None:
            logger.debug(
                "威胁层仅产生方向提示，不写入敌人坐标: "
                f"direction={self._coarse_threat_direction}"
            )
        return local_points

    def _visible_threat_regions(
        self, context: Context
    ) -> List[Tuple[int, int, int, int]]:
        """Return credible danger-overlay regions for the current camera view.

        This is deliberately independent of ally detection: while the search
        camera is panned away from the party, the threat region alone is enough
        to prove that the viewport contains a hidden enemy.  The caller keeps
        the camera offset and later returns to the party before moving.
        """
        if not self._set_threat_overlay(context, True):
            self._set_threat_overlay(context, False)
            return []

        try:
            img = screencap(context)
            if img is None:
                return []
            return battle_vision.recognize_threat_regions(context, img)
        finally:
            # Red cover masks health bars and can be mistaken for enemy pixels
            # by the normal scanner, so always restore the ordinary view.
            self._set_threat_overlay(context, False)

    def _refresh_local_threat_map(
        self, context: Context, round_seen: int
    ) -> bool:
        """Refresh hidden-enemy markers in the current viewport only."""
        self._coarse_threat_direction = None
        if not self._set_threat_overlay(context, True):
            self._threat_overlay_safe = False
            return False
        try:
            img = screencap(context)
            if img is None:
                return False
            self._scan_world_view(
                context,
                img,
                round_seen,
                overlay_open=True,
            )
            return True
        finally:
            restored = self._set_threat_overlay(context, False)
            self._threat_overlay_safe = restored
