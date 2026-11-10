"""战斗画面图像分析。

只包含不依赖战斗实例状态的纯函数：颜色掩膜解析、选中态菱形定位、
红条像素统计与镜头位移估算。便于单独测试与复用。

威胁覆盖层的识别（锯齿空洞中心、覆盖区矩形框）属于识别层，
在 :mod:`battle_classify` 中。
"""

from typing import Any, List, Optional, Sequence, Set, Tuple

import cv2
from maa.context import Context

from .battle_map import CELL_HEIGHT, CELL_WIDTH, COLS, ROWS, BattleGrid

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


def count_action_range(grid: BattleGrid) -> int:
    """统计战场快照中的可移动 / 可攻击格数量。"""
    return sum(
        1
        for cell in grid
        if cell.is_moveable or cell.is_attackable
    )


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
    """比较常驻回合数字区域的平均差异。

    OCR 读不到回合数字时，用它作为回合推进的兜底判据。
    """
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
