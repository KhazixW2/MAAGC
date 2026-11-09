"""战斗网格的几何计算。

只包含不依赖战斗实例状态的纯函数：格间方向、屏幕距离、朝向归一化
与落点是否真正接近敌人的判定。
"""

from typing import Optional, Sequence, Tuple

from .battle_grid import Cell


def grid_direction(first: Cell, second: Cell) -> Tuple[int, int]:
    """返回从 ``first`` 指向 ``second`` 的八方向单位向量 ``(col, row)``。"""
    delta_col = second.col - first.col
    delta_row = second.row - first.row
    return (
        1 if delta_col > 0 else -1 if delta_col < 0 else 0,
        1 if delta_row > 0 else -1 if delta_row < 0 else 0,
    )


def screen_distance(first: Cell, second: Cell, action: str) -> int:
    """两个格子在当前截图上的平方距离。"""
    first_x, first_y = first.action_click_point(action)
    second_x, second_y = second.safe_click_point()
    return (first_x - second_x) ** 2 + (first_y - second_y) ** 2


def is_adjacent_cell(first: Cell, second: Cell) -> bool:
    """八方向相邻，不把同格或远端世界地图记忆当作可攻击目标。"""
    row_distance = abs(first.row - second.row)
    col_distance = abs(first.col - second.col)
    return max(row_distance, col_distance) == 1


def move_reduces_enemy_distance(
    target: Cell, ally: Cell, enemy: Cell
) -> bool:
    """只接受真正接近敌人的落点，屏蔽垂直/水平噪声造成的两格往返。"""
    ally_x, ally_y = ally.safe_click_point()
    target_x, target_y = target.action_click_point("move")
    enemy_x, enemy_y = enemy.safe_click_point()
    before = (ally_x - enemy_x) ** 2 + (ally_y - enemy_y) ** 2
    after = (target_x - enemy_x) ** 2 + (target_y - enemy_y) ** 2
    return after < before


def sign_direction(
    delta_x: float, delta_y: float, dead_zone: float = 0.0
) -> Tuple[int, int]:
    """把像素/格子差值归一化成 ``(x, y)`` 八方向。

    ``dead_zone`` 以内的差值视为 0，用于忽略噪声方向的抖动。
    """
    direction_x = (
        0
        if delta_x == 0 or abs(delta_x) < dead_zone
        else (1 if delta_x > 0 else -1)
    )
    direction_y = (
        0
        if delta_y == 0 or abs(delta_y) < dead_zone
        else (1 if delta_y > 0 else -1)
    )
    return direction_x, direction_y


def world_direction(
    origin: Tuple[int, int], target: Tuple[int, int]
) -> Tuple[int, int]:
    """世界坐标下的 ``(row, col)`` 八方向单位向量。"""
    row_direction = (
        0
        if target[0] == origin[0]
        else (1 if target[0] > origin[0] else -1)
    )
    col_direction = (
        0
        if target[1] == origin[1]
        else (1 if target[1] > origin[1] else -1)
    )
    return row_direction, col_direction


def ally_centroid(allies: Sequence[Cell]) -> Tuple[int, int]:
    """我方单位的屏幕坐标中心点。"""
    ally_x = sum(cell.safe_click_point()[0] for cell in allies) // len(allies)
    ally_y = sum(cell.safe_click_point()[1] for cell in allies) // len(allies)
    return ally_x, ally_y


def nearest_cell(
    cells: Sequence[Cell], row: int, col: int
) -> Optional[Cell]:
    """曼哈顿距离最近的格子。"""
    if not cells:
        return None
    return min(
        cells,
        key=lambda cell: abs(cell.row - row) + abs(cell.col - col),
    )
