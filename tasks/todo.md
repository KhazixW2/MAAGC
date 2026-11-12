# 回退 PR #8 合并带来的战斗改动（保留市场采购功能）

> 计划：`C:\Users\29046\.claude\plans\clever-petting-kurzweil.md`
> 合并点：`5c5358a`（PR #8，squash，父提交 `e374746`）

## 执行清单

- [x] 1. 完整回退 4 个战斗 Python 文件到 `e374746`
  - [x] `agent/action/fight/fight_utils.py`
  - [x] `agent/action/fight/pirate_raid_processor.py`
  - [x] `agent/action/zshg/auto_fight_processor.py`
  - [x] `agent/action/fight/fight_processor.py`
- [x] 2. `fight_processor.py` 补回市场采购代码
  - [x] `import re`
  - [x] 260 行市场代码块（MARKET_* 常量 + `_parse_market_purchase_plan` + `_run_market_purchases`）
  - [x] 接线点 1：`market_plan = _parse_market_purchase_plan(context)`
  - [x] 接线点 2：`due_items` 过滤 + `_run_market_purchases(context, due_items)`
- [x] 3. `city.json` 定点回退 3 个节点（保留未提交的 `FightFail` OCR 改动）
  - [x] `FindCityTask_Refresh` → OCR「刷新」+ `next: FindCityTask_RefreshConfirm`
  - [x] `FindCityTask_RefreshConfirm` → OCR「确定」
  - [x] `ClickGoToContinent` → OCR「前往大陆」
- [x] 4. 回退 `event_utils.json`、`main_ui.json`
- [x] 5. 删除 `assets/resource/base/pipeline/skip_month.json`
- [x] 6. 回退 `tests/test_task_board_recovery.py`
- [x] 7. 确认保持不变：`market_buy.json` / `CastleTab.png` / `auto_task.json:MarketPurchasePlan` / `assets/interface.json`

## 验证清单

- [x] V1. `python -m unittest discover -s tests -v` → **39 tests OK**
      （39 而非 40：合并版测试文件多 1 个用例，上游 `e374746` 本来就是 39）
- [x] V2. `python -m py_compile` 4 个战斗文件 → COMPILE OK
- [x] V3. 节点引用审计（pipeline 节点集 334 个 vs 代码引用）→ **缺失 0**
- [x] V4. 残留检查（SkipMonth / `_rest_to_skip_month` / `MAX_CONFIRMED_ROUNDS` /
      `_retreat_stuck_battle` / `AnnualSettlement` / `MallPageHeader`）→ **0 命中**
- [x] V5. `python check_resource.py assets/resource/base` → All directories checked.
      （`assets/resource` 不是合法 bundle 路径，失败与本次改动无关）
- [x] V6. `git diff HEAD` 复核：
      - `FightFail` 的 OCR 改动仍在（city.json 相对上游只剩这一处差异）
      - `assets/interface.json` 未被触碰（`v1.6.1` + `./python/python.exe` + 市场选项）
      - 市场代码块与合并版**逐字节一致**（260 行，两处接线各 1 次）

## 恢复回来的上游逻辑（已确认在位）

| 逻辑 | 位置 |
| --- | --- |
| 水晶刷新 `FindCityTask_Refresh` | `fight_utils.py:448` |
| `SwitchInnerCity` | `fight_utils.py`（2 处） |
| 左右近邻探测 | `fight_processor.py:230-234` |
| `PASSIVE_ROUNDS` / `ACTIVE_FROM_ROUND` / `MAX_NO_PROGRESS_CYCLES` | `auto_fight_processor.py:46-49` |
| `ARCHIPELAGO_READY_NODE` 单节点常量 | `pirate_raid_processor.py` |

## 结果回顾

**改动规模**：`git diff HEAD --stat` = 9 个文件，+128 / -953。

- 4 个战斗 Python 文件：`+58 / -528`
- pipeline：`city.json` / `event_utils.json` / `main_ui.json` / 删除 `skip_month.json`
- `tests/test_task_board_recovery.py` 同步回退

**保留的市场采购功能**：`market_buy.json`（自包含，零外部节点引用）、`CastleTab.png`、
`auto_task.json:MarketPurchasePlan`、`interface.json`「市场定时采购」输入项，
以及 `fight_processor.py` 里 260 行市场代码 + 2 处接线。

**明确丢弃的合并内容**：`_retreat_stuck_battle` / `RETREAT_*` / `MAX_CONFIRMED_ROUNDS`、
`AnnualSettlement` 分支、`_at_bigmap` 多锚点判定、`_screen_fingerprint`、
`SCREENCAP_ATTEMPTS` 截图重试、`Event_ChildGrowUp` / `Event_GrainConvert` 节点、
`MallPageHeader`、招募/退休弹窗 roi 扩大与「招莫」、休息跳月流程（`skip_month.json` 全部）。

**未提交**：按约定改动保留在工作区，等确认后再 commit。

**经验教训**：见 `tasks/lessons.md`。

---

## 代码评审结果（code-reviewer agent）

### 机制层面：全部通过 ✓

| 检查项 | 结果 |
| --- | --- |
| 6 个文件与上游 `e374746` 逐字节一致 | ✓ `cmp` 验证 |
| `fight_processor.py` 相对上游 | ✓ 纯增量（4 个 hunk：import re + 市场块 + 2 处接线） |
| `city.json` | ✓ 55 节点，除保留的 `FightFail` 外与上游一致，JSON 合法 |
| **pipeline JSON → 节点引用**（`next`/`timeout`/`on_error` 全量，含 `[JumpBack]` 前缀剥离） | ✓ **缺失 0**（334 节点） |
| Python → 节点引用 | ✓ 缺失 0 |
| 重复节点名 | ✓ 0 |
| `tools/validate_schema.py`（15 个 pipeline + interface.json） | ✓ 全部通过 |
| 市场功能完整性 | ✓ 引用的节点全部存在于 `market_buy.json` / `auto_task.json` |

### 行为层面：回退重新引入了上游的老问题（属预期，非回退缺陷）

| 级别 | 问题 | 位置 |
| --- | --- | --- |
| HIGH-1 | 年终结算弹窗在 12 个月边界终止整轮年度任务（PR #8 用 `AnnualSettlement` + `year_end_festival` 分支修过，实测 2026-10-02 第 36/60 个月） | `event_utils.json` / `fight_processor.py:356` |
| HIGH-2 | 切回大陆依赖 OCR「前往大陆」，PR #8 作者实测**该节点永不命中**（底栏只显示区域名） | `city.json` → `ClickGoToContinent` |
| HIGH-3 | `battle_fail` 恢复为 `return False`；**叠加你未提交的 `FightFail` OCR 改动**（roi 覆盖屏幕 60%×94%），屏幕任意位置出现「失败」即终止整轮年度任务 | `fight_processor.py:114` + `city.json:455` |
| MEDIUM-1 | 恢复水晶刷新：任务池无候选时每月消耗 10 水晶，违反年度流程自身「禁止水晶消耗」的规则 | `fight_utils.py:447` |
| MEDIUM-2 | 「招募」OCR 少了「招莫」别名，且 `interface.json` 仍覆盖 `next=[Confirm,Cancel]` → 误识别会**拒绝**佣兵 | `event_utils.json:32` |
| MEDIUM-3 | `Event_GrainConvert` / `Event_ChildGrowUp` 节点删除 + `Event_MercenaryJoin` roi 收窄 → 两种弹窗不再识别 | `event_utils.json:8` |
| MEDIUM-4 | 大地图判定退回单锚点 `UI_MainWindows`（模板分数实测 0.399 < 阈值 0.700 时会误判 unknown） | `fight_utils.py:68` |
| MEDIUM-5 | 城市菜单滚动搜索移除（只剩 `SwitchInnerCity` 分支） | `fight_utils.py:215` |
| LOW-1 | `PopUpWindowsTipConfirm` roi 收窄 | `main_ui.json:551` |
| LOW-3 | 市场块内既有死代码（PR #8 自带，非本次引入）：`if False and qty <= MARKET_QTY_MAX` 使固定数量路径不可达，`牛:1:10` 实际仍是「买光」 | `fight_processor.py:610,760` |
| LOW-5 | `Optional` 未使用（上游既有） | `pirate_raid_processor.py` |

### 立即可操作的两条（非战斗代码，但影响本次改动生效）

1. **`install-cli/agent/` 是陈旧构建产物**（gitignored）：里面还是 `e374746` 时代的代码，
   `grep -c "MARKET_\|_run_market_purchases"` = **0**。
   从该目录启动游戏时**市场采购会静默不生效**，需重跑 `tools/ci/install_cli.py`。
2. **`interface.json` 的 `child_exec: "./python/python.exe"` 无法从源码运行**：
   仓库根既没有 `python/` 也没有 `python.exe`（已确认）。`tools/ci/install_cli.py:126-132`
   会为打包目录重写这两个字段，所以**发布版没问题**，但用 MaaPi/MFAU 直接指向仓库源码会拉不起 agent。
   上游的 `python.exe` + `../agent/main.py` 才是源码运行的安全值。

### 评审结论

回退**机械完整、内部自洽**，无悬空引用、无死导入、无会崩的异常；
所有 HIGH/MEDIUM 都是"回退 = 恢复上游老 bug"的必然结果 —— 这正是本次回退的语义，
已在动手前向你确认过取舍。**没有发现需要立刻修复的回退缺陷。**

---

## 代码精简结果（simplify：复用 / 精简 / 效率 / 抽象层次）

目标：在**不改变行为**的前提下合并重复代码、消除魔法值、把同一套判据收敛到一处。
范围：本次改动的 4 个战斗 Python 文件 + 4 个 zshg 模块 + 1 个 pipeline 文件。

### 已应用

| # | 改动 | 文件 | 收益 |
| --- | --- | --- | --- |
| S1 | 新增 `agent/utils/screen.py::screencap()`，取代 3 份逐字相同的 `_screencap` + 24 处裸 `post_screencap().wait().get()` | 新文件 + `fight_utils` / `fight_processor` / `pirate_raid_processor` / `auto_fight_processor` / `battle_grid` / `child` / `marry` / `role_utils` | 截图语义（失败返回 None、不抛异常）只剩**一份实现**，27 处调用点不再各自复制 |
| S2 | 地图分层节点名收敛为常量 `ARCHIPELAGO_READY_NODE` / `CONTINENT_READY_NODES` | `fight_utils`（唯一定义处）→ `fight_processor` / `pirate_raid_processor` | 节点名不再在 3 个文件里各写一遍字符串 |
| S3 | 分层就绪判定收敛为 `fight_utils.find_ready_node()` | `fight_utils` → `pirate_raid_processor`（删掉本地 `_any_ready`） | 「任一就绪信号命中」只有一份实现 |
| S4 | 大地图判据收敛为 `fight_utils._at_bigmap()` | `fight_utils` → `fight_processor`（3 处） | 判据锚点只写一次 |
| S5 | 单步回退收敛为 `fight_utils._back_or_click_center()` | `fight_utils`（2 处调用） | 「有返回点返回、否则点中心」不再复制两遍 |
| S6 | `_wait_for_recognition()` 支持「单个节点 or 一组节点」 | `fight_utils` | `fight_processor` 里 5 秒手写轮询循环被删掉，改调共享实现 |
| S7 | 3 处内联的 `getattr(detail,"status",None) + getattr(status,"succeeded",False)` 改调 `fight_utils._task_succeeded()` | `fight_processor`（市场采购块） | 任务成功判定与仓库其余部分统一 |
| S8 | 删除 `_preprocess_accept_task()` 里注释掉的死代码块 | `fight_utils` | — |
| S9 | 清理批量替换残留的多余括号包裹 | `fight_processor` / `fight_utils` / `pirate_raid_processor` | — |
| S10 | `FindCityTask_RefreshConfirm` 补显式 `"next": []` | `city.json` | 符合 `docs/zh_cn/节点编写指南.md` §9.5「终止节点统一显式写 next: []」的项目惯例（协议上等价，纯可读性） |
| S11 | `pirate_raid_processor` 删掉未使用的 `Optional` 导入 | `pirate_raid_processor` | 对应上游评审的 LOW-5 |
| S12 | 测试替身补 `Job.succeeded`；`test_task_board_recovery` 抽出 `_run_accept_new_task()` 测试夹具 | `tests/` | 假对象与真实 `JobWithResult` API 对齐 |

### 明确跳过（附理由）

| 项 | 理由 |
| --- | --- |
| 「恢复 PR #8 的战斗鲁棒性」（多锚点大地图判定 / 截图重试 `SCREENCAP_ATTEMPTS` / 撤退兜底 / 遮挡事件处理 / 休息跳月 / 城市菜单滚动 / 年终结算分支） | 本次工作区的语义就是**回退**这些内容（见上文「明确丢弃的合并内容」）。恢复它们等于撤销回退，超出本次「精简/复用/调整结构」的范围。 |
| `if False and qty <= MARKET_QTY_MAX` 死分支（市场块内，上游自带） | 是上游作者**有意保留**给后续「数量控制」迭代的规划代码（注释里写明），删掉会丢掉已完成的阶梯长按标定工作。仅记录，不动。 |
| `FightFail` 的 OCR 改动 | 是你未提交的自有改动，不在精简范围内，保持原样。 |
| 16 处「父节点 next 含唯一子节点」的链 | **复核后判定为误报**：`docs/zh_cn/节点编写指南.md` §9.5 明确「省略 next 与 next: [] 在协议里等价」，协议默认值即空列表，不存在自环。 |
| `install-cli/agent/` 陈旧构建产物 | 是 gitignore 的构建产物，应由 `tools/ci/install_cli.py` 重新生成，不是源码改动。 |

### 验证

| 项 | 结果 |
| --- | --- |
| `python -m py_compile`（全部 11 个改动 .py + 新增 `agent/utils/screen.py`） | ✓ COMPILE_OK |
| `python -m unittest discover -s tests` | ✓ **39 tests, OK** |
| `python check_resource.py assets/resource/base` | ✓ All directories checked. |
| pipeline 节点引用审计（Python → 节点） | ✓ 缺失 0 |
| 88 列行宽（改动文件） | ✓ 无新增超长行（余下 8 处均为改动前既有） |
| `screencap` 语义等价性 | ✓ 三份原始 `_screencap` 逐字相同，均为「失败返回 None」；新实现完全一致 |

---

## 战斗日志优化：进度单行刷新 + 结算日志去重

> 反馈原文：「这些战斗信息进行优化，然后进度的 可以设计为单行刷新的。很多连续进度刷新的可以这样试试」

### 问题

原始日志（同一场战斗）：

```text
info:整场战斗回合进度 +1: 1 (source=被动阶段结束回合)
info:整场战斗回合进度 +1: 2 (source=被动阶段结束回合)
info:整场战斗回合进度 +1: 3 (source=被动阶段结束回合)
info:整场战斗回合进度 +1: 4 (source=被动阶段结束回合)
warn:被动阶段第 1 次点击后回合未变化
info:整场战斗回合进度 +1: 5 (source=被动阶段结束回合)
info:识别到战斗胜利结算页，战斗正常结束
info:识别到战斗胜利结算页，战斗正常结束      <-- 重复
```

两个问题：**①** 连续刷新的进度信息一行一条，终端被刷屏；
**②** 结算提示重复打印（`_detect_battle_result` 被 7 个调用点反复调用，每次命中都打一条）。

### 已应用的改动

| # | 改动 | 文件 | 说明 |
| --- | --- | --- | --- |
| L1 | 新增 `ProgressPrinter`：`\r` 回到行首重写同一行，并按历史最大宽度补空格（内容变短不留残影） | `agent/utils/logger.py` | 终端原地刷新 |
| L2 | 进度消息带 `_PROGRESS_FLAG` 标记写入 loguru，**控制台 sink 用 filter 丢弃**（文件 sink 保留每一条） | `agent/utils/logger.py` | 终端不重复、文件不丢信息 |
| L3 | 控制台 `format` 改为可调用：打印常规日志前先 `progress.commit()` 收尾进度行 | `agent/utils/logger.py` | 两条日志不会粘在同一行 |
| L4 | 进度文件记录用 `opt(depth=2)`，日志里的调用位置指向业务代码而不是 logger 内部 | `agent/utils/logger.py` | 日志可追溯 |
| L5 | 战斗回合进度 → 进度行：`整场战斗 回合 N · <最近动作>` | `auto_fight_processor.py` | 回合进度不再逐条刷屏 |
| L6 | 「攻击目标格子 / 移动目标格子」→ 进度行 | `auto_fight_processor.py` | 每回合都在变的高频信息 |
| L7 | `_detect_battle_result` 改为实例方法，胜负日志只在 **session 内首次命中**时打印 | `auto_fight_processor.py` + `battle_world_map.py`（新增 `reported_result` 字段） | 消除重复结算提示；新一场战斗新建 session，自然重新允许打印 |
| L8 | 「HUD 未识别到有效任务，正在滑动刷新 (n/N)」→ 进度行 | `fight_utils.py` | 连续 5 次刷新不再刷屏 |
| L9 | 「滑动寻找目标城市 X (n/N)」→ 进度行 | `fight_processor.py` | 连续滑动不再刷屏 |
| L10 | `_process_fighting` 开头 `progress.clear()`，擦掉上一场战斗遗留的进度行 | `fight_utils.py` | 新战斗开始时终端干净 |

### 效果

同一场战斗现在只占 4 行：

```text
进度: 整场战斗 回合 6 · 被动阶段结束回合
warn:被动阶段第 1 次点击后回合未变化
进度: 整场战斗 回合 6 · 被动阶段结束回合
info:识别到战斗胜利结算页，战斗正常结束
```

日志文件里仍然逐条保留全部历史（回溯不受影响）：

```text
2026-11-09 20:46:15.093 | INFO     | __main__:<module>:12 | 识别到战斗胜利结算页，战斗正常结束
2026-11-09 20:46:15.095 | INFO     | __main__:<module>:16 | 进度: 整场战斗 回合 3 · 被动阶段结束回合
2026-11-09 20:46:15.095 | INFO     | __main__:<module>:17 | 第二条普通日志
```

### 验证结果

| 项 | 结果 |
| --- | --- |
| `python -m py_compile`（全部改动 .py + 新增测试） | ✓ COMPILE_OK |
| `python -m unittest discover -s tests` | ✓ **52 tests, OK**（39 → 52，新增 13 条） |
| `python check_resource.py assets/resource/base` | ✓ All directories checked. |
| 控制台字节流实测（重定向到文件后逐字节核对） | ✓ 进度行 `\r` 覆盖、无重复；常规日志前自动换行；Windows 下 `\n`→`\r\n` 换行正常 |
| 日志文件内容实测 | ✓ 每条进度逐条保留，且调用位置指向业务代码 |
| 88 列行宽 | ✓ 无新增超长行（余下 10 处均为改动前既有） |
| 死锁风险 | ✓ 进度记录被控制台 filter 丢弃 → 不会在持锁时回调 `commit()`；锁用 `RLock` 兜底 |

### 新增测试 `tests/test_progress_logging.py`（13 条）

- `ProgressPrinter`：原地覆盖 / 短内容补空格擦残影 / commit 换行 / 无进度时 commit 不写任何东西 / clear 擦除 / record 回调收到每条
- `_console_filter`：进度记录被控制台丢弃、普通记录放行并补简短等级名
- 战斗进度：回合推进刷新进度行、进度行带最近动作
- 结算去重：同一场战斗只打印一次失败/胜利、新一场战斗重新打印

---

## 年度任务控制台降噪（「还是很多啊」）

> 反馈原文：贴了一整段年度任务战斗日志 —— 「还是很多啊」。
> 上一轮只解决了「单场战斗内的连续刷屏」，但**每个月的固定开销**（几十行
> 阶段性诊断）还在，所以整体看起来还是很吵。

### 问题定位

按「每月」为单位盘了一遍控制台输出，噪音来自三层：

| 层 | 内容 | 原来每月的行数 |
| --- | --- | --- |
| 月度固定诊断 | 「当前游戏月份为 X 月」「主城任务板已就绪」「检测到已接取任务」「====== 战斗阶段 ======」等 | ~12 |
| 战斗内诊断 | 「移动确认成功」「攻击确认成功」「局部图没有直接缩短敌我距离的蓝格」等**每个动作一条** | ~10 |
| 城市/事件/节日诊断 | 「滑动后当前城市」「近邻探测找到目标城市」「事件处理完成」等 | ~5 |

合计 ~25 行/月，12 个月就是 300 行，真正的里程碑（月份完成、战斗胜负）被淹没。

### 本轮已应用的改动

| # | 改动 | 文件 | 说明 |
| --- | --- | --- | --- |
| Y1 | 新增 `YearlyProgress` 数据类 + 模块级单例，承载「第 N/M 月 · 城市 · 阶段」状态 | `fight_utils.py` | 月度各阶段共用**同一行**进度显示 |
| Y2 | 各阶段接入进度行：事件检测 / 寻找城市 / 节日处理 / 接取任务 / 战斗 / 战斗中 / 战后处理 | `fight_processor.py` + `fight_utils.py` | 阶段推进不再新增日志行 |
| Y3 | 月度固定诊断 **~30 条** `logger.info` → `logger.debug` | `fight_utils.py` + `fight_processor.py` | 月份/任务板/采购规则等中间态只进日志文件 |
| Y4 | 战斗内诊断 **43 条** `logger.info` → `logger.debug`（保留：任务执行被停止、回合超限、战斗胜利结算页） | `auto_fight_processor.py` | 每个动作一条的诊断下沉到 DEBUG |
| Y5 | 启航节连续购买商品 → 进度行 | `fight_processor.py` | 同 L8 的处理方式 |
| Y6 | 市场逐件采购日志 → `logger.debug`（保留清单汇总与「已买下」） | `fight_processor.py` | 采购是少数月份才发生，但同样收敛 |
| Y7 | `ProgressPrinter.update()` 增加 `depth` 参数，日志里的调用位置指向**真正的业务代码**而不是中间封装 | `agent/utils/logger.py` | 经 `YearlyProgress` / `_refresh_progress` 两层封装后仍可追溯 |
| Y8 | 删除已无调用方的 `ProgressPrinter.clear()` | `agent/utils/logger.py` | 死代码 |

> **信息不丢**：日志文件 sink 的等级是 `DEBUG`，所有降级的诊断**逐条保留**在
> `debug/custom/<日期>.log` 里，回溯能力不受影响；只有终端变安静。

### 效果（模拟 3 个月年度任务，实测控制台可见行）

```text
info:开始年度任务处理
info:年度任务执行月份数: 3
info:团长大人, 您回来了！
进度: 整场战斗 回合 6 · 被动阶段结束回合
info:识别到战斗胜利结算页，战斗正常结束
进度: 第 1/3 月 · 战后处理
info:第 1/3 个月完成: 3 -> 4
进度: 整场战斗 回合 6 · 被动阶段结束回合
info:识别到战斗胜利结算页，战斗正常结束
进度: 第 2/3 月 · 战后处理
info:第 2/3 个月完成: 3 -> 4
...
info:年度任务处理完成
```

**每月 4 行**（原来 ~25 行），3 个月共 16 行可见 —— 12 个月约 52 行。

### 本轮验证结果

| 项 | 结果 |
| --- | --- |
| `python -m py_compile`（全部改动 .py） | ✓ COMPILE_OK |
| `python -m unittest discover -s tests` | ✓ **54 tests, OK**（52 → 54） |
| `python check_resource.py assets/resource/base` | ✓ All directories checked. |
| 模拟年度任务（3 个月）+ CR 感知终端渲染 | ✓ 可见行 16 行，约 4 行/月 |
| 日志文件归因 | ✓ 进度记录指向业务代码（`fight_processor:run:902`、`auto_fight_processor:_record_round_advance:2753`） |
| 88 列行宽 | ✓ 无新增超长行（唯一一处超长是上游市场代码 `months = int(round(value * 12))…`，逐字来自 `5c5358a`） |

### 新增测试（2 条）

- `YearlyProgress`：进度行包含「月份 · 城市 · 阶段」；年度任务开始前不显示月份
- `ProgressPrinter`：record 回调收到调用方指定的 `depth`

## 年度任务第 7 个月失败修复（「进入任务板状态连续无变化: open_city_list」）

### 现象

```text
info:====== 接取任务 ======
err:进入任务板状态连续无变化: open_city_list
warn:第 7/12 个月未确认进度 (2/2)
err:第 7/12 个月达到重试上限，停止年度任务
```

### 根因

大地图上残留着一条「获得 科内塔辉石 1」的奖励横幅。它是**屏幕上的模态
面板**，盖住了「雄月城」城市名；`EnterCity` 识别得到城市名，但点击落在
横幅上被吞掉 —— 场景不变 → 状态机连续三次看到同一个 `open_city_list`
→ 报错 → 该月重试耗尽 → 年度任务停摆。

在模拟器（emulator-5558）上复现并逐项验证了横幅的行为（2026-11-09）：

| 操作 | 结果 |
| --- | --- |
| 点横幅标题「获得」 | 无反应，横幅还在 |
| 点横幅里的物品图标 | 无反应，横幅还在 |
| 横幅**之外**点一下 | 横幅消失（这次点击被横幅吃掉，不会触发底下的城市/商城） |
| 横幅**之外**横滑一下 | 横幅消失 |
| 按返回键 | 无反应 |
| 等 45 秒 / ~15 分钟 | 横幅**不会**自动消失 |

所以「等它自己淡出」是行不通的，必须主动关掉。

### 本轮已应用的改动

| # | 改动 | 文件 | 说明 |
| --- | --- | --- | --- |
| Z1 | 新增 `BigMapDismissItemGainBanner` 节点（DirectHit + Swipe） | `main_ui.json` | 在横幅**之外**滑一下把它关掉；起点 (195,600) 控制器坐标，横向 60px |
| Z2 | `wait_item_gain_banner_gone()` 改为 `dismiss_item_gain_banner()`：有界滑动关闭并复核 | `fight_utils.py` | 旧实现是「等它淡出」，实测永远不会淡出 |
| Z3 | 关横幅的动作挪到点城市**之前** | `fight_utils.py` | `open_city_task_panel` 的 `EnterCity` 分支先关横幅再点城市 |
| Z4 | `_ensure_at_target_city` 同步改用新函数 | `fight_processor.py` | 城市定位阶段同样会被横幅吞掉点击 |

> **为什么用滑动而不是点击**：滑动不会被当成点击，不会误触城市名或商城
> 按钮；最坏情况只是让地图平移 60px，城市名仍在 `EnterCity` 的识别
> 范围内。

### 一个容易踩的坑

用户贴的日志里 `====== 接取任务 ======` 是 `info` 级别，说明**跑的是 8 月 3
日的旧构建**（`install-cli/`），不是工作区源码 —— 新代码里这条已经降级
成 `debug`。改完代码必须重新执行 `tools/ci/install_cli.py` 才能让改动
真正生效。

### 本轮验证结果

| 项 | 结果 |
| --- | --- |
| `python -m py_compile`（全部改动 .py） | ✓ COMPILE_OK |
| `python -m unittest discover -s tests` | ✓ **58 tests, OK** |
| `python check_resource.py assets/resource/base` | ✓ All directories checked. |
| `python check_resource.py install-cli/resource/base` | ✓ All directories checked. |
| 模拟器实测：横幅外滑动（节点同坐标） | ✓ 横幅消失，底下的界面未被误触 |
| 识别框 ROI `[200,250,320,140]`（控制器） | ✓ 有横幅时框住「获得」；无横幅的地图上无「获得」字样 |
| 重新构建 `install-cli` | ✓ 新节点与新函数均已进包 |

### 新增测试（4 条，改写 `ItemGainBannerTests`）

- 没有横幅时直接返回，不触发滑动
- 有横幅时滑动关闭（`BigMapDismissItemGainBanner`）
- 横幅一直不消失时按重试上限收手
- `open_city_task_panel` 点城市之前先关横幅

---

## 战斗处理器拆分（`auto_fight_processor.py` 3280 → 9 个模块）

> 需求：简化代码、抽象函数、拆分文件，单文件不超过 1000 行。
> 方案：Mixin 能力类（`AutoFightProcessor` 组合 5 个能力 Mixin）。

### 拆分结果

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `auto_fight_processor.py` | 786 | 主类 `AutoFightProcessor`：主循环、主动阶段建图、角色调度 |
| `battle_camera.py` | 768 | `BattleCameraMixin`：镜头平移、顺时针建图、边缘单位重定位 |
| `battle_decision.py` | 532 | `BattleDecisionMixin`：单角色的攻击 / 移动决策与落点规划 |
| `battle_action.py` | 523 | `BattleActionMixin`：选中态、点击、动作结果校验、回合推进 |
| `battle_vision.py` | 430 | 纯 cv2 函数：掩膜解析、威胁区域、菱形定位、镜头位移 |
| `battle_planner.py` | 346 | `BattlePlannerMixin`：世界地图合并与移动落点规划 |
| `battle_threat.py` | 204 | `BattleThreatMixin`：危险覆盖层开关与隐匿敌人方向推断 |
| `battle_geometry.py` | 101 | 纯几何函数：方向、距离、邻接、质心 |
| `battle_support.py` | 52 | `BattleSupport`：跨能力共享常量与状态声明 |

### 抽象与简化

- **14 个实例方法 → 模块级纯函数**（`battle_vision` / `battle_geometry`）：
  `_threat_enemy_cell_from_mask`、`_threat_origin_from_mask`、`_camera_motion`、
  `_camera_shift_is_real`、`_is_adjacent_cell`、`_sign_direction`、
  `_grid_direction`、`_screen_distance` 等。这些函数不依赖战斗状态，
  便于单独测试与复用。
- **魔法数字 → 具名常量**：`THREAT_HSV_LOW/HIGH`、`HEALTH_HSV_LOW/HIGH`、
  `DIAMOND_*`、`THREAT_CELL_*`、`BATTLE_AREA_HEIGHT/WIDTH` 等。
- **动作语义集中为 `_action_kind(label)`**（`battle_support.py`）：点击方式
  （`action_click_point("attack"/"move")`）与结果校验语义（血条变化 /
  菱形行动点）原本在 `battle_action.py` 两处各写一套字符串判断，现统一到
  一处；`_ActionPlan.is_attack` 随之删除，标签成为唯一事实来源。
- **`run()` 拆成 5 个可读步骤**：`_run_battle_round` → `_prepare_active_round`
  → `_act_with_allies` → `_run_active_round`；建图结论用
  `ActiveRoundStatus` + `_ActiveRoundResult` 显式表达（READY / WAIT / FAILED）。
- **`_decide_and_act()` 拆成 13 个聚焦方法**（`battle_decision.py`），
  攻击与移动分成两条独立执行分支。
- **删除 3 个死方法**：`_hidden_enemy_direction`、`_search_for_allies_and_stay`、
  `_ally_at_grid_edge`（全仓库 grep 无调用），以及两个只写不读的属性。

### 过程中发现并修复的两个真实回归

1. **丢掉 `CustomAction` 基类** —— 拆分后 `AutoFightProcessor` 只继承 Mixin，
   不再是 `CustomAction` 子类，`AgentServer.custom_action` 注册时直接抛
   `AttributeError: 'AutoFightProcessor' object has no attribute 'c_handle'`，
   **整个测试模块 import 失败**。已把 `CustomAction` 加回基类列表。
2. **敌人方向记忆的作用域被放大** —— 原实现是 `run()` 内的局部变量，
   每次调用（含任务恢复）都从零开始；改成实例属性后会跨 `run()` 调用残留。
   已在 `run()` 开头显式重置，恢复原有语义。

### 拆分后的验证结果

| 项 | 结果 |
| --- | --- |
| `python -m py_compile agent/action/zshg/*.py` | ✓ COMPILE OK |
| `python -m unittest discover -s tests` | ✓ **64 tests, OK**（HEAD 基线 61 条 + 新增 3 条） |
| `python check_resource.py assets/resource/base` | ✓ All directories checked. |
| 未使用导入扫描（AST） | ✓ 0 处 |
| `self.xxx` 未定义属性扫描（AST） | ✓ 0 处 |
| 方法迁移前后 AST 归一化比对 | ✓ 61 个方法：35 个完全一致，13 个为纯委派差异（已逐个核对），13 个为魔法数字改常量 |
| 单文件行数 | ✓ 全部 ≤ 1000（最大 788） |
| 独立复核（子代理对照 HEAD 逐条核对控制流） | ✓ 未发现行为变更；返修 2 处 LOW（动作标签约定分散两处、记忆刷新与判空的隐式耦合）+ 1 处过期文档引用 |
| `tools/ci/install_cli.py` 重新打包 | ✓ 打包版本可导入，`AutoFightProcessor` 注册成功 |

### 测试同步更新

`tests/test_auto_fight_world_scan.py` / `tests/test_progress_logging.py` 里
针对已迁走的实例方法做 monkeypatch 的地方，改为 patch 新的模块级函数
（`action.fight.battle_vision.*` / `action.fight.battle_geometry.*`）与
`action.fight.battle_camera.screencap`、`action.fight.battle_action.progress`。
断言的行为本身未变。

---

## 战斗模块迁到 `action/fight/`（`zshg` 只保留角色/联姻/日常）

战斗相关代码原先散在 `action/zshg/`，但它们服务的是 `fight_utils.json`
的 AutoFight 流程，与 `action/fight/` 的战斗调度器属于同一子系统。
统一到 `action/fight/` 后，目录职责按「子系统」而不是按「游戏区域」划分。

### 迁移清单（11 个文件）

`auto_fight_processor.py` 与 10 个 `battle_*.py`
（`battle_action` / `battle_camera` / `battle_decision` / `battle_geometry` /
`battle_grid` / `battle_planner` / `battle_support` / `battle_threat` /
`battle_vision` / `battle_world_map`）。

`action/zshg/` 保留 `child.py`、`daily_task.py`、`marry.py`、
`matchMarryProcessor.py`、`role_utils.py`、`task_hud_recognizer.py`。

### 同步修改

| 位置 | 改动 |
| --- | --- |
| `agent/action/fight/fight_utils.py` | `from action.zshg.battle_world_map import ...` → `from .battle_world_map import ...`（同包内相对导入） |
| `agent/agent_allfile.py` | `import action.zshg.auto_fight_processor` → `import action.fight.auto_fight_processor` |
| `tests/test_auto_fight_world_scan.py`、`tests/test_progress_logging.py`、`tests/test_battle_world_map.py` | 导入与 patch 路径 `action.zshg.*` → `action.fight.*` |
| `docs/zh_cn/developers/custom.md`、`docs/zh_cn/graph/battle_flow_analysis_2026-07-31.md`、`docs/zh_cn/自动推年与战斗系统分析.md` | 路径与行号引用改为新位置 |

### 依赖环检查

迁移前 `action/zshg/` **不** 引用 `action/fight/`，只有反向依赖
（`fight_utils.py` → `action.zshg.battle_world_map`）。把
`battle_world_map.py` 一并迁到 `fight/` 后改为同包相对导入，
`task_hud_recognizer.py` 仍留在 `zshg` 供 `fight_processor.py` / `fight_utils.py`
使用 —— 两个方向都不存在环。

### 迁移后的验证结果

| 项 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests` | ✓ 64 tests OK |
| `python -m py_compile` 全部 agent / tests 文件 | ✓ COMPILE OK |
| 全仓库残留 `zshg.*battle_*` / `zshg.auto_fight` 引用扫描 | ✓ 0 处 |
| `install-cli` 重新打包 + 自定义动作注册冒烟测试 | ✓ `AutoFightProcessor` 注册成功 |

## 修复：战后随机事件（表白失败）卡死年度任务

### 现象

第 3/12 月战斗胜利后卡住，控制台只剩：

```
进度: 第 3/12 月 · 雄月城 · 战后处理
warn:战后奖励流程结束后未直接识别到大地图，尝试处理战后中断事件
err:战后流程未能回到大地图
warn:第 3/12 个月未确认进度 (1/2)
err:恢复时识别到战斗失败结算页，不自动重开战斗
err:第 3/12 个月恢复失败，停止年度任务
```

屏幕停在「感a力b科虎佩公向肖恩表白失败！」的随机事件信纸弹窗上。

### 根因

| # | 问题 | 证据 | 修复 |
| --- | --- | --- | --- |
| 1 | `FightFail` 用**全屏 OCR 找裸的「失败」**，把「表白失败」横幅当成战斗失败结算页 | `maafw.log` 01:00:17.680：`FightFail` best_result = `感a力b科虎佩公向肖恩表白失败！`(0.983) → Succeeded | expected 改成完整短语 `战斗失败` |
| 2 | `Event_ConfessionFail` 的 ROI 截断横幅首字，OCR 整行读错 | 同一行文字在 `Event_ConfessionSuccess` 的 ROI 下读成 `感a力b科虎佩公向肖恩表白失败！`(0.979)，在 `Event_ConfessionFail` 的 ROI 下读成 `力b科度佩公向省恩表自失收！`(0.728) | ROI 改成 `[112,163,441,180]`（与 `Event_ConfessionSuccess` 一致） |
| 3 | 该事件节点从来没生效过 | 5 个日志文件里 `Event_ConfessionFail` 识别 174 次、命中 **0** 次（ROI 是 2026-03-15 `7a08bda` 加的） | 同上 |
| 4 | 「战后中断事件」分支点屏幕中心，对模态信纸无效 | `BackButton_500ms` 模板 0.288（阈值 0.7）不命中 → 退到点 (360,640) 信纸正文 → 8 轮空转 21 秒 | 删除该分支 |

### 改动

| 位置 | 改动 |
| --- | --- |
| `agent/action/fight/fight_utils.py` | 删除 `_recover_post_battle_to_bigmap`；`ensure_at_bigmap` 增加「关掉挡住大地图的随机事件」；`_process_post` 改用 `ensure_at_bigmap` 兜底 |
| `assets/resource/base/pipeline/city.json` | `FightFail` expected `失败` → `战斗失败` |
| `assets/resource/base/pipeline/event_utils.json` | `Event_ConfessionFail` ROI → `[112,163,441,180]` |
| `tests/test_bigmap_event_recovery.py` | 新增 7 个回归用例 |
| `docs/zh_cn/自动推年与战斗系统分析.md`、`docs/zh_cn/graph/battle_flow_analysis_2026-07-31.md` | 同步流程说明 |

事件识别仍然只有 `detect_and_manage_event` 一处：战前 `preprocess_events` 走它，
回大地图的 `ensure_at_bigmap` 也走它，不再有「战后中断事件」的第二条路径。
MaaFW 侧只在 `ensure_at_bigmap` 走到「不在大地图」分支时才做事件识别，
正常路径没有额外开销。

### 验证结果

| 项 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests` | ✓ 71 tests OK（新增 7 个） |
| 新增用例在 HEAD 上必须失败 | ✓ `FightFail` 会匹配表白横幅、`Event_ConfessionFail` ROI 包不住横幅，两条断言均失败 |
| `python check_resource.py assets/resource/base` | ✓ All directories checked |
| `install-cli` 重新打包 + 自定义动作注册冒烟测试 | ✓ `AutoFightProcessor` 注册成功 |

## 回合推进判定改用 Pipeline OCR 节点（2026-11-10）

> 需求：用 Pipeline 节点识别右下角「回合：N」，数字两次变化即确认回合推进；
> 顺手精简 `battle_action.py` 原先 50 行、3 个函数的像素差分实现。

### 执行清单

- [x] 1. 复用已有节点 `FightRoundNumber`（OCR），修正 ROI 与 expected
      - ROI `[570,1135,150,80]` → `[570,1160,150,55]`（旧 ROI 下数字框只有 2/10 帧能检出）
      - expected `回合\s*[:：]?\s*\d+` → `回合\s*[:：]?\s*\d*`
- [x] 2. `battle_action.py`：删除 `_track_round_change`（像素差分）与
      `battle_vision.round_marker_difference`
- [x] 3. `battle_action.py`：新增 `_read_round_number` / `_watch_round` /
      `_wait_for_round_change`，合计约 34 行（原来 3 个函数约 50 行 + 17 行视觉工具）
- [x] 4. 新增 `tests/test_battle_round_number.py`（7 个用例，含「连续两帧同值才确认」）

### 验证结果

| 项 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests` | ✓ 78 tests OK（新增 7 个） |
| `python check_resource.py assets/resource/base` | ✓ All directories checked |
| 实机 OCR：`FightRoundNumber` 节点 | ✓ 读出 `回合：` + `1`（score 0.997 / 0.994） |
| 实机回合推进（点一次「结束回合」） | ✓ 基线 3 → 结束回合 → 第 1 帧读到 4（未确认）→ 第 2 帧读到 4（确认推进） |

> 实机验证时点了 3 次「结束回合」把用户那场测试战斗推进到了第 4 回合。

## 战斗模块四层重构（2026-11-10）

> 需求：把 `agent/action/fight/` 的 8 个 Mixin 模块重构成 map / classify / action / strategy 四层；
> 局部建图与全局建图合并成一个统一地图模型。
> 设计文档：`docs/superpowers/specs/2026-11-10-battle-module-four-layers-design.md`

### 执行清单

- [x] 1. `battle_map.py`：统一地图模型（`CellKind` / `Cell` / `BattleGrid` /
      `BattleMap` / `BattleSessionState` / `BattleRuntime`），**不依赖任何战斗模块**
- [x] 2. `battle_classify.py`：`GridClassifier`（原 `GridScanner`）+ `ThreatClassifier`
- [x] 3. `battle_action.py`：`BattleActions` + `ActionKind` 收拢全部游戏操作
- [x] 4. `battle_strategy.py`：`BattleStrategy` / `ActionPlan` / `astar_path` / `choose_move_candidate`
- [x] 5. `battle_mapping.py`：`BattleMapBuilder` 建图流程（拆出以避免 map → action 循环导入）
- [x] 6. `battle_geometry.py`：新增 `ally_is_actionable` / `coarse_threat_direction`
      （建图层与策略层共用的纯函数）
- [x] 7. `auto_fight_processor.py`：改显式类组合，类名与模块路径不变
- [x] 8. 删除 `battle_grid.py` / `battle_world_map.py` / `battle_planner.py` /
      `battle_decision.py` / `battle_camera.py` / `battle_threat.py` / `battle_support.py`
- [x] 9. 测试迁移：`test_battle_world_map.py` → `test_battle_map.py`，
      `test_auto_fight_world_scan.py` / `test_battle_round_number.py` / `test_progress_logging.py`
      改为按层构造对象
- [x] 10. 同步设计文档与 `docs/zh_cn/developers/custom.md`

### 验证结果

| 项 | 结果 |
| --- | --- |
| `python -m py_compile agent/action/fight/*.py tests/*.py` | ✓ COMPILE_OK |
| `python -m unittest discover -s tests -t tests -p "test_*.py"` | ✓ **90 tests OK**（真实基线 71，新增 19 条） |
| `python check_resource.py assets/resource/base` | ✓ All directories checked |
| 依赖环检查 | ✓ `battle_map` 无内部 import；`map → vision/classify → action → strategy → mapping → processor` 单向无环 |

> **基线数字更正**：本节原写「基线 78」，是错的。用 `git worktree` 拉出
> `745bdb6` 实际跑出来的基线是 **71 tests**（逐文件加总核对过）。
> 之前写 78 说明数字核对环节从未真正执行过——重构后 79 条里也一直
> 混着「看起来变多了」的错觉。

### 实机验证（阶段 8）

环境：MuMu 模拟器（ADB `127.0.0.1:16448`），MaaMCP 的 ADB controller + agent supervisor
拉起本仓库 agent（`MAAGC_TEST_ACTIVE_FROM_ROUND=1` 提前进入主动阶段）。

| 项 | 结果 |
| --- | --- |
| 完整业务流程 | ✓ 找城（近邻探测命中「雄月城」）→ 接任务（选「世界第一好爸爸」）→ 进入战斗 |
| classify 层 | ✓ `Battle_UnitScan_Blue/Red/Green` 色块扫描 + 「确认锯齿空洞敌人格」持续输出 |
| map 层 | ✓ 顺时针全局建图推进 `origin` / `explored`（80→100→155→165） |
| action 层 | ✓ 「危险覆盖层切换为 开启/关闭 (n/2): click=True」等操作全部执行 |
| strategy 层 | ✓ 「战斗到达第 1 回合仍未结束，切换为主动搜索与追击模式」 |
| 回合数字读取 | ✓ `FightRoundNumber` OCR 读到「回合：3」（score 0.989，box `[585,1165,109,26]`） |

### 代码审查回合（阶段 9）

对 `a6972f5` 做了一次完整代码审查（对比 `745bdb6`），结论是 **BLOCK**，
查出 3 个 CRITICAL、2 个 HIGH。**上一轮写的「allies=0 是既有缺陷」结论是错的**，
下面逐条更正。

**C1（CRITICAL，已修）** `battle_classify.scan_grid` 把节点类型丢了：
`_find_status_bar_cells` 的返回值把「聚类形态」写成了 `CellKind`，
颜色身份（蓝=我方 / 红=敌方 / 绿=友军）被整个丢弃，于是
**我方和友军单位全部被判成敌人**。`allies=0` 就是这么来的，
不是既有缺陷，是本次重构引入的回归。

**C2（CRITICAL，已修）** 红色碎片的过滤极性被写反：
旧逻辑「实体长条无条件保留、普通红条必须带生命数字」，
新逻辑反了过来——祭坛被整块丢掉，尸体碎片变成假敌人。

**C3（CRITICAL，已修）** 同格多色的优先级语义丢失：
`_PRIORITY` 表里的 `FRIEND/SELF` 变成死条目（比较的 kind 恒为
`ENEMY/ENVIRONMENT`），同格时变成「后写入者获胜」，
`UNIT_NODES` 顺序是 SELF→ENEMY→FRIEND，于是敌方会盖掉我方。

**H1（HIGH，已修）** 回合推进判定从像素差分换成 OCR 后，
参考帧读不到数字就永久失效（`wait_for_round_change` 恒 False，
两次结束回合后直接判负）。现在 OCR 为主判据、像素差分为兜底，
两者任一确认即认为回合推进。

**H2（HIGH，已修）** `capture_selected_state` 的选中态重置漏掉环境目标，
残留上一帧的 `unit_center` / `target_center`。

**M1（MEDIUM，已修）** `_log_matrix` 把环境目标打成 `0`，
和空格无法区分，排查时直接掩盖了根因。现在环境目标打成 `G`。

**M2（MEDIUM，已修）** 识别层一条单测都没有：79 条用例里
`GridClassifier.scan_grid` 被调用 0 次，这正是 C1/C2/C3 全绿通过的原因。
新增 `tests/test_battle_classify.py` 9 条，覆盖颜色身份、碎片过滤、
实体长条、同格优先级。

**修复方式**：局部网格重新拆成两个正交维度——
`Cell.kind`（身份：谁）和 `Cell.is_environment`（形态：是不是实体长条），
`CellKind.ENVIRONMENT` 只保留给全局地图做环境目标分桶。
单值枚举表达不了两个维度，合并时丢了身份信息。

### 修复后的实机复验（2026-11-11）

修复 C1/C2/C3 后重跑同一场战斗（MuMu，ADB `127.0.0.1:16448`）：

| 项 | 结果 |
| --- | --- |
| 战斗识别 | ✓ `战场识别: 6x10, 我方=1, 人物敌方=7` —— C1 修复后我方单位被正确识别（此前恒为 0） |
| 统一矩阵 | ✓ 打印出 `S`/`A`（我方）与 `E`（敌人）分区，环境目标打成 `G`（M1 修复） |
| 行动 | ✓ 选我方角色 → 选敌 → 双击攻击 `(6,4) -> (482,779)` → 「攻击确认成功: 目标红色状态条像素 419 -> 0」 |
| 回合推进 | ✓ OCR 主判据与像素兜底都触发（「主动阶段已确认回合变化」「结束回合按钮连续命中」） |
| 战果 | ✓ **战斗胜利**，识别到胜利结算页，战后流程完成并返回大地图 |

> 注：日志里的「边缘单位重定位: allies=0」统计的是**处于边缘需要重心化的我方单位数**，
> 不是「识别到的我方单位数」，为 0 属正常，不是 C1 的症状。

### 遗留

- **文件行数超标**：`battle_strategy.py` 1002 行、`battle_mapping.py` 958 行，
  超过项目「单文件 800 行」上限。长度是从旧模块继承的，不是本次引入，
  但重构后仍需拆分。
- ~~**文档里的旧模块路径**~~：已更新为四层新模块名
  （`battle_map.py` / `battle_classify.py` / `battle_action.py` /
  `battle_strategy.py` / `battle_mapping.py`），Pipeline 文件
  `battle_grid.json` 名称不变、未动。

## 回合推进判据失效（2026-11-11，年度任务第 1/12 月中断）

> 现象：`warn:参考帧未读到回合数字，无法确认回合变化` ×2 →
> `err:被动阶段两次结束回合均未生效` → 年度任务在第 1 个月达到重试上限后中止。

### 根因（实测数据，非推测）

1. **基线取自过期帧**：`round_reference` 是回合开始时的快照，战斗开场
   动画期间 HUD「回合：N」还没画完，这一帧必然读不到数字，
   于是整轮「回合推进」判定失去基线。
2. **兜底的像素差分判据本身是无效的**（用同一场战斗的实拍帧实测）：

   | 帧对 | 回合号 | ROI(670,1175,20,25) 平均差分 |
   | --- | --- | --- |
   | 004636 vs 004804 | 11 → 11（同一回合） | **29.22** |
   | 004154 vs 004636 | 3 → 11（真正换回合） | **4.46** |

   HUD 数字只有 13×13 px，且画在半透明底栏上，场景动画的影响远大于
   数字本身的变化 —— 差分信号是**反的**：阈值 10.0 会漏报真实换回合、
   误报没有换回合。这条兜底路径不但没用，还会在「主动阶段自动换回合」
   判定里造成误判（误判成已换回合就会跳过结束回合，战斗卡住）。

### 修复

- [x] 基线改用**点击结束回合之前**的最新帧读取，连续取帧重试；
      整段读不到时退回会话内上次读到的数字（`BattleSessionState.last_round_number`）。
- [x] 移除无效的像素差分判据，换成**不依赖 OCR** 的证据：
      「结束回合」按钮只在我方回合可点，敌方回合会消失，
      按钮消失后重新出现 = 这一回合真的走完了。
- [x] 统一轮询（OCR 数字 + 按钮状态），按钮的「消失」即使发生在
      点击后的头几帧也不会被漏判；弹窗盖住按钮时不算敌方回合。
- [x] 结束回合前按钮不可见时，等它重新出现即确认回合已推进
      （上一轮点击其实生效了，只是没确认上），不再直接判负。
- [x] 「主动阶段自动换回合」改用按钮可见性判断，不再依赖回合数字。

### 验证

- [x] `python -m unittest discover -s tests -t tests -p "test_*.py"` → 全绿
- [x] `python check_resource.py assets/resource/base` → 通过
- [x] 实机复跑年度任务，确认被动阶段不再卡死（2026-11-12，见下节）

## 回合推进判据第二次失效（2026-11-12，年度任务第 3/4 月中断）

> 现象：`warn:被动阶段第 1 次点击后回合未变化` ×2 →
> `err:被动阶段两次结束回合均未生效` → `第 3/4 个月达到重试上限，停止年度任务`。
> 上一轮修的「收紧 ROI 单独认数字」并没有解决根因。

### 根因（maafw.log 逐帧证据）

`FightRoundNumber` 的 OCR **原始结果里一直有数字**，但节点返回给 Python 的
结果里没有：

```text
"detail":{"all":[{"box":[584,1165,84,24],"text":"回合："},
                 {"box":[675,1170,13,14],"text":"3"}],
          "best":{"box":[584,1165,84,24],"text":"回合："},
          "filtered":[{"box":[584,1165,84,24],"text":"回合："}]}
```

`expected` 是 `回合\s*[:：]?\s*\d*`：**「回合：」能匹配，「3」这个独立文本框
匹配不上** → 被 `filtered` 剔掉。Python 侧的 `all_results` 对应的正是
`filtered`，所以代码永远只看到标签，看不到数字。

> 更正上一节的结论：「`all_results` 是全量、不受 expected 过滤」是**错的**。
> 受过滤的是 `filtered`，而 Python API 拿到的是过滤后的结果。

补充实测：上一轮加的独立节点 `FightRoundNumberDigit`
（OCR `expected: \d+`，`roi [650,1150,70,55]`）**一次都没命中过**
（`run_recognition` 3ms 返回、无任何识别事件）—— 小 ROI 里 13×14 px 的
数字 OCR 直接不产出结果。等于加了个永远为空的节点。

### 修复

| # | 改动 | 文件 |
| --- | --- | --- |
| 1 | `FightRoundNumber.expected` 改成数组 `["回合\s*[:：]?\s*\d*", "\d+"]` —— 两个文本框都保留 | `assets/resource/base/pipeline/city.json` |
| 2 | 删除永远不命中的 `FightRoundNumberDigit` 节点 | 同上 |
| 3 | `read_round_number()` 去掉「收紧 ROI 再认一次」分支，直接从宽 ROI 结果里取数字（`_extract_round_number` 本来就兼容「标签框 + 纯数字框」两种形态） | `agent/action/fight/battle_action.py` |
| 4 | `FightEndRound` 节点加固定点击点 `target: [359,1232]` | `assets/resource/base/pipeline/city.json` |
| 5 | 结束回合点击次数 2 → 3，失败等待 6/12/18 秒 | `agent/action/fight/battle_action.py` |
| 6 | `BigMapDismissItemGainBanner` 的滑动补 `duration: 1500`（默认 200ms 会被游戏当成点击，而横幅只响应滑动） | `assets/resource/base/pipeline/main_ui.json` |

### 关于「结束回合」点击落点

协议文档写明：`target` 为 4 元矩形时**在矩形内随机选点（中心概率高）**。
`FightEndRound` 用的是 `target: true`（= 刚识别到的框），落点在
`[333,1198,53,68]` 内随机 —— 实测两次失败的落点分别是 `(358,1201)`、
`(340,1229)`，都在框的边缘；同一时刻手动点 `(359,1232)`（框中心）回合立刻推进。
改成固定 2 元 `target` 后落点唯一。

### 实机验证

| 项 | 结果 |
| --- | --- |
| 单测 `python -m unittest discover -s tests` | ✓ **101 tests OK** |
| `python check_resource.py assets/resource/base` | ✓ All directories checked |
| 实机 OCR（`TestRoundWithDigit` 探针） | ✓ `回合：` + `4` 两个框都返回（score 0.998 / 0.994） |
| 实机 OCR（收紧 ROI 探针） | ✗ 确实一次都不命中（证实旧节点是死节点） |
| 复跑年度任务（4 个月） | ✓ 0 次「两次结束回合均未生效」；每回合一次点击即确认推进 |
| 复跑年度任务（4 个月，第二轮） | ✓ 第 4/4 个月完成 `12 -> 1`（**跨年翻页正常**） |
| 复跑年度任务（12 个月，真实配置） | ✓ 见下方「结果回顾」 |

## MaaMCP 跑年度任务的实操要点（2026-11-12）

| # | 坑 | 处理 |
| --- | --- | --- |
| 1 | `run_pipeline(start_agent=True)` 报 `Agent 启动失败 (identifier=xxx): connect() 返回 False` | 先 `clear_pipeline_resources()` 再跑：Resource 里残留的 agent 绑定会让新 agent 连不上 |
| 2 | `stop_pipeline` 之后 controller 从注册表被摘掉 | 重新 `connect_adb_device` 拿新 controller_id |
| 3 | MCP 返回 `status: succeeded` 可能**早于** agent 真正跑完好几分钟 | 完成与否只看 `assets/debug/custom/<日期>.log` 里的「年度任务处理完成」 |
| 4 | MCP 会**复用同一 controller_id 上已存在的 agent 进程** | 复跑前用日志行号/消息格式核对跑的是不是新代码（本次靠 `(1/2)` vs `(1/3)` 发现跑的是旧代码） |
| 5 | `interface.json` 的 `child_exec: ./python/python.exe` 从源码跑不起来 | 临时改成绝对路径 `F:/workspace/MAAGC/.venv/Scripts/python.exe`（收尾要还原） |
| 6 | 模拟器上战斗卡住时，先用 MCP `screencap` 看画面、`ocr`/`run_pipeline` 跑只读探针 | 别急着改代码；先用只读节点复现识别问题 |

## 结果回顾

- **年度任务**：跨年翻页实测通过（`12 -> 1`），实机跑完一整年。
- **根因修正**：上一轮「收紧 ROI 单独认数字」的方案方向错了（节点恒不命中），
  真正的根因是 **OCR 节点的 `expected` 会过滤掉独立文本框**。

## 全年年度任务实跑 + 图层切换改造（2026-11-12，续）

### 一、只跑了 4 个月而不是 12 个月

`auto_task.json` 里 `YearlyTaskMonths.expected = ["12"]`，日志却打
`年度任务执行月份数: 4`。**不是配置写错，是 MCP 侧的 Resource/Tasker 失配**：

`clear_pipeline_resources()` 只注销了 Resource，**缓存下来的 Tasker 仍绑定旧的
Resource**。之后 `run_pipeline` 里的 `override_pipeline(merged)` 写进的是**新的**
Resource，跑任务的 Tasker 根本不用它 → `YearlyTaskMonths` 节点保持旧值。

修复顺序（缺一不可）：

```text
stop_pipeline(controller_id)      # 从注册表摘掉 controller + tasker + agent
clear_pipeline_resources()         # 清 Resource
connect_adb_device(device_name)    # 拿新 controller_id（= 新 tasker）
run_pipeline(...)                  # 这次 override 才真正落到跑的那个 Resource
```

修复后日志打出 `年度任务执行月份数: 12`，跑满 12 个月。

### 二、地图图层切换 UI 已改版（`前往群岛` / `前往大陆` 已不存在）

实机验证（MuMu，ADB `127.0.0.1:16448`）：

| 旧 UI | 新 UI（实测） |
| --- | --- |
| 大地图上有「前往群岛」「前往大陆」按钮 | **两个按钮都不存在**，`ClickGoToArchipelago` / `ClickGoToContinent` 是死节点 |
| — | 底部中间页签显示**当前图层名**（大陆「加尔提兰」/ 群岛「瑞格群岛」）；点它弹出图层选择弹窗，列出**其它**图层 |

改造（`city.json` + `fight_utils.py`）：

| 节点 | 说明 |
| --- | --- |
| `MapLayerTab` | OCR `[".+"]`，roi `[297,1228,125,52]` —— 读当前图层名 |
| `MapLayerPopupOpen` | DirectHit 点击 `[360,1185]` —— 打开图层弹窗 |
| `MapLayerPopupOption` | OCR `[".+"]`，roi `[190,1055,370,65]` —— 弹窗选项，点击时用 `expected` 覆盖成目标图层名 |
| `MapLayerArchipelago` | OCR「群岛」，roi 同页签 —— 群岛层判据 |

`fight_utils` 新增：`read_map_layer_name` / `remember_continent_layer` /
`get_continent_layer_name` / `on_archipelago_layer` / `_map_layer_options` /
`_same_layer` / `switch_map_layer` / `wait_for_map_layer`。

三个踩坑点：

1. **页签文字会被截断**：「加尔提兰」读成「加尔提」→ 图层名比较用**双向子串**，
   不能严格相等。
2. **战斗页的「战斗开始」按钮和图层页签在同一块区域**（roi 重叠），
   OCR 会读成「战斗升始」。记大陆名**必须先确认在大地图上**
   （`_at_bigmap()`），否则会把按钮文字当图层名记下来（实机复现过）。
3. **弹窗选项 ROI 必须收紧**到弹窗标签带 `[190,1055,370,65]`；
   ROI 太宽时弹窗关闭状态下会读出垃圾文本（「办：」）。

### 三、海盗袭击庄园事件改走横幅直点

旧流程：切到群岛层 → 找「海盗侵袭」入口。实测**大陆层大地图上就有横幅**
「海盗正在袭扰你的庄园。」，点它 → 自动跳群岛并打开「海盗侵袭」准备页。

新链路（`pirate_raid_processor.py`）：

```text
点横幅 → 进入战斗 → 航海确认(消耗 1 游戏月) → 战斗开始 → AutoFight → 结算 → 切回大陆
```

分支顺序改为：胜利页 → 战斗页 → 准备页 → 已在战斗 → 已在群岛 → **横幅可见**
→ 旧式切群岛兜底。每步之前先 `_dismiss_random_popup()` 清随机事件弹窗
（生娃、佣兵加入等会盖住「进入战斗」按钮）。

### 四、实机验证结果

| 项 | 结果 |
| --- | --- |
| `python -m unittest discover -s tests` | ✓ **113 tests OK**（101 基线 + 新增 12 条图层单测） |
| `python check_resource.py assets/resource/base` | ✓ All directories checked |
| 全年年度任务（MuMu 实机） | ✓ 21:49:42 → 22:15:51，月份推进 6→8→9→10→11→12→1→2→3→4→5→6→7（跨年），日志 `年度任务处理完成` |
| 全年运行中的错误 | ✓ 0 次「达到重试上限」/「未确认进度」/「结束回合均未生效」 |
| 图层切换 | ✓ `切换地图图层: 加尔提` 成功 |
| 海盗袭击事件 | ✓ 实机打完并结算，横幅消失 |

残留的良性告警（重试即成功，不影响流程）：
`被动阶段第 1 次点击后回合未变化`、`点击结束回合前仍读不到回合数字基线`。

### 五、收尾

- `assets/interface.json` 的 agent 段**已还原为上游值**
  （`child_exec: "./python/python.exe"`、`child_args: ["./agent/main.py"]`）。
  从源码用 MaaMCP 跑 agent 时需要临时改成绝对路径
  `F:/workspace/MAAGC/.venv/Scripts/python.exe` + `["-u", "../agent/main.py"]`
  （MCP 把 `.py` 参数解析成 `interface.json` 所在目录下的相对路径，
  且该仓库没有打包用的 `assets/python/python.exe`）。
- 删除临时探针文件：`debug/test_round.json`、`debug/hud_round.png`、
  `debug/tpl_endround.png`（`debug/` 本身在 .gitignore 里）。
- 新增 `tests/test_map_layer_switch.py`（12 条）：图层名读取、同名判定、
  大陆名记忆的三种守卫、切换到指定图层、回退到第一个选项、
  无目标且无回退时失败、已在目标图层时不重复点击。

