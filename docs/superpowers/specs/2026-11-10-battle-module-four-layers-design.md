# 战斗模块四层重构设计

- 日期：2026-11-10
- 范围：`agent/action/fight/` 战斗引擎（8 个模块，7200 行）
- 不在范围：`fight_utils.py`、`fight_processor.py`（年度/月度流程）、`pirate_raid_processor.py`

## 1. 目标

把当前按「能力 Mixin」切分的战斗引擎，重构成四个职责单一、依赖单向的层：

| 层 | 职责 | 一句话 |
|---|---|---|
| `classify` | 识别 | 一张截图里，格子中是什么类型的角色 / 场地 |
| `map` | 地图 | 局部视野网格与全局累积地图，合并成一个统一模型 |
| `strategy` | 策略 | 什么时候该攻击、该移动、该探索、该结束回合 |
| `action` | 动作 | 所有游戏操作：点击、移动、攻击、撤离、结束回合、开关威胁层、平移镜头 |

## 2. 依赖方向

四层是**职责**划分，不是文件依赖的线性排列。实际落地时每一层拆成
「纯模型」与「过程代码」两类文件，依赖图如下（箭头表示「依赖」）：

```
   battle_map.py        纯模型：CellKind / Cell / BattleGrid / BattleMap /
                        BattleSessionState / BattleRuntime …（无内部依赖）
        ▲   ▲   ▲
        │   │   │
        │   │   └── battle_geometry.py（纯几何函数）
        │   └────── battle_vision.py（纯像素函数）
        └────────── battle_classify.py（截图 → 格子分类）
                         ▲
                         │
        battle_action.py ┘（所有游戏操作 + 结果校验）
                         ▲
                         │
        battle_strategy.py（纯决策 → ActionPlan）
                         ▲
                         │
        battle_mapping.py（建图流程）
                         ▲
                         │
        auto_fight_processor.py（编排）
```

单向无环。两条关键约束：

- **地图层拆成两个文件**：`battle_map.py` 只放模型，**不 import 任何战斗模块**；
  建图流程（顺时针探索、定位我方、边缘重定位、威胁层合并）放在
  `battle_mapping.py`。否则 `map → action → map` 的模块级循环导入无法避免。
- **跨层共用的纯函数放 `battle_geometry.py`**：例如 `coarse_threat_direction`
  同时被建图层与策略层需要，放进任一层都会造成反向依赖；
  `ally_is_actionable` 同理。

层间对象通过**构造注入**连接（`BattleMapBuilder(session, classifier,
threat_classifier, actions, runtime)`），没有全局单例，也没有模块级互相 import。

## 3. 模块映射

| 新文件 | 内容 | 来源 |
|---|---|---|
| `battle_map.py` | `CellKind` / `Cell` / `BattleGrid` / `UnitObservation` / `BattleMap` / `ClockwiseExplorer` / `BattleRuntime` / `BattleSessionState` / `BattleSessionRegistry` / `session_key` | `battle_grid.py` + `battle_world_map.py` |
| `battle_mapping.py` | `BattleMapBuilder`（建图流程） | `battle_planner._merge_world_view` / `_scan_world_view` + `battle_camera` 建图部分 + `battle_threat` |
| `battle_classify.py` | `GridClassifier`（原 `GridScanner`）+ `ThreatClassifier` | `battle_grid.GridScanner` + `battle_vision` 的 threat 函数 + `battle_planner._promote_occluded_adjacent_targets` |
| `battle_action.py` | `BattleActions` / `ActionKind` | `battle_action.py` + `battle_decision` 执行段 + `battle_threat._set_threat_overlay` + `battle_camera._pan_camera` |
| `battle_strategy.py` | `BattleStrategy` / `ActionPlan` / `astar_path` / `choose_move_candidate` | `battle_decision` 决策段 + `battle_planner` 规划段 + `battle_world_map` 寻路段 |
| `battle_geometry.py` | 新增 `ACTION_SAFE_X/Y`、`THREAT_DIRECTION_DEAD_ZONE`、`ally_is_actionable`、`coarse_threat_direction` | — |
| `battle_vision.py` | 保留为低层像素库，移除已上移的 threat 函数 | — |

删除：`battle_grid.py`、`battle_world_map.py`、`battle_planner.py`、`battle_decision.py`、
`battle_camera.py`、`battle_threat.py`、`battle_support.py`。

`install-cli/agent/` 是 gitignore 的构建产物（由 `tools/ci/install_cli.py` 重新生成），
**不参与本次改动**。

## 4. 三个关键模型变化

### 4.1 统一 `BattleMap`（局部 ⊕ 全局）

```python
class BattleMap:
    grid: BattleGrid          # 当前 6x10 局部视野
    observed / units          # 累积到全局坐标的观测

    def local_to_world(row, col) -> GridPoint
    def world_to_local(point) -> Optional[Tuple[int, int]]
    def unit_points(kind, current_round, max_age) -> List[GridPoint]
    def fresh_targets(current_round) -> Tuple[List[GridPoint], List[GridPoint]]
    def merge_view(grid, round_seen, replace_visible) -> None
```

对外查询一律返回全局坐标，消灭当前「局部 row/col 与全局 point 两套语言并存」的问题
（`battle_world_map` 用字符串 `SELF/ENEMY/FRIEND/ENVIRONMENT`，
`battle_grid` 用 4 值 `CellType` 且没有 environment 成员）。

`BattleSessionState.world` 改为 `BattleSessionState.map`（持有 `BattleMap`）。

目标有效期策略收进地图层，因为建图层与策略层都要用：
`ENEMY_OBSERVATION_MAX_AGE = 0`（敌人每回合都可能移动，旧坐标只能当方向提示）、
`ENVIRONMENT_OBSERVATION_MAX_AGE = 2`（环境目标是静态的，允许短暂遮挡）。

### 4.2 `CellKind` 枚举取代 `CellType` + `is_environment_object`

```python
class CellKind(Enum):
    EMPTY = "empty"
    SELF = "self"
    ENEMY = "enemy"
    FRIEND = "friend"
    ENVIRONMENT = "environment"   # 场地：祭坛 / 石碑
```

> **实施更正**：单值枚举表达不了两个正交维度。
> `CellType` 表达「谁在这个格子里」（蓝=我方 / 红=敌方 / 绿=友军），
> `is_environment_object` 表达「它长什么样」（祭坛、石碑的实体长条）。
> 合并成一个枚举后，识别层只能二选一，于是把**颜色身份整个丢掉**，
> 我方与友军单位全部被判成敌人（见 §8.5）。
> 最终形态：局部网格仍是两个维度——`Cell.kind` 管身份、
> `Cell.is_environment` 管形态；`CellKind.ENVIRONMENT` 只留给
> **全局地图**做环境目标分桶。

### 4.3 `ActionKind` 枚举取代中文字符串嗅探

当前 `_ActionPlan.label` 取值 `"移动"` / `"攻击"` / `"相邻威胁中心攻击"` / `"威胁中心攻击"`，
靠 `BattleSupport._action_kind` 里的 `if "攻击" in label` 判定语义——字符串嗅探且
点击方式与结果校验两处各写一套。改为：

```python
class ActionKind(Enum):
    MOVE = "move"
    ATTACK = "attack"
```

`label` 仅保留作日志文案。

## 5. 职责归属判定规则

判定一条代码属于哪一层，用这三个问题：

1. **它读画面并输出结论吗？** → `classify`
2. **它改变游戏状态（点击/平移/开关）吗？** → `action`
3. **它既不改画面也不读像素，只是在已有状态上做取舍吗？** → `strategy`
4. **它维护"世界是什么样"的状态（网格、坐标、累积观测）吗？** → `map`

边界情形：

- `_promote_occluded_adjacent_targets`（遮挡的黄色框恢复）→ `classify`：
  它回答「这个格子里是不是可攻击的敌人」。
- `_merge_open_threat_overlay` → 拆三份：
  锯齿中心识别 → `classify`（`ThreatClassifier`）；
  方向推断 → `battle_geometry.coarse_threat_direction`（纯函数，两层共用）；
  写入世界地图 → `map`。
- `_search_for_enemies`（181 行，A/B/C 三条兜底路径）→ 建图层
  （`BattleMapBuilder.search_for_enemies`）：
  它决定「往哪个方向找、找不到怎么办」，属于地图流程的一部分；
  具体的平移与扫描动作 → `action` / `classify`。
- `_record_move` → `map`：只更新状态，不碰画面。
- `_world_pursuit` → `strategy`：它输出的是「往哪个方向追」的决策。

## 6. 迁移阶段

每阶段独立可验证，全程保持 `python -m py_compile` 与单测绿。

| 阶段 | 内容 | 验证 |
|---|---|---|
| 0 | 建新模块骨架，旧模块暂留 | 编译通过 |
| 1 | `battle_map.py` 统一模型 | 世界地图用例迁移后绿 |
| 2 | `battle_classify.py` | 识别类用例绿 |
| 3 | `battle_action.py` 收拢所有游戏操作 | 动作语义用例绿 |
| 4 | `battle_strategy.py` 纯决策 | 决策用例绿 |
| 5 | `battle_mapping.py` 建图流程 | 建图用例绿 |
| 6 | 重写 `auto_fight_processor.py` 用组合 | 全量单测绿 |
| 7 | 删旧模块 + 更新测试 | 编译 + 全量单测 |
| 8 | 实机验证 | ADB 跑一场真实战斗 ✓（MuMu，见 §8.4） |

## 7. 硬约束

- **行为保持**：所有阈值常量、判定顺序、日志文案原样搬迁，不趁机改逻辑。
- **`AutoFightProcessor` 类名与模块路径不变**
  （`assets/resource/base/pipeline/fight_utils.json` 通过 `"custom_action"` 引用）。
- **基线测试数 71**（`python -m unittest discover -s tests -t tests`），
  重构后不得下降——拆分后 import 失败会被 unittest 静默跳过，
  只看「OK」会被骗，必须先对总数。
  （真实基线是 71，用 `git worktree` 拉 `745bdb6` 实跑核对过；
  重构后为 79：新增 1 条 `fresh_targets` 有效期用例。）
- 每阶段结束跑一次 `python -m py_compile` 全量 + 单测。

## 8. 验证方式

1. **静态**：`python -m py_compile` 覆盖全部改动文件。
2. **单元**：`python -m unittest discover -s tests -t tests -p "test_*.py"`，
   断言总数 ≥ 78 且 OK。
3. **资源**：`python check_resource.py`。
4. **实机**：MaaMCP 连 MuMu 模拟器，
   `MAAGC_TEST_ACTIVE_FROM_ROUND=1` 提前进入主动阶段跑一场真实战斗，
   看日志确认建图 / 决策 / 动作三层都走到。

### 8.4 实机结果（2026-11-10）

MuMu 模拟器（ADB `127.0.0.1:16448`），MaaMCP 的 ADB controller +
agent supervisor 拉起本仓库 agent，`MAAGC_TEST_ACTIVE_FROM_ROUND=1`：

- 业务流程：找城（近邻探测命中「雄月城」）→ 接任务 → 进入战斗 ✓
- classify：`Battle_UnitScan_Blue/Red/Green`、`确认锯齿空洞敌人格` ✓
- map：`origin` / `explored` 持续累积 ✓
- action：`危险覆盖层切换为 开启/关闭 (n/2): click=True` ✓
- strategy：`切换为主动搜索与追击模式` ✓
- 回合数字 OCR：`FightRoundNumber` 读到「回合：3」（score 0.989）✓

### 8.5 审查回合：分类层回归（2026-11-11）

对第一版实机结果下过的结论——「全局建图 `allies=0` 是重构前就存在的
既有缺陷」——**是错的**，代码审查把它推翻并定位了根因：

`battle_classify.scan_grid` 从 `_find_status_bar_cells` 拿到的最后一个字段
是「聚类形态」（实体长条 / 普通血条），代码却把它当成了「这个单位是谁」。
颜色身份（蓝=我方 / 红=敌方 / 绿=友军）来自节点名，被整个丢弃，
于是**我方与友军单位全部被判成敌人**，`allies` 恒为 0。

旧代码用两个字段表达两件事：`cell_type`（身份）+ `is_environment_object`（形态）。
合并成单值枚举 `CellKind` 时，两者只能占一个位置，识别层选了形态、丢了身份。
这不是「枚举写得不够好」，是**模型少了一个维度**。

同时被查出并修掉的：

- 红色碎片过滤的极性写反（实体长条被丢弃、无生命数字的碎片被当成敌人）；
- 同格多色的优先级语义丢失（`_PRIORITY` 里 `FRIEND/SELF` 变死条目，
  变成后写入者获胜，而 `UNIT_NODES` 顺序是 SELF→ENEMY→FRIEND，
  敌方会盖掉我方）；
- 回合推进判定换成 OCR 后没有兜底：参考帧读不到数字就永久失效，
  两次结束回合后直接判负。现在 OCR 为主判据、像素差分为兜底；
- 选中态重置漏掉环境目标，残留上一帧的坐标；
- 识别层一条单测都没有——79 条用例里 `scan_grid` 被调用 0 次，
  这正是上面三个 CRITICAL 能全绿通过的原因。

修复方式：局部网格重新拆成两个正交维度（`Cell.kind` 管身份、
`Cell.is_environment` 管形态），`CellKind.ENVIRONMENT` 只留给全局地图分桶；
并新增 `tests/test_battle_classify.py` 覆盖颜色身份、碎片过滤、实体长条、
同格优先级。

**教训**：A/B 对照要对照**判别逻辑本身**，不是对照日志长相。
新旧代码在同一个坏场景下打出同一行日志（日志逐行一致），
只说明它们都卡住了，不说明卡住的原因相同。

### 8.6 修复后实机复验（2026-11-11）

同一台 MuMu、同一套入口重跑：

- `战场识别: 6x10, 我方=1, 人物敌方=7` —— 我方单位被正确识别（C1 修复生效）；
- 统一矩阵打印出我方 `S`/`A` 与敌方 `E` 分区，环境目标打成 `G`；
- 双击攻击 `(6,4) -> (482,779)` → 「攻击确认成功: 目标红色状态条像素 419 -> 0」；
- OCR 主判据与像素差分兜底都参与回合推进确认；
- **战斗胜利**，识别到胜利结算页，战后流程完成并返回大地图。

补充澄清：日志里的「边缘单位重定位: allies=0」统计的是
**处于边缘、需要重心化的我方单位数**，不是「识别到的我方单位数」，
为 0 属正常。第一版把这一行当成「定位不到我方」的症状，是误读。

