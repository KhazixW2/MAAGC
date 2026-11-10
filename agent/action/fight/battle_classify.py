"""识别层：判断一格 / 一帧里到底有什么。

只做“看画面 → 出结论”，不改画面、不做取舍：

- :class:`GridClassifier`
  把当前截图识别成局部网格：每格是空的、我方、敌方、友军还是场地
  （祭坛 / 石碑），以及哪些格可攻击、哪些格可移动。
- :class:`ThreatClassifier`
  危险覆盖层里的隐匿敌人：锯齿空洞中心、覆盖区矩形框。

“这一回合该打谁、该往哪走”属于策略层，不在这里做。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple

import cv2
from maa.context import Context

from utils import logger
from utils.screen import screencap

from . import battle_vision
from .battle_map import (
    CELL_HEIGHT,
    CELL_WIDTH,
    COLS,
    ROWS,
    BattleGrid,
    Cell,
    CellKind,
)


class GridClassifier:
    """战场网格识别器：识别单位类型和可攻击/可移动范围。"""

    UNIT_NODES = {
        CellKind.SELF: "Battle_UnitScan_Blue",
        CellKind.ENEMY: "Battle_UnitScan_Red",
        CellKind.FRIEND: "Battle_UnitScan_Green",
    }

    # 血条/状态条通常由多个水平短色块组成。较高的数字、旗帜和人物服装
    # 不参与单位定位，避免把同一人物拆成许多“敌人”。
    BAR_SEGMENT_MIN_WIDTH = 6
    BAR_SEGMENT_MIN_HEIGHT = 4
    BAR_SEGMENT_MAX_HEIGHT = 6
    BAR_CLUSTER_MIN_SEGMENTS = 2
    BAR_CLUSTER_MIN_SPAN = 35
    # 树木、人物等前景会把状态条遮成两段。只接受“两个以上、同一水平线、
    # 总着色宽度足够”的紧凑短条，兼容局部遮挡而不放宽单个场景色块。
    OCCLUDED_BAR_MIN_SPAN = 20
    OCCLUDED_BAR_MIN_COLOR_WIDTH = 20
    # 贴到屏幕左右边缘的角色血条会被视口裁掉，实测双子祭司只剩约 30px。
    # 仅在边缘小范围内放宽，避免把场景中央的服装碎片当成角色。
    EDGE_BAR_MIN_SPAN = 25
    EDGE_BAR_MARGIN = 45
    # 地图到达下边界时，角色状态条会被底部 HUD 裁成两三个短段；
    # 只在 HUD 上沿附近接受更短的组合，避免放宽整个战场。
    BOTTOM_BAR_MIN_SPAN = 20
    BOTTOM_BAR_MIN_Y = 1120
    BAR_CLUSTER_Y_TOLERANCE = 6
    BAR_CLUSTER_X_GAP = 15
    # 活体状态条左侧固定带同色生命数字。尸体衣物也可能恰好拼成几段
    # 水平红块，但不会同时出现 720p 下约 5~18 x 9~24 的数字笔画。
    VITALITY_DIGIT_MIN_WIDTH = 5
    VITALITY_DIGIT_MAX_WIDTH = 18
    VITALITY_DIGIT_MIN_HEIGHT = 9
    VITALITY_DIGIT_MAX_HEIGHT = 24
    VITALITY_DIGIT_MIN_X_OFFSET = 20
    VITALITY_DIGIT_MAX_X_OFFSET = 85
    VITALITY_DIGIT_Y_TOLERANCE = 20
    # 祭坛、石碑等大型目标的“数字 + 血条”会连成一个较高的整体，
    # 不具备人物血条的分段结构，但横向跨度足够大，可以单独接受。
    SOLID_BAR_MIN_WIDTH = 50
    SOLID_BAR_MIN_HEIGHT = 8
    SOLID_BAR_MAX_HEIGHT = 50
    SOLID_BAR_EXPECTED_HALF_WIDTH = 58
    SOLID_BAR_UNIT_Y_OFFSET = 100

    # 范围标记应当是一个有明显尺寸的区域，而不是人物服装上的小色块。
    # 可点击范围格在 720p 下约为 98x98。人物、武器会把其后的色块
    # 切成较小碎片；这些碎片的中心经常落在人物上，不能作为点击目标。
    RANGE_MIN_WIDTH = 75
    RANGE_MIN_HEIGHT = 75

    # 敌人站在黄色攻击格内时，人物会把完整边框切成四段角标。
    # 攻击格中心大约位于血条中心上方 40px；直接围绕当前敌方血条
    # 聚合角标，比把动态网格硬套到固定 row/col 更可靠。
    ATTACK_MARKER_Y_OFFSET = 40
    ATTACK_MARKER_HALF_SIZE = 58
    ATTACK_MARKER_MIN_SPAN = 70

    # 后写入的单位类型优先级更高。我方状态条比环境中的红色装饰更可靠。
    # 优先级只看“身份”（谁在这个格子里），形态（是否实体长条）单独
    # 记在 Cell.is_environment 上，两者正交。
    _PRIORITY = {
        CellKind.EMPTY: 0,
        CellKind.ENEMY: 1,
        CellKind.FRIEND: 2,
        CellKind.SELF: 3,
    }

    def scan_grid(
        self,
        grid: BattleGrid,
        context: Context,
        img: Optional[Any] = None,
        cell_types: Optional[Iterable[CellKind]] = None,
    ) -> Any:
        """基于一张当前截图重建单位快照，并返回所使用的截图。

        ``cell_types`` 用于选中角色后的覆盖层画面。此时蓝色移动格会污染
        我方蓝色状态条识别，因此只重扫敌方，保留同帧敌人与范围坐标。
        """
        if img is None:
            img = screencap(context)

        grid.reset()

        requested_types = (
            set(cell_types) if cell_types is not None else set(self.UNIT_NODES)
        )
        for cell_kind, node_name in self.UNIT_NODES.items():
            if cell_kind not in requested_types:
                continue
            reco = context.run_recognition(node_name, img)
            results = reco.filtered_results if reco.hit and reco.filtered_results else []
            boxes = [tuple(int(value) for value in result.box) for result in results]
            unit_cells = self._find_status_bar_cells(boxes)
            if cell_kind == CellKind.ENEMY:
                before_count = len(unit_cells)
                # 红色节点同时命中尸体衣物、技能特效。实体长条（祭坛、
                # 石碑）无条件保留，普通红条必须带生命数字才算活体单位。
                unit_cells = [
                    unit
                    for unit in unit_cells
                    if unit[-1]
                    or self._has_vitality_digit(boxes, unit[2], unit[3])
                ]
                rejected = before_count - len(unit_cells)
                if rejected:
                    logger.debug(
                        f"{node_name}: 剔除 {rejected} 个无生命数字的红色碎片"
                    )
            logger.debug(
                f"{node_name}: 原始色块={len(boxes)}, 状态条单位={len(unit_cells)}"
            )

            for (
                row,
                col,
                center_x,
                center_y,
                target_x,
                target_y,
                is_solid_bar,
            ) in unit_cells:
                cell = grid.get_cell(row, col)
                if cell is None or self._PRIORITY[cell.kind] > self._PRIORITY[cell_kind]:
                    continue
                cell.kind = cell_kind
                cell.is_environment = is_solid_bar
                # 单位与点击坐标来自同一个状态条聚类。格子只负责决策，
                # 不再用固定格心覆盖当前画面的真实坐标。
                cell.unit_center = (center_x, center_y)
                if (target_x, target_y) != (center_x, center_y):
                    cell.target_center = (target_x, target_y)

        accepted_count = sum(cell.kind != CellKind.EMPTY for cell in grid)
        logger.debug(f"当前战场快照接受单位数: {accepted_count}")
        return img

    def _find_status_bar_cells(
        self, boxes: Iterable[Tuple[int, int, int, int]]
    ) -> List[Tuple[int, int, int, int, int, int, bool]]:
        """把分段状态条聚类为单位所在格子。

        末位布尔值表示该状态条是不是实体长条（祭坛、石碑等场地目标），
        颜色身份由调用方按节点给出，两者不能互相替代。
        """
        candidates = []
        solid_bars: List[List[Tuple[int, int, int, int]]] = []
        flat_bars: List[List[Tuple[int, int, int, int]]] = []
        for x, y, width, height in boxes:
            if (
                width >= self.SOLID_BAR_MIN_WIDTH
                and self.SOLID_BAR_MIN_HEIGHT <= height
                <= self.SOLID_BAR_MAX_HEIGHT
            ):
                solid_bars.append([(x, y, width, height)])
                continue
            # 满血人物的状态条有时会连成一个扁平长块，尤其在屏幕边缘
            # 被裁切后更常见。它和“数字 + 血条”连成的高块不同，仍应
            # 作为普通人物单位，不能过滤成祭坛等环境物件。
            if (
                width >= self.BAR_CLUSTER_MIN_SPAN
                and self.BAR_SEGMENT_MIN_HEIGHT
                <= height
                <= self.BAR_SEGMENT_MAX_HEIGHT
            ):
                flat_bars.append([(x, y, width, height)])
                continue
            if (
                width < self.BAR_SEGMENT_MIN_WIDTH
                or height < self.BAR_SEGMENT_MIN_HEIGHT
                or height > self.BAR_SEGMENT_MAX_HEIGHT
            ):
                continue
            candidates.append((x, y, width, height))

        # 先按水平线聚类，再用横向空隙拆成一条条状态栏。若先按格子
        # 切分，跨格的同一血条会被拆开，相邻角色的血条反而可能拼接。
        y_clusters: List[List[Tuple[int, int, int, int]]] = []
        for box in sorted(candidates, key=lambda item: item[1] + item[3] // 2):
            center_y = box[1] + box[3] // 2
            cluster = next(
                (
                    group
                    for group in y_clusters
                    if abs(
                        center_y
                        - sum(item[1] + item[3] // 2 for item in group)
                        / len(group)
                    )
                    <= self.BAR_CLUSTER_Y_TOLERANCE
                ),
                None,
            )
            if cluster is None:
                y_clusters.append([box])
            else:
                cluster.append(box)

        bars: List[List[Tuple[int, int, int, int]]] = [
            *solid_bars,
            *flat_bars,
        ]
        for y_cluster in y_clusters:
            current_bar: List[Tuple[int, int, int, int]] = []
            current_right = -1
            for box in sorted(y_cluster, key=lambda item: item[0]):
                if (
                    current_bar
                    and box[0] - current_right > self.BAR_CLUSTER_X_GAP
                ):
                    bars.append(current_bar)
                    current_bar = []
                    current_right = -1
                current_bar.append(box)
                current_right = max(current_right, box[0] + box[2])
            if current_bar:
                bars.append(current_bar)

        best_by_cell = {}
        for bar in bars:
            left = min(item[0] for item in bar)
            right = max(item[0] + item[2] for item in bar)
            top = min(item[1] for item in bar)
            bottom = max(item[1] + item[3] for item in bar)
            span = right - left
            is_solid_bar = (
                len(bar) == 1
                and bar[0][2] >= self.SOLID_BAR_MIN_WIDTH
                and self.SOLID_BAR_MIN_HEIGHT
                <= bar[0][3]
                <= self.SOLID_BAR_MAX_HEIGHT
            )
            is_edge_clipped_bar = (
                span >= self.EDGE_BAR_MIN_SPAN
                and (
                    left <= self.EDGE_BAR_MARGIN
                    or right >= COLS * CELL_WIDTH - self.EDGE_BAR_MARGIN
                )
                and all(
                    item[3] <= self.BAR_SEGMENT_MAX_HEIGHT for item in bar
                )
            )
            is_bottom_clipped_bar = (
                len(bar) >= self.BAR_CLUSTER_MIN_SEGMENTS
                and span >= self.BOTTOM_BAR_MIN_SPAN
                and top >= self.BOTTOM_BAR_MIN_Y
                and all(
                    item[3] <= self.BAR_SEGMENT_MAX_HEIGHT for item in bar
                )
            )
            is_occluded_bar = (
                len(bar) >= self.BAR_CLUSTER_MIN_SEGMENTS
                and span >= self.OCCLUDED_BAR_MIN_SPAN
                and sum(item[2] for item in bar)
                >= self.OCCLUDED_BAR_MIN_COLOR_WIDTH
                and max(item[2] for item in bar) >= 12
                and max(item[1] for item in bar)
                - min(item[1] for item in bar)
                <= 2
                and all(
                    self.BAR_SEGMENT_MIN_HEIGHT
                    <= item[3]
                    <= self.BAR_SEGMENT_MAX_HEIGHT
                    for item in bar
                )
            )
            if (
                not is_solid_bar
                and not is_edge_clipped_bar
                and not is_bottom_clipped_bar
                and not is_occluded_bar
                and (
                    span < self.BAR_CLUSTER_MIN_SPAN
                    or (
                        len(bar) < self.BAR_CLUSTER_MIN_SEGMENTS
                        and bar[0][3] > self.BAR_SEGMENT_MAX_HEIGHT
                    )
                )
            ):
                continue

            center_x = (left + right) // 2
            center_y = (top + bottom) // 2
            target_x = center_x
            target_y = center_y
            if is_solid_bar:
                # 大型目标的血条位于本体下方。决策归格、黄色攻击格关联
                # 和最终点击都应使用本体位置，而不是血条所在的下一行。
                # 被人物遮挡时通常只剩血条左半段，按完整血条的实测半宽
                # 恢复横向中心，避免攻击角标聚合区域向左偏移。
                center_x = min(
                    COLS * CELL_WIDTH - 1,
                    left
                    + max(
                        span // 2,
                        self.SOLID_BAR_EXPECTED_HALF_WIDTH,
                    ),
                )
                center_y = max(0, top - self.SOLID_BAR_UNIT_Y_OFFSET)
                target_x = center_x
                target_y = (top + bottom) // 2
            row = center_y // CELL_HEIGHT
            col = center_x // CELL_WIDTH
            if not (0 <= row < ROWS and 0 <= col < COLS):
                continue

            key = (row, col)
            previous = best_by_cell.get(key)
            if previous is None or span > previous[0]:
                best_by_cell[key] = (
                    span,
                    center_x,
                    center_y,
                    target_x,
                    target_y,
                    is_solid_bar,
                )

        return sorted(
            [
                (
                    row,
                    col,
                    center_x,
                    center_y,
                    target_x,
                    target_y,
                    is_solid_bar,
                )
                for (row, col), (
                    _,
                    center_x,
                    center_y,
                    target_x,
                    target_y,
                    is_solid_bar,
                ) in best_by_cell.items()
            ],
            key=lambda item: (item[0], item[1]),
        )

    @classmethod
    def _has_vitality_digit(
        cls,
        boxes: Iterable[Tuple[int, int, int, int]],
        bar_center_x: int,
        bar_center_y: int,
    ) -> bool:
        """确认状态条左侧存在同色生命数字笔画。"""
        for x, y, width, height in boxes:
            if not (
                cls.VITALITY_DIGIT_MIN_WIDTH
                <= width
                <= cls.VITALITY_DIGIT_MAX_WIDTH
                and cls.VITALITY_DIGIT_MIN_HEIGHT
                <= height
                <= cls.VITALITY_DIGIT_MAX_HEIGHT
            ):
                continue
            center_x = x + width // 2
            center_y = y + height // 2
            x_offset = bar_center_x - center_x
            if (
                cls.VITALITY_DIGIT_MIN_X_OFFSET
                <= x_offset
                <= cls.VITALITY_DIGIT_MAX_X_OFFSET
                and abs(center_y - bar_center_y)
                <= cls.VITALITY_DIGIT_Y_TOLERANCE
            ):
                return True
        return False

    def scan_ranges(
        self, grid: BattleGrid, context: Context, img: Optional[Any] = None
    ) -> Any:
        """在同一张当前截图上扫描攻击/移动范围。"""
        if img is None:
            img = screencap(context)

        grid.reset_flags()
        # 检测攻击范围（红色）
        self._detect_range(grid, img, "attack", context)
        # 检测移动范围（绿色）
        self._detect_range(grid, img, "move", context)
        return img

    def _detect_range(
        self, grid: BattleGrid, img: Any, range_type: str, context: Context
    ):
        """检测并标记某种范围"""
        node_name = f"Battle_{range_type.capitalize()}Range"
        reco = context.run_recognition(node_name, img)

        if not reco.hit or not reco.filtered_results:
            logger.debug(f"{range_type} 范围识别未命中")
            return

        results = list(reco.filtered_results)
        if range_type == "attack":
            self._mark_attackable_enemies(grid, results)

        best_by_cell = {}
        for result in results:
            x, y, width, height = (int(value) for value in result.box)
            if width < self.RANGE_MIN_WIDTH or height < self.RANGE_MIN_HEIGHT:
                continue

            # 范围框用中心点归格，不能再用左上角归格、中心点点击。
            cell_row = (y + height // 2) // CELL_HEIGHT
            cell_col = (x + width // 2) // CELL_WIDTH
            cell = grid.get_cell(cell_row, cell_col)
            if cell is None:
                continue

            area = width * height
            key = (cell_row, cell_col)
            previous = best_by_cell.get(key)
            if previous is None or area > previous[0]:
                best_by_cell[key] = (
                    area,
                    (x + width // 2, y + height // 2),
                )

        for (cell_row, cell_col), (_, center) in best_by_cell.items():
            cell = grid.cells[cell_row][cell_col]
            if range_type == "attack":
                cell.is_attackable = True
                # 敌人占据攻击格时，完整范围框常被本体切碎。上面的
                # _mark_attackable_enemies 已根据血条和黄色角标恢复了本体
                # 点击点，不能再被同格内某个残缺范围块的中心覆盖；大型
                # 祭坛会因此点到本体下方的空地。
                if cell.attack_center == (0, 0):
                    cell.attack_center = center
            else:
                cell.is_moveable = True
                cell.move_center = center

        logger.debug(f"{node_name}: 接受范围格={len(best_by_cell)}")

    def _mark_attackable_enemies(
        self, grid: BattleGrid, results: Iterable[Any]
    ) -> None:
        """识别被人物遮断的黄色攻击框，并绑定到真实敌方血条。"""
        boxes = [tuple(int(value) for value in result.box) for result in results]
        accepted = []
        for enemy in grid.enemy_units:
            # 普通人物的 unit_center 就是血条；大型祭坛的 unit_center
            # 为了归格已上移到本体，而 target_center 才保留真实血条。
            # 攻击格中心统一位于血条上方约 40px，祭坛若继续基于
            # unit_center 计算会再上移约 100px，点到石碑上半部。
            if enemy.target_center != (0, 0):
                center_x, bar_y = enemy.target_center
            else:
                center_x, bar_y = enemy.safe_click_point()
            center_y = bar_y - self.ATTACK_MARKER_Y_OFFSET
            left = center_x - self.ATTACK_MARKER_HALF_SIZE
            right = center_x + self.ATTACK_MARKER_HALF_SIZE
            top = center_y - self.ATTACK_MARKER_HALF_SIZE
            bottom = center_y + self.ATTACK_MARKER_HALF_SIZE

            overlaps = []
            for x, y, width, height in boxes:
                overlap_left = max(left, x)
                overlap_right = min(right, x + width)
                overlap_top = max(top, y)
                overlap_bottom = min(bottom, y + height)
                if overlap_left >= overlap_right or overlap_top >= overlap_bottom:
                    continue
                overlaps.append(
                    (overlap_left, overlap_top, overlap_right, overlap_bottom)
                )

            if not overlaps:
                continue
            marker_width = max(box[2] for box in overlaps) - min(
                box[0] for box in overlaps
            )
            marker_height = max(box[3] for box in overlaps) - min(
                box[1] for box in overlaps
            )
            if (
                marker_width < self.ATTACK_MARKER_MIN_SPAN
                or marker_height < self.ATTACK_MARKER_MIN_SPAN
            ):
                continue

            enemy.is_attackable = True
            enemy.attack_center = (center_x, center_y)
            accepted.append((enemy.row, enemy.col, center_x, bar_y))

        if accepted:
            logger.debug(f"黄色角标确认可攻击敌人: {accepted}")

    def promote_occluded_adjacent_targets(
        self, grid: BattleGrid, ally: Cell
    ) -> List[Cell]:
        """Recover an adjacent enemy whose yellow target frame is occluded.

        Large altars and two overlapping units can leave only a few yellow
        corner fragments on the enemy cell.  A selected frame that still has
        other attack-range cells proves the yellow layer is active; combined
        with a current real red status bar in an adjacent cell this is enough
        to attempt the user's normal double-click attack.  Result verification
        still rejects a click that the game does not accept.
        """

        has_attack_layer = any(cell.is_attackable for cell in grid)
        if not has_attack_layer:
            return []
        promoted: List[Cell] = []
        for enemy in grid.enemy_units:
            if enemy.is_attackable or enemy.is_moveable:
                continue
            if max(abs(enemy.row - ally.row), abs(enemy.col - ally.col)) > 1:
                continue
            center_x, bar_y = (
                enemy.target_center
                if enemy.target_center != (0, 0)
                else enemy.safe_click_point()
            )
            enemy.is_attackable = True
            enemy.attack_center = (
                center_x,
                max(1, bar_y - self.ATTACK_MARKER_Y_OFFSET),
            )
            promoted.append(enemy)
        if promoted:
            logger.debug(
                "相邻敌人攻击框被本体遮挡，按红条与攻击层恢复目标: "
                + str([(cell.row, cell.col) for cell in promoted])
            )
        return promoted


class ThreatClassifier:
    """危险覆盖层里的隐匿敌人识别。

    隐匿敌人没有红色血条，但底部“危险”开关仍会绘制其攻击覆盖区。
    识别结果只用于给出粗粒度追击方向，绝不冒充敌人坐标。
    """

    def hidden_enemy_cell(
        self, img: Any, allies: Sequence[Cell]
    ) -> Optional[Tuple[int, int, float]]:
        """Locate the enemy's grid cell from the jagged hole in danger tiles.

        A hidden enemy does not remove the red mask completely: its sprite
        covers the centre of its own 120px cell while the ground still shows
        around the edges.  Normal danger cells retain a red centre, and empty
        terrain has no red coverage at all.  Requiring two adjacent danger
        cells keeps grass and tree holes from becoming targets.
        """
        if img is None or len(img.shape) < 2:
            return None

        height = min(int(img.shape[0]), ROWS * CELL_HEIGHT)
        width = min(int(img.shape[1]), COLS * CELL_WIDTH)
        if height < ROWS * CELL_HEIGHT or width < COLS * CELL_WIDTH:
            return None
        hsv = cv2.cvtColor(
            img[:height, :width], cv2.COLOR_BGR2HSV
        )
        mask = cv2.inRange(hsv, battle_vision.THREAT_HSV_LOW, battle_vision.THREAT_HSV_HIGH) > 0
        coverage = {
            (row, col): float(
                mask[
                    row * CELL_HEIGHT : (row + 1) * CELL_HEIGHT,
                    col * CELL_WIDTH : (col + 1) * CELL_WIDTH,
                ].mean()
            )
            for row in range(ROWS)
            for col in range(COLS)
        }
        ally_cells = {(cell.row, cell.col) for cell in allies}
        best: Optional[Tuple[float, int, int]] = None
        for row in range(ROWS):
            for col in range(COLS):
                if (row, col) in ally_cells:
                    continue
                cell_coverage = coverage[(row, col)]
                if not (
                    battle_vision.THREAT_CELL_MIN_COVERAGE
                    <= cell_coverage
                    <= battle_vision.THREAT_CELL_MAX_COVERAGE
                ):
                    continue
                centre = mask[
                    row * CELL_HEIGHT + 40 : row * CELL_HEIGHT + 80,
                    col * CELL_WIDTH + 40 : col * CELL_WIDTH + 80,
                ]
                centre_coverage = float(centre.mean())
                if centre_coverage > battle_vision.THREAT_CELL_CENTER_MAX_COVERAGE:
                    continue
                neighbours = [
                    coverage.get((row + row_delta, col + col_delta), 0.0)
                    for row_delta, col_delta in ((-1, 0), (1, 0), (0, -1), (0, 1))
                ]
                red_neighbours = sum(
                    value >= battle_vision.THREAT_CELL_NEIGHBOR_MIN_COVERAGE
                    for value in neighbours
                )
                if red_neighbours < battle_vision.THREAT_CELL_MIN_RED_NEIGHBORS:
                    continue
                # Prefer a conspicuously occluded centre, then the candidate
                # closest to the surrounding red threat pattern.
                score = (
                    2.5 * (battle_vision.THREAT_CELL_MAX_COVERAGE - cell_coverage)
                    + sum(neighbours)
                    + (battle_vision.THREAT_CELL_CENTER_MAX_COVERAGE - centre_coverage)
                )
                if best is None or score > best[0]:
                    best = (score, row, col)
        if best is None:
            return None
        score, row, col = best
        return row, col, score

    def threat_origin(
        self, img: Any, allies: Sequence[Cell]
    ) -> Optional[Tuple[int, int, float]]:
        """Find a hidden enemy from the jagged centre of its threat overlay.

        The overlay is drawn on threatened cells, while an enemy sprite hides the
        overlay on its own cell.  Thus the enemy becomes a non-red hole surrounded
        by red cells.  Friendly-unit holes are rejected from their known positions.
        The result is used only as a pursuit direction, never as a click target.
        """
        if img is None or len(img.shape) < 2:
            return None

        height = min(int(img.shape[0]), battle_vision.BATTLE_AREA_HEIGHT)
        width = min(int(img.shape[1]), battle_vision.BATTLE_AREA_WIDTH)
        ring = battle_vision.THREAT_ORIGIN_RING_RADIUS
        core = battle_vision.THREAT_ORIGIN_CORE_RADIUS
        if height <= ring * 2 or width <= ring * 2:
            return None

        hsv = cv2.cvtColor(
            img[:height, :width], cv2.COLOR_BGR2HSV
        )
        red_mask = cv2.inRange(
            hsv, battle_vision.THREAT_HSV_LOW, battle_vision.THREAT_HSV_HIGH
        )
        mask = (red_mask > 0).astype("uint8")
        if int(mask.sum()) < battle_vision.THREAT_REGION_MIN_BOX_AREA // 8:
            return None

        ally_points = [cell.safe_click_point() for cell in allies]
        best: Optional[Tuple[float, int, int]] = None
        for y in range(ring, height - ring, battle_vision.THREAT_ORIGIN_SAMPLE_STEP):
            for x in range(ring, width - ring, battle_vision.THREAT_ORIGIN_SAMPLE_STEP):
                if mask[y, x]:
                    continue
                if any(
                    (x - ally_x) ** 2 + (y - ally_y) ** 2
                    < battle_vision.THREAT_ORIGIN_MIN_DISTANCE_FROM_ALLY ** 2
                    for ally_x, ally_y in ally_points
                ):
                    continue

                core_coverage = float(
                    mask[y - core : y + core, x - core : x + core].mean()
                )
                if core_coverage > 0.30:
                    continue
                coverage = (
                    float(mask[y - ring : y - core, x - core : x + core].mean()),
                    float(mask[y + core : y + ring, x - core : x + core].mean()),
                    float(mask[y - core : y + core, x - ring : x - core].mean()),
                    float(mask[y - core : y + core, x + core : x + ring].mean()),
                )
                weakest = min(coverage)
                score = weakest + sum(coverage) / 8 - core_coverage * 0.25
                if weakest < 0.20 or score < battle_vision.THREAT_ORIGIN_MIN_SCORE:
                    continue
                if best is None or score > best[0]:
                    best = (score, x, y)

        if best is None:
            return None
        score, x, y = best
        return x, y, score

    def visible_regions(
        self, context: Context, img: Any
    ) -> List[Tuple[int, int, int, int]]:
        """返回当前画面中可信的危险覆盖区矩形框 ``(x, y, width, height)``。

        尺寸阈值用于排除红旗、服装和场景中的红色碎片，只保留至少接近
        一个 120x120 战斗格的大连通框。
        """
        credible: List[Tuple[int, int, int, int]] = []
        for x, y, width, height in battle_vision.filtered_boxes(
            context, "Battle_ThreatRegion", img
        ):
            if (
                width >= battle_vision.THREAT_REGION_MIN_WIDTH
                and height >= battle_vision.THREAT_REGION_MIN_HEIGHT
                and width * height >= battle_vision.THREAT_REGION_MIN_BOX_AREA
            ):
                credible.append((x, y, width, height))
        return credible

    @staticmethod
    def largest_region_key(
        box: Tuple[int, int, int, int]
    ) -> Tuple[int, int, int, int]:
        """威胁区域框的比较键：面积优先，位置次之。"""
        x, y, width, height = box
        return (width * height, x, y, width, height)
