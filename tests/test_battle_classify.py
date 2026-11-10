"""识别层单测：状态条聚类后，格子身份与形态必须分开表达。

这些用例锁住三条规则：

1. 颜色身份来自节点（蓝=我方 / 红=敌方 / 绿=友军），不能被聚类形态覆盖；
2. 红色节点里的无生命数字碎片要剔除，实体长条（祭坛/石碑）要保留；
3. 同格多色时按身份优先级取值，我方 > 友军 > 敌方。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from action.fight.battle_classify import GridClassifier  # noqa: E402
from action.fight.battle_map import BattleGrid, CellKind  # noqa: E402


class _Context:
    """按节点名返回预设色块框的假上下文。"""

    def __init__(self, boxes_by_node):
        self.tasker = SimpleNamespace(stopping=False)
        self._boxes = boxes_by_node

    def run_recognition(self, node, image):
        boxes = self._boxes.get(node, [])
        results = [
            SimpleNamespace(box=tuple(box), score=1.0) for box in boxes
        ]
        return SimpleNamespace(hit=bool(results), filtered_results=results)


def _blue():
    return GridClassifier.UNIT_NODES[CellKind.SELF]


def _red():
    return GridClassifier.UNIT_NODES[CellKind.ENEMY]


def _green():
    return GridClassifier.UNIT_NODES[CellKind.FRIEND]


def _person_bar(x=100, y=300):
    """两段短条组成的普通人物状态条。"""
    return [(x, y, 20, 5), (x + 25, y, 15, 5)]


def _vitality_digit(bar_center_x, bar_center_y):
    """状态条左侧的生命数字笔画。"""
    return (
        bar_center_x - 25 - 5,
        bar_center_y - 6,
        10,
        12,
    )


def _scan(boxes_by_node):
    grid = BattleGrid()
    classifier = GridClassifier()
    classifier.scan_grid(
        grid, _Context(boxes_by_node), img=object()
    )
    return grid


class GridClassifierTest(unittest.TestCase):
    def test_blue_bar_is_self_unit(self):
        """蓝色状态条必须是我方单位，不能被当成敌人。"""
        grid = _scan({_blue(): _person_bar()})

        self.assertEqual(1, len(grid.self_units))
        self.assertEqual(0, len(grid.enemy_units))
        self.assertEqual(0, len(grid.friend_units))
        self.assertEqual(CellKind.SELF, grid.cells[2][1].kind)
        self.assertFalse(grid.cells[2][1].is_environment)

    def test_green_bar_is_friend_unit(self):
        """绿色状态条是友军单位。"""
        grid = _scan({_green(): _person_bar()})

        self.assertEqual(1, len(grid.friend_units))
        self.assertEqual(0, len(grid.enemy_units))
        self.assertEqual(0, len(grid.self_units))

    def test_red_bar_with_vitality_digit_is_enemy(self):
        """带生命数字的红条是敌人。"""
        bar = _person_bar()
        center_x = 120
        center_y = 302
        grid = _scan(
            {_red(): bar + [_vitality_digit(center_x, center_y)]}
        )

        self.assertEqual(1, len(grid.enemy_units))
        self.assertEqual(CellKind.ENEMY, grid.cells[2][1].kind)
        self.assertFalse(grid.cells[2][1].is_environment)

    def test_red_fragment_without_digit_is_rejected(self):
        """没有生命数字的红条碎片要剔除，不能变成假敌人。"""
        grid = _scan({_red(): _person_bar()})

        self.assertEqual(0, len(grid.enemy_units))
        self.assertEqual(0, len(grid.self_units))

    def test_solid_bar_is_environment_not_enemy(self):
        """实体长条是场地目标：不是敌人，但也不算我方。"""
        grid = _scan({_red(): [(300, 500, 60, 20)]})

        self.assertEqual(0, len(grid.enemy_units))
        self.assertEqual(1, len(grid.environment_units))
        self.assertTrue(grid.cells[3][2].is_environment)

    def test_solid_bar_kept_without_vitality_digit(self):
        """实体长条没有生命数字也必须保留。"""
        grid = _scan({_red(): [(300, 500, 60, 20)]})

        self.assertEqual(1, len(grid.environment_units))

    def test_same_cell_prefers_self_over_enemy(self):
        """同格同时出现我方与敌方状态条时，取身份更高的我方。"""
        grid = _scan(
            {
                _blue(): _person_bar(),
                _red(): _person_bar() + [_vitality_digit(120, 302)],
            }
        )

        cell = grid.cells[2][1]
        self.assertEqual(CellKind.SELF, cell.kind)
        self.assertEqual(1, len(grid.self_units))
        self.assertEqual(0, len(grid.enemy_units))

    def test_same_cell_prefers_friend_over_enemy(self):
        """同格同时出现友军与敌方状态条时，取友军。"""
        grid = _scan(
            {
                _green(): _person_bar(),
                _red(): _person_bar() + [_vitality_digit(120, 302)],
            }
        )

        cell = grid.cells[2][1]
        self.assertEqual(CellKind.FRIEND, cell.kind)
        self.assertEqual(1, len(grid.friend_units))
        self.assertEqual(0, len(grid.enemy_units))

    def test_environment_does_not_hide_self_identity(self):
        """实体长条即使来自蓝色节点，也仍然带颜色身份。"""
        grid = _scan({_blue(): [(300, 500, 60, 20)]})

        cell = grid.cells[3][2]
        self.assertTrue(cell.is_environment)
        self.assertEqual(CellKind.SELF, cell.kind)
        self.assertEqual(1, len(grid.environment_units))
        self.assertEqual(1, len(grid.self_units))


if __name__ == "__main__":
    unittest.main()
