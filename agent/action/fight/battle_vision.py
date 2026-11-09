"""战斗画面图像分析。

只包含不依赖战斗实例状态的纯函数：颜色掩膜解析、威胁区域识别、
选中态菱形定位、红条像素统计与镜头位移估算。便于单独测试与复用。
"""

from typing import Any, List, Optional, Sequence, Set, Tuple

import cv2
from maa.context import Context

from .battle_grid import CELL_HEIGHT, CELL_WIDTH, COLS, ROWS, BattleGrid, Cell

# 战斗区域在 720x1280 下延伸到 y=1160，更下方属于系统 UI。
BATTLE_AREA_HEIGHT = 1160
BATTLE_AREA_WIDTH = 720

# 危险覆盖层的红色 HSV 区间。
THREAT_HSV_LOW = (2, 130, 75)
THREAT_HSV_HIGH = (20, 200, 165)

# 状态条窄横带的红色 HSV 区间。
HEALTH_HSV_LOW = (0, 80, 130)
HEALTH_HSV_HIGH = (22, 255, 255)

# 隐匿单位没有红色生命条，但底部“危险”开关仍会绘制其攻击覆盖区。
# 只接受至少接近一个 120x120 战斗格的大连通框，排除红旗、服装和
# 场景中的红色碎片；覆盖区只用于给出粗粒度追击方向，不冒充敌人坐标。
THREAT_REGION_MIN_BOX_AREA = 10000
THREAT_REGION_MIN_WIDTH = 90
THREAT_REGION_MIN_HEIGHT = 60

# 威胁色块描在敌人可攻击的格子上，敌人的立绘会把自己所在格的
# 红色遮罩遮住。因此不能把整片红区的包围盒中心当作敌人；要找的是
# 被红色威胁格从四周包住的“锯齿/空洞”中心。
THREAT_ORIGIN_CORE_RADIUS = 20
THREAT_ORIGIN_RING_RADIUS = 105
THREAT_ORIGIN_SAMPLE_STEP = 12
THREAT_ORIGIN_MIN_DISTANCE_FROM_ALLY = 55
THREAT_ORIGIN_MIN_SCORE = 0.38

# 威胁覆盖层与战斗网格严格按 120px 对齐。敌人的立绘会遮住自身格的
# 中心，但仍会露出一部分红色；以此识别“锯齿中间”的具体敌人格。
THREAT_CELL_MIN_COVERAGE = 0.08
THREAT_CELL_MAX_COVERAGE = 0.70
THREAT_CELL_CENTER_MAX_COVERAGE = 0.10
THREAT_CELL_NEIGHBOR_MIN_COVERAGE = 0.20
THREAT_CELL_MIN_RED_NEIGHBORS = 2

# 选中态一排菱形行动点的尺寸范围。
DIAMOND_MIN_WIDTH = 6
DIAMOND_MAX_WIDTH = 13
DIAMOND_MIN_HEIGHT = 5
DIAMOND_MAX_HEIGHT = 9
DIAMOND_MIN_AREA = 18
DIAMOND_MAX_AREA = 110
DIAMOND_MIN_CENTER_GAP = 10
DIAMOND_MAX_CENTER_GAP = 22
DIAMOND_MIN_ROW_SPAN = 30
DIAMOND_MAX_ROW_SPAN = 90


def filtered_boxes(
    context: Context, node_name: str, img: Any
) -> List[Tuple[int, int, int, int]]:
    """识别指定节点并返回过滤后的矩形框列表。"""
    recognition = context.run_recognition(node_name, img)
    if (
        not recognition
        or not recognition.hit
        or not recognition.filtered_results
    ):
        return []
    return [
        tuple(int(value) for value in result.box)  # type: ignore[misc]
        for result in recognition.filtered_results
    ]


def recognize_threat_regions(
    context: Context, img: Any
) -> List[Tuple[int, int, int, int]]:
    """返回当前画面中可信的危险覆盖区矩形框 ``(x, y, width, height)``。

    尺寸阈值用于排除红旗、服装和场景中的红色碎片，只保留至少接近
    一个 120x120 战斗格的大连通框。
    """
    credible: List[Tuple[int, int, int, int]] = []
    for x, y, width, height in filtered_boxes(
        context, "Battle_ThreatRegion", img
    ):
        if (
            width >= THREAT_REGION_MIN_WIDTH
            and height >= THREAT_REGION_MIN_HEIGHT
            and width * height >= THREAT_REGION_MIN_BOX_AREA
        ):
            credible.append((x, y, width, height))
    return credible


def largest_region_key(
    box: Tuple[int, int, int, int]
) -> Tuple[int, int, int, int]:
    """威胁区域框的比较键：面积优先，位置次之。"""
    x, y, width, height = box
    return (width * height, x, y, width, height)


def count_action_range(grid: BattleGrid) -> int:
    """统计战场快照中的可移动 / 可攻击格数量。"""
    return sum(
        1
        for cell in grid
        if cell.is_moveable or cell.is_attackable
    )


def threat_enemy_cell_from_mask(
    img: Any, allies: Sequence[Cell]
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
    mask = cv2.inRange(hsv, THREAT_HSV_LOW, THREAT_HSV_HIGH) > 0
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
                THREAT_CELL_MIN_COVERAGE
                <= cell_coverage
                <= THREAT_CELL_MAX_COVERAGE
            ):
                continue
            centre = mask[
                row * CELL_HEIGHT + 40 : row * CELL_HEIGHT + 80,
                col * CELL_WIDTH + 40 : col * CELL_WIDTH + 80,
            ]
            centre_coverage = float(centre.mean())
            if centre_coverage > THREAT_CELL_CENTER_MAX_COVERAGE:
                continue
            neighbours = [
                coverage.get((row + row_delta, col + col_delta), 0.0)
                for row_delta, col_delta in ((-1, 0), (1, 0), (0, -1), (0, 1))
            ]
            red_neighbours = sum(
                value >= THREAT_CELL_NEIGHBOR_MIN_COVERAGE
                for value in neighbours
            )
            if red_neighbours < THREAT_CELL_MIN_RED_NEIGHBORS:
                continue
            # Prefer a conspicuously occluded centre, then the candidate
            # closest to the surrounding red threat pattern.
            score = (
                2.5 * (THREAT_CELL_MAX_COVERAGE - cell_coverage)
                + sum(neighbours)
                + (THREAT_CELL_CENTER_MAX_COVERAGE - centre_coverage)
            )
            if best is None or score > best[0]:
                best = (score, row, col)
    if best is None:
        return None
    score, row, col = best
    return row, col, score


def threat_origin_from_mask(
    img: Any, allies: Sequence[Cell]
) -> Optional[Tuple[int, int, float]]:
    """Find a hidden enemy from the jagged centre of its threat overlay.

    The overlay is drawn on threatened cells, while an enemy sprite hides the
    overlay on its own cell.  Thus the enemy becomes a non-red hole surrounded
    by red cells.  Friendly-unit holes are rejected from their known positions.
    The result is used only as a pursuit direction, never as a click target.
    """
    if img is None or len(img.shape) < 2:
        return None

    height = min(int(img.shape[0]), BATTLE_AREA_HEIGHT)
    width = min(int(img.shape[1]), BATTLE_AREA_WIDTH)
    ring = THREAT_ORIGIN_RING_RADIUS
    core = THREAT_ORIGIN_CORE_RADIUS
    if height <= ring * 2 or width <= ring * 2:
        return None

    hsv = cv2.cvtColor(
        img[:height, :width], cv2.COLOR_BGR2HSV
    )
    red_mask = cv2.inRange(hsv, THREAT_HSV_LOW, THREAT_HSV_HIGH)
    mask = (red_mask > 0).astype("uint8")
    if int(mask.sum()) < THREAT_REGION_MIN_BOX_AREA // 8:
        return None

    ally_points = [cell.safe_click_point() for cell in allies]
    best: Optional[Tuple[float, int, int]] = None
    for y in range(ring, height - ring, THREAT_ORIGIN_SAMPLE_STEP):
        for x in range(ring, width - ring, THREAT_ORIGIN_SAMPLE_STEP):
            if mask[y, x]:
                continue
            if any(
                (x - ally_x) ** 2 + (y - ally_y) ** 2
                < THREAT_ORIGIN_MIN_DISTANCE_FROM_ALLY ** 2
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
            if weakest < 0.20 or score < THREAT_ORIGIN_MIN_SCORE:
                continue
            if best is None or score > best[0]:
                best = (score, x, y)

    if best is None:
        return None
    score, x, y = best
    return x, y, score


def selected_self_cells(img: Any) -> Set[Tuple[int, int]]:
    """从选中画面的一排蓝色菱形行动点定位我方所在格。"""
    if img is None or len(img.shape) < 2:
        return set()
    height = min(int(img.shape[0]), BATTLE_AREA_HEIGHT)
    width = min(int(img.shape[1]), BATTLE_AREA_WIDTH)
    hsv = cv2.cvtColor(
        img[:height, :width], cv2.COLOR_BGR2HSV
    )
    mask = cv2.inRange(hsv, (95, 80, 130), (125, 255, 255))
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    fragments = []
    for x, y, box_width, box_height, area in stats[1:]:
        if (
            DIAMOND_MIN_WIDTH <= box_width <= DIAMOND_MAX_WIDTH
            and DIAMOND_MIN_HEIGHT <= box_height <= DIAMOND_MAX_HEIGHT
            and DIAMOND_MIN_AREA <= area <= DIAMOND_MAX_AREA
        ):
            fragments.append(
                (
                    int(x),
                    int(y),
                    int(box_width),
                    int(box_height),
                )
            )

    y_groups: List[List[Tuple[int, int, int, int]]] = []
    for fragment in sorted(
        fragments, key=lambda item: item[1] + item[3] // 2
    ):
        center_y = fragment[1] + fragment[3] // 2
        group = next(
            (
                candidate
                for candidate in y_groups
                if abs(
                    center_y
                    - sum(
                        item[1] + item[3] // 2 for item in candidate
                    )
                    / len(candidate)
                )
                <= 3
            ),
            None,
        )
        if group is None:
            y_groups.append([fragment])
        else:
            group.append(fragment)

    cells: Set[Tuple[int, int]] = set()
    for group in y_groups:
        sequence: List[Tuple[int, int, int, int]] = []
        for fragment in sorted(group, key=lambda item: item[0]):
            if sequence:
                previous = sequence[-1]
                center_gap = (
                    fragment[0]
                    + fragment[2] // 2
                    - previous[0]
                    - previous[2] // 2
                )
                if not (
                    DIAMOND_MIN_CENTER_GAP
                    <= center_gap
                    <= DIAMOND_MAX_CENTER_GAP
                ):
                    if len(sequence) >= 3:
                        append_selected_cell(cells, sequence)
                    sequence = []
            sequence.append(fragment)
        if len(sequence) >= 3:
            append_selected_cell(cells, sequence)
    return cells


def append_selected_cell(
    cells: Set[Tuple[int, int]],
    sequence: Sequence[Tuple[int, int, int, int]],
) -> None:
    """校验一排菱形的尺寸，并把其中心换算成局部网格。"""
    left = sequence[0][0]
    right = sequence[-1][0] + sequence[-1][2]
    if not DIAMOND_MIN_ROW_SPAN <= right - left <= DIAMOND_MAX_ROW_SPAN:
        return
    center_x = (left + right) // 2
    center_y = int(
        sum(item[1] + item[3] // 2 for item in sequence) / len(sequence)
    )
    row = center_y // CELL_HEIGHT
    col = center_x // CELL_WIDTH
    if 0 <= row < ROWS and 0 <= col < COLS:
        cells.add((row, col))


def red_bar_pixels(img: Any, point: Tuple[int, int]) -> int:
    """统计目标状态条附近的红色像素，用于确认攻击造成的变化。"""
    if img is None:
        return 0
    center_x, center_y = point
    height, width = img.shape[:2]
    left = max(0, center_x - 70)
    right = min(width, center_x + 71)
    # 只统计血条本身的窄横带。旧的 ±16px 会把攻击预览的红色角标
    # 一并算入，确认点击后角标消失就会被误判为“目标扣血”。
    top = max(0, center_y - 4)
    bottom = min(height, center_y + 5)
    if left >= right or top >= bottom:
        return 0
    hsv = cv2.cvtColor(
        img[top:bottom, left:right], cv2.COLOR_BGR2HSV
    )
    mask = cv2.inRange(hsv, HEALTH_HSV_LOW, HEALTH_HSV_HIGH)
    return int(cv2.countNonZero(mask))


def frame_difference(first: Any, second: Any) -> float:
    """整屏缩略后的平均像素差异，用于粗略判断画面是否发生变化。"""
    first_roi = cv2.resize(first[:BATTLE_AREA_HEIGHT], (180, 290))
    second_roi = cv2.resize(second[:BATTLE_AREA_HEIGHT], (180, 290))
    return float(cv2.absdiff(first_roi, second_roi).mean())


def camera_motion(first: Any, second: Any) -> Tuple[float, float, float]:
    """用背景相位相关估算真实镜头平移，忽略人物待机动画。"""
    if first is None or second is None:
        return 0.0, 0.0, 0.0
    first_gray = cv2.cvtColor(first[100:1080], cv2.COLOR_BGR2GRAY)
    second_gray = cv2.cvtColor(second[100:1080], cv2.COLOR_BGR2GRAY)
    first_small = cv2.resize(first_gray, (360, 490))
    second_small = cv2.resize(second_gray, (360, 490))
    first_edges = cv2.Sobel(first_small, cv2.CV_32F, 1, 1)
    second_edges = cv2.Sobel(second_small, cv2.CV_32F, 1, 1)
    (shift_x, shift_y), response = cv2.phaseCorrelate(
        first_edges, second_edges
    )
    return shift_x * 2.0, shift_y * 2.0, float(response)


def camera_shift_is_real(
    shift_x: float, shift_y: float, response: float
) -> bool:
    """判断背景位移是否来自真正的镜头平移，而不是人物动画。"""
    distance_squared = shift_x * shift_x + shift_y * shift_y
    # 整屏滑动实测约 240~480px。尸体、人物动画会降低相位相关
    # response，但不可能凭空产生这种量级的背景位移；大位移直接接纳。
    # 小幅居中仍要求较高置信度，避免把待机动画误当成镜头移动。
    return distance_squared >= 10000 or (
        response >= 0.08 and distance_squared >= 625
    )


def round_marker_difference(
    first: Any, second: Any, roi: Tuple[int, int, int, int]
) -> float:
    """比较常驻回合数字区域的平均差异。"""
    x, y, width, height = roi
    if (
        first is None
        or second is None
        or first.shape[0] < y + height
        or second.shape[0] < y + height
        or first.shape[1] < x + width
        or second.shape[1] < x + width
    ):
        return 0.0
    first_roi = first[y : y + height, x : x + width]
    second_roi = second[y : y + height, x : x + width]
    return float(cv2.absdiff(first_roi, second_roi).mean())
