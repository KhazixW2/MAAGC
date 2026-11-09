"""镜头平移与全局建图能力。

负责地图滑动、真实位移测量、顺时针螺旋搜索与边缘单位重定位。
镜头探索本身不算战斗进展，只有新观察到的地图格才会重置看门狗。
"""

import time
from typing import Any, List, Optional, Tuple

from maa.context import Context

from utils import logger
from utils.screen import screencap

from . import battle_vision
from .battle_grid import COLS, ROWS, BattleGrid, Cell
from .battle_support import BattleSupport
from .battle_world_map import (
    ENEMY as WORLD_ENEMY,
    ENVIRONMENT as WORLD_ENVIRONMENT,
    SELF as WORLD_SELF,
)


class BattleCameraMixin(BattleSupport):
    """镜头平移、边界探测与螺旋搜索。"""

    MAX_SEARCH_SWIPES = 16
    MAX_STRAIGHT_SEARCH_SWIPES = 3

    def _pan_camera(
        self,
        context: Context,
        direction: Tuple[int, int],
        fine: bool = False,
    ) -> bool:
        """按方向平移镜头；``fine`` 使用小幅微调节点。"""
        prefix = "Battle_MapNudge" if fine else "Battle_MapPan"
        node_by_direction = {
            (1, 0): f"{prefix}Right",
            (-1, 0): f"{prefix}Left",
            (0, 1): f"{prefix}Down",
            (0, -1): f"{prefix}Up",
            (1, 1): f"{prefix}DownRight",
            (-1, 1): f"{prefix}DownLeft",
            (-1, -1): f"{prefix}UpLeft",
            (1, -1): f"{prefix}UpRight",
        }
        node = node_by_direction.get(direction)
        if node is None:
            return False
        result = context.run_task(node)
        return self._task_result_has_hit(result, {node})

    def _pan_and_scan(
        self,
        context: Context,
        direction: Tuple[int, int],
        phase: str,
    ) -> Optional[Tuple[BattleGrid, Any]]:
        """滑动一个视野；只有背景真实移动时才返回新建战场快照。"""
        before_img = screencap(context)
        if before_img is None or not self._pan_camera(context, direction):
            logger.warning(f"{phase}地图滑动 {direction} 失败")
            return None
        after_img = screencap(context)
        if after_img is None:
            logger.warning(f"{phase}滑动后截图失败，撤销该方向")
            self._pan_camera(context, (-direction[0], -direction[1]))
            return None

        shift_x, shift_y, response = battle_vision.camera_motion(
            before_img, after_img
        )
        if not battle_vision.camera_shift_is_real(
            shift_x, shift_y, response
        ):
            logger.debug(
                f"{phase}向 {direction} 滑动后背景位移="
                f"({shift_x:.1f},{shift_y:.1f}), response={response:.3f}，"
                "判定已到边界"
            )
            return None

        grid = BattleGrid()
        self.scanner.scan_grid(grid, context, after_img)
        return grid, after_img

    def _pan_world_once(
        self,
        context: Context,
        direction: Tuple[int, int],
        round_seen: int,
        *,
        overlay_open: bool,
        phase: str,
        fine: bool = False,
    ) -> Optional[Tuple[bool, Optional[BattleGrid], Any]]:
        """Pan once, register measured camera motion, then update the map.

        ``direction`` is expressed as ``(world_row, world_col)``.  The return
        value is ``(moved, grid, image)``; ``None`` means the swipe action itself
        failed and therefore must not be mistaken for a map boundary.
        """
        if self._session is None:
            return None
        before_img = screencap(context)
        if before_img is None:
            return None
        before_observed = len(self._session.world.observed)
        direction_xy = direction[1], direction[0]
        if not self._pan_camera(context, direction_xy, fine=fine):
            logger.warning(f"{phase}地图滑动动作失败: direction={direction}")
            return None
        after_img = screencap(context)
        if after_img is None:
            return None

        shift_x, shift_y, response = battle_vision.camera_motion(
            before_img, after_img
        )
        if not battle_vision.camera_shift_is_real(
            shift_x, shift_y, response
        ):
            self._session.world.mark_boundary(direction)
            logger.debug(
                f"{phase}到达地图边界: direction={direction}, "
                f"motion=({shift_x:.1f},{shift_y:.1f}), response={response:.3f}"
            )
            return False, None, after_img

        origin_delta = self._session.world.apply_camera_motion(shift_x, shift_y)
        grid = self._scan_world_view(
            context,
            after_img,
            round_seen,
            overlay_open=overlay_open,
        )
        # Camera exploration alone is not combat progress.  In particular,
        # threat cells can appear/disappear as overlapping viewports clip the
        # jagged hole; resetting the watchdog for that visual churn made the
        # 12-cycle no-progress fuse stay forever at 1/12.  Newly observed map
        # cells are useful progress, while confirmed attacks/moves reset the
        # counter at their action sites.
        if len(self._session.world.observed) > before_observed:
            self._session.record_progress()
        logger.debug(
            f"{phase}地图更新: direction={direction}, "
            f"motion=({shift_x:.1f},{shift_y:.1f}), "
            f"origin_delta={origin_delta}, "
            f"origin={self._session.world.camera_origin}, "
            f"explored={len(self._session.world.observed)}"
        )
        return True, grid, after_img

    def _explore_world_clockwise(
        self, context: Context, round_seen: int
    ) -> bool:
        """Explore right/down/left/up with threat mapping kept enabled."""
        if self._session is None:
            return False
        explorer = self._session.new_exploration_pass()
        world = self._session.world
        self._coarse_threat_direction = None
        logger.debug(
            f"开始第 {self._session.exploration_passes} 次顺时针全局建图："
            "右 -> 下 -> 左 -> 上"
        )
        if not self._set_threat_overlay(context, True):
            self._threat_overlay_safe = False
            return False

        action_failures = 0
        try:
            initial_img = screencap(context)
            if initial_img is None:
                return False
            self._scan_world_view(
                context,
                initial_img,
                round_seen,
                overlay_open=True,
            )
            while not explorer.completed and not context.tasker.stopping:
                if world.has_allies() and (
                    self._world_has_fresh_targets()
                    or self._coarse_threat_direction is not None
                ):
                    logger.debug("全局地图已同时具备我方和敌人，提前结束探索")
                    break
                direction = explorer.direction
                pan_result = self._pan_world_once(
                    context,
                    direction,
                    round_seen,
                    overlay_open=True,
                    phase="顺时针探索",
                )
                if pan_result is None:
                    action_failures += 1
                    if action_failures >= 2:
                        logger.error("顺时针探索连续两次滑动动作失败")
                        return False
                    continue
                action_failures = 0
                moved, _, _ = pan_result
                explorer.record_pan(moved)
        finally:
            restored = self._set_threat_overlay(context, False)
            self._threat_overlay_safe = restored

        if not self._threat_overlay_safe:
            logger.error("顺时针探索结束后无法关闭威胁层")
            return False
        logger.debug(
            "顺时针建图结束: "
            f"complete={explorer.completed}, swipes={explorer.successful_swipes}, "
            f"allies={len(world.unit_points(WORLD_SELF))}, "
            f"enemies={len(world.unit_points(WORLD_ENEMY))}, "
            f"environment={len(world.unit_points(WORLD_ENVIRONMENT))}"
        )
        return world.has_allies() and (
            self._world_has_fresh_targets()
            or self._coarse_threat_direction is not None
        )

    def _focus_known_ally(
        self, context: Context, round_seen: int
    ) -> Optional[Tuple[BattleGrid, Any]]:
        """Move the camera to the newest known ally and leave it actionable."""
        if self._session is None:
            return None
        if not self._set_threat_overlay(context, False):
            return None
        world = self._session.world
        failed_pans = 0

        for _ in range(14):
            img = screencap(context)
            if img is None:
                return None
            grid = self._scan_world_view(
                context,
                img,
                round_seen,
                overlay_open=False,
            )
            actionable = [
                cell for cell in grid.self_units if self._ally_is_actionable(cell)
            ]
            centered = [
                cell
                for cell in actionable
                if 1 <= cell.row <= ROWS - 2 and 1 <= cell.col <= COLS - 2
            ]
            if centered:
                return grid, img
            if actionable:
                target_world = world.local_to_world(
                    actionable[0].row, actionable[0].col
                )
            else:
                ally_points = world.unit_points(WORLD_SELF)
                if not ally_points:
                    return None
                target_world = ally_points[0]

            local = world.world_to_local(target_world)
            if local is None:
                origin_row, origin_col = world.camera_origin
                centre = origin_row + ROWS // 2, origin_col + COLS // 2
                row_direction = (
                    0
                    if target_world[0] == centre[0]
                    else (1 if target_world[0] > centre[0] else -1)
                )
                col_direction = (
                    0
                    if target_world[1] == centre[1]
                    else (1 if target_world[1] > centre[1] else -1)
                )
            else:
                row_direction = -1 if local[0] <= 0 else (1 if local[0] >= ROWS - 1 else 0)
                col_direction = -1 if local[1] <= 0 else (1 if local[1] >= COLS - 1 else 0)
            direction = row_direction, col_direction
            if direction == (0, 0):
                # The remembered ally was inside this view but a fresh normal
                # scan invalidated it.  Retry with another observation.
                continue

            pan_result = self._pan_world_once(
                context,
                direction,
                round_seen,
                overlay_open=False,
                phase="定位我方",
                fine=True,
            )
            if pan_result is None:
                failed_pans += 1
                if failed_pans >= 2:
                    return None
                continue
            moved, _, _ = pan_result
            if not moved:
                # Reaching a camera boundary does not invalidate a unit that
                # is already inside the conservative click-safe rectangle.
                # Returning that edge view lets the local planner select the
                # ally and move toward the retained threat target instead of
                # re-entering global exploration forever.
                if actionable:
                    logger.debug(
                        "镜头已到边界，沿用当前可操作我方视野进入点对点行动"
                    )
                    return grid, img
                return None
        return None

    def _search_for_enemies(
        self, context: Context
    ) -> Optional[Tuple[Tuple[int, int], BattleGrid, bool]]:
        """顺时针螺旋搜索人物/环境目标；若敌我同屏则停留。"""
        executed_steps: List[Tuple[int, int]] = []
        offset_x = 0
        offset_y = 0
        views_scanned = 1
        found_direction: Optional[Tuple[int, int]] = None
        found_enemy_count = 0
        found_environment_count = 0
        found_environment_only = False
        found_view = 0
        found_grid: Optional[BattleGrid] = None
        blocked_direction: Optional[Tuple[int, int]] = None

        logger.debug(
            "当前视野没有敌人，开始顺时针螺旋搜索，"
            f"最多滑动 {self.MAX_SEARCH_SWIPES} 次"
        )

        # 搜索阶段既检查真实红色血条，也检查危险覆盖区。草丛会遮住
        # 血条，而覆盖区的锯齿中心仍标识敌人；找到覆盖区时，当前镜头
        # 相对起点的偏移就是返回我方后应推进的粗粒度方向。
        self._set_threat_overlay(context, False)

        for direction in self._spiral_directions(self.MAX_SEARCH_SWIPES):
            if context.tasker.stopping:
                break
            if direction == blocked_direction:
                continue
            blocked_direction = None
            scan_result = self._pan_and_scan(
                context, direction, "敌人搜索"
            )
            if scan_result is None:
                # 当前方向已经撞边，跳过本螺旋段剩余的同方向手势；
                # 换向后再允许该方向出现在后续螺旋段中。
                blocked_direction = direction
                continue
            search_grid, _ = scan_result
            executed_steps.append(direction)
            offset_x += direction[0]
            offset_y += direction[1]
            views_scanned += 1
            threat_regions = []
            if not search_grid.enemy_units:
                threat_regions = self._visible_threat_regions(context)
            logger.debug(
                f"地图搜索视野 {views_scanned}: offset=({offset_x},{offset_y}), "
                f"真实红色状态条={len(search_grid.enemy_units)}, "
                f"威胁区域={len(threat_regions)}, "
                f"环境目标={len(search_grid.environment_units)}, "
                f"我方={len(search_grid.self_units)}"
            )
            if (
                search_grid.enemy_units
                or search_grid.environment_units
                or threat_regions
            ):
                found_direction = (offset_x, offset_y)
                found_enemy_count = len(search_grid.enemy_units) or len(threat_regions)
                found_environment_count = len(search_grid.environment_units)
                found_environment_only = (
                    not search_grid.enemy_units
                    and not threat_regions
                    and bool(search_grid.environment_units)
                )
                found_view = views_scanned
                found_grid = search_grid
                if found_environment_only:
                    logger.debug(
                        "搜索视野已确认祭坛/石碑类环境目标："
                        f"数量={found_environment_count}"
                    )
                elif threat_regions:
                    logger.debug(
                        "搜索视野已确认隐藏敌人的威胁区域（锯齿中心方向有效）："
                        f"区域数={len(threat_regions)}"
                    )
                else:
                    logger.debug(
                        "搜索视野已确认带生命数字的敌人状态条："
                        f"数量={found_enemy_count}"
                    )
                break

        if (
            found_direction is not None
            and found_grid is not None
            and found_grid.self_units
        ):
            self._set_threat_overlay(context, False)
            logger.debug(
                f"搜索视野同时保留 {len(found_grid.self_units)} 名我方，"
                "停留当前视野直接行动，不再逆向返回"
            )
            return found_direction, found_grid, found_environment_only

        # B 路径：敌人已确认但当前相机我方不可见。
        # 大地图下「敌我同屏」几乎不可能发生，不能让 reverse-pan 这一个
        # 不可靠的反向滑动决定整场成败。改用更小的顺时针螺旋专门找回
        # 我方位置；找到后保留敌人方向（让 run() 跨回合推进），即便 grid
        # 里不再有敌人也能继续推进。
        if found_direction is not None:
            logger.warning(
                "搜索到远端敌人/环境目标但当前视野我方不可见，"
                "改用顺时针小螺旋找回我方位置（不回滚到原点）"
            )
            ally_anchor_grid = self._find_allies(context, max_swipes=6)
            if ally_anchor_grid is not None:
                target_label = (
                    f"{found_environment_count} 个环境目标"
                    if found_environment_only
                    else f"{found_enemy_count} 名敌人"
                )
                logger.debug(
                    f"远端发现 {target_label}，相对方向={found_direction}，"
                    f"扫描视野={found_view}；已回到我方视野，按方向推进"
                )
                self._set_threat_overlay(context, False)
                return (
                    found_direction,
                    ally_anchor_grid,
                    found_environment_only,
                )
            logger.warning("小螺旋找回我方失败，回退到 restore_pans 路径")

        # C 路径：未找到敌人 或 B 路径的 _find_allies 也失败。
        # 按实际成功移动的反向序列回退原点，边缘处未产生画面变化的手势
        # 不在 executed_steps 中。
        self._restore_pans(context, executed_steps)

        self._set_threat_overlay(context, False)
        self._wait_for_scene_settle(context, timeout=3.0)
        restored_grid = BattleGrid()
        restored_img = screencap(context)
        if restored_img is not None:
            self.scanner.scan_grid(restored_grid, context, restored_img)
        else:
            self.scanner.scan_grid(restored_grid, context)
        restore_succeeded = bool(restored_grid.self_units) or (
            restored_img is not None
            and self._has_safe_blue_evidence(context, restored_img)
        )
        for retry in range(1, 3):
            if restore_succeeded:
                break
            if context.tasker.stopping:
                return None
            logger.warning(
                f"逆向返回第 {retry} 次补充扫描仍未确认我方，"
                "等待 HUD 收敛后重试"
            )
            time.sleep(0.4)
            self._wait_for_scene_settle(context, timeout=2.0)
            retry_img = screencap(context)
            if retry_img is None:
                continue
            restored_grid = BattleGrid()
            self.scanner.scan_grid(restored_grid, context, retry_img)
            restore_succeeded = bool(restored_grid.self_units) or (
                self._has_safe_blue_evidence(context, retry_img)
            )

        if not restore_succeeded:
            logger.error("逆向返回后没有重新找到我方单位，地图搜索结果作废")
            return None

        if found_direction is not None:
            target_label = (
                f"{found_environment_count} 个环境目标"
                if found_environment_only
                else f"{found_enemy_count} 名敌人"
            )
            logger.debug(
                f"远端发现 {target_label}，相对方向={found_direction}，"
                f"扫描视野={found_view}"
            )
            return found_direction, restored_grid, found_environment_only
        return None

    def _find_allies(
        self, context: Context, max_swipes: int
    ) -> Optional[BattleGrid]:
        """从当前相机出发顺时针螺旋找回我方；找到后停留。

        作为 `_search_for_allies_and_stay`（大螺旋）与
        `_search_for_enemies` 的小步螺旋找回我方子流程的公共实现。
        大地图下 `_search_for_enemies` 找到远端敌人但当前视野无我方时，
        调用本函数以更可靠的方式把相机带回我方视野。
        """
        self._set_threat_overlay(context, False)
        executed_steps: List[Tuple[int, int]] = []
        offset_x = 0
        offset_y = 0
        blue_evidence_seen = False
        attempted_steps = 0
        blocked_direction: Optional[Tuple[int, int]] = None

        logger.debug(
            "开始顺时针螺旋找回我方位置，"
            f"最多滑动 {max_swipes} 次"
        )

        for view_index, direction in enumerate(
            self._spiral_directions(max_swipes), start=2
        ):
            if context.tasker.stopping:
                break
            if direction == blocked_direction:
                continue
            blocked_direction = None
            attempted_steps += 1
            scan_result = self._pan_and_scan(
                context, direction, "我方搜索"
            )
            if scan_result is None:
                blocked_direction = direction
                continue
            search_grid, current_img = scan_result
            executed_steps.append(direction)
            offset_x += direction[0]
            offset_y += direction[1]
            logger.debug(
                f"我方搜索视野 {view_index}: offset=({offset_x},{offset_y}), "
                f"我方={len(search_grid.self_units)}, "
                f"敌人={len(search_grid.enemy_units)}"
            )
            if search_grid.self_units:
                logger.debug(
                    f"重新找到 {len(search_grid.self_units)} 名我方单位，"
                    f"停留在 offset=({offset_x},{offset_y})"
                )
                return search_grid
            if self._has_safe_blue_evidence(context, current_img):
                if not blue_evidence_seen:
                    logger.debug(
                        f"我方搜索视野 {view_index} 发现蓝色碎片，"
                        "但没有完整我方状态条；继续既定螺旋，"
                        "不追加四方向邻域探查"
                    )
                blue_evidence_seen = True

        logger.debug(
            f"顺时针探索完成（max={max_swipes}, 实际手势={attempted_steps}, "
            f"有效视野移动={len(executed_steps)}）；"
            "未找到完整我方状态条，restore 回原视野"
        )
        self._restore_pans(context, executed_steps)
        self._wait_for_scene_settle(context, timeout=3.0)
        return None

    def _restore_pans(
        self, context: Context, executed_steps: List[Tuple[int, int]]
    ) -> None:
        """逆序撤销已确认产生背景位移的镜头滑动。"""
        for direction in reversed(executed_steps):
            if context.tasker.stopping:
                return
            self._pan_camera(context, (-direction[0], -direction[1]))

    def _recenter_edge_allies(
        self,
        context: Context,
        grid: BattleGrid,
        round_seen: int,
        *,
        known_target: Optional[Tuple[int, int]] = None,
    ) -> Tuple[BattleGrid, bool, Any]:
        """Pan edge units inward and keep the persistent map synchronized."""
        if not grid.self_units:
            return grid, False, None

        current_grid = grid
        moved = False
        latest_img = None
        known_world_target = (
            self._session.world.local_to_world(*known_target)
            if known_target is not None and self._session is not None
            else None
        )

        # A corner unit needs one diagonal nudge, not two alternating cardinal
        # nudges.  The candidate frame must still contain the party and, when a
        # real enemy was visible, that enemy as well.  Failed nudges are rolled
        # back through the same map-aware path so camera_origin stays correct.
        for _ in range(4):
            edge_enemies = [
                cell
                for cell in current_grid.enemy_units
                if self._edge_recenter_direction(
                    cell,
                    x_bounds=self.ACTION_SAFE_X,
                    y_bounds=(160, 1000),
                )
                is not None
            ]
            unsafe_allies = [
                cell
                for cell in current_grid.self_units
                if not self._ally_is_actionable(cell)
            ]
            known_edge_cells: List[Cell] = []
            if known_world_target is not None and self._session is not None:
                current_local_target = self._session.world.world_to_local(
                    known_world_target
                )
                if current_local_target is not None:
                    synthetic_target = Cell(*current_local_target)
                    if self._edge_recenter_direction(
                        synthetic_target,
                        x_bounds=self.ACTION_SAFE_X,
                        y_bounds=(160, 1000),
                    ) is not None:
                        known_edge_cells.append(synthetic_target)
            if not edge_enemies and not unsafe_allies and not known_edge_cells:
                break
            focus_cells = edge_enemies + known_edge_cells + unsafe_allies
            direction = self._edge_recenter_direction(
                focus_cells,
                x_bounds=self.ACTION_SAFE_X,
                y_bounds=(160, 1000)
                if edge_enemies or known_edge_cells
                else self.ACTION_SAFE_Y,
            )
            if direction is None:
                break
            logger.debug(
                "边缘单位重定位: "
                f"allies={len(unsafe_allies)}, enemies={len(edge_enemies)}, "
                f"hidden_targets={len(known_edge_cells)}, "
                f"direction={direction}"
            )
            pan_result = self._pan_world_once(
                context,
                (direction[1], direction[0]),
                round_seen,
                overlay_open=False,
                phase="边缘重定位",
                fine=True,
            )
            if pan_result is None:
                logger.warning(f"边缘重定位滑动 {direction} 失败")
                break
            did_pan, candidate_grid, candidate_img = pan_result
            if not did_pan or candidate_grid is None:
                break
            keeps_enemy = not edge_enemies or bool(candidate_grid.enemy_units)
            if not candidate_grid.self_units or not keeps_enemy:
                logger.warning("边缘重定位后无法同时保留敌我，撤销本次滑动")
                self._pan_world_once(
                    context,
                    (-direction[1], -direction[0]),
                    round_seen,
                    overlay_open=False,
                    phase="边缘重定位回滚",
                    fine=True,
                )
                break
            current_grid = candidate_grid
            latest_img = candidate_img
            moved = True

        return current_grid, moved, latest_img

    @classmethod
    def _edge_recenter_direction(
        cls,
        cells: Any,
        *,
        x_bounds: Optional[Tuple[int, int]] = None,
        y_bounds: Optional[Tuple[int, int]] = None,
    ) -> Optional[Tuple[int, int]]:
        """Return a cardinal or diagonal pan that pulls edge units inward."""

        if isinstance(cells, Cell):
            cells = [cells]
        cells = list(cells)
        if not cells:
            return None
        x_min, x_max = x_bounds or cls.ACTION_SAFE_X
        y_min, y_max = y_bounds or cls.ACTION_SAFE_Y

        left_overflow = max(
            (x_min - cell.safe_click_point()[0] for cell in cells), default=0
        )
        right_overflow = max(
            (cell.safe_click_point()[0] - x_max for cell in cells), default=0
        )
        top_overflow = max(
            (y_min - cell.safe_click_point()[1] for cell in cells), default=0
        )
        bottom_overflow = max(
            (cell.safe_click_point()[1] - y_max for cell in cells), default=0
        )
        direction_x = (
            -1
            if left_overflow > max(0, right_overflow)
            else (1 if right_overflow > 0 else 0)
        )
        direction_y = (
            -1
            if top_overflow > max(0, bottom_overflow)
            else (1 if bottom_overflow > 0 else 0)
        )
        return (direction_x, direction_y) if direction_x or direction_y else None

    @classmethod
    def _spiral_directions(cls, max_steps: int) -> List[Tuple[int, int]]:
        """生成右起步的顺时针搜索步；任一直线段最多移动三格。"""
        directions = [(1, 0), (0, 1), (-1, 0), (0, -1)]
        result: List[Tuple[int, int]] = []
        segment_length = 1
        direction_index = 0
        while len(result) < max_steps:
            for _ in range(2):
                direction = directions[direction_index % 4]
                direction_index += 1
                for _ in range(
                    min(segment_length, cls.MAX_STRAIGHT_SEARCH_SWIPES)
                ):
                    result.append(direction)
                    if len(result) >= max_steps:
                        return result
            segment_length += 1
        return result

    @classmethod
    def _has_safe_blue_evidence(cls, context: Context, img: Any) -> bool:
        """判断安全战场区域是否仍有被遮挡/截断的我方蓝色碎片。"""
        recognition = context.run_recognition("Battle_UnitScan_Blue", img)
        if not recognition.hit or not recognition.filtered_results:
            return False
        for result in recognition.filtered_results:
            x, y, width, height = (int(value) for value in result.box)
            if width < 2 or height < 3:
                continue
            center_x = x + width // 2
            center_y = y + height // 2
            if (
                cls.ACTION_SAFE_X[0]
                <= center_x
                <= cls.ACTION_SAFE_X[1]
                and cls.ACTION_SAFE_Y[0]
                <= center_y
                <= cls.ACTION_SAFE_Y[1]
            ):
                return True
        return False
