# 经验教训 / Lessons Learned

## 2026-11-04 — squash 合并旧基线 PR 分支会静默覆盖上游新提交

**场景**：PR #8（作者 Huan983）在**旧版上游基线**上开发，被 squash 合并（`5c5358a`，父 `e374746`）到 `main`。
因为是 squash 合并且 PR 触碰了同样的文件，上游在 `e374746` 之后的 6 个战斗提交
（`63660b5` / `91337c0` / `e50dcaa` / `e82dfe1` / `2949135` / `2bc6db2`）被 PR 分支的旧版本**静默覆盖**，
合并信息里完全看不出来。

**识别方法**（下次遇到"合并后行为莫名变了"先跑这两条）：

```bash
git log --oneline --first-parent -5                       # 找到刚合进来的那次合并
git diff --stat <PR真实基线> <合并提交>                   # PR 到底改了多少
git diff --stat <上游HEAD>   <合并提交>                   # 合并相对上游实际引入了什么
```

本例中两条 `--stat` 结果差异巨大（+5155/-365 vs +803/-58），
且小 diff 只落在 4 个战斗文件上 —— 这就是"旧基线 PR 覆盖新上游"的指纹。

**正确做法**：

1. 先确认要保留的功能（这里是 PR 的市场采购），再决定回退范围。
2. `git checkout <上游HEAD> -- <文件>` 做整文件回退（**最稳**，不留半吊子混合体）。
3. 需要保留的功能用**外科式**补回：把 PR 里的代码块原样取出、贴到上游基线上，
   补完再逐个依赖符号核对（`grep` 出该代码块引用的所有内部符号，确认上游都有）。
4. 工作区如果有**未提交的改动**（本例是 `city.json` 的 `FightFail` 节点），
   **不能整文件 checkout**，否则会被抹掉 —— 改用定点 Edit 回退受影响的节点。

**配套验证**（回退后必做，不只是跑单测）：

- **节点引用审计**：pipeline JSON 的 `next` 链是**字符串引用**，Python 代码里的
  `run_task("X")` 也是字符串引用。只 grep Python 会漏掉 JSON 内部的悬空引用，
  必须把「所有 pipeline 定义的节点名」和「所有被引用的节点名」做双向差集。
- **回退的测试必须同步回退**：本例 `tests/test_task_board_recovery.py` 断言的是
  "刷新 0 次 + 调 `_rest_to_skip_month`"，而上游断言的是"刷新 1 次" —— 测试文件
  和被回退的实现是**一对**，不同步回退就会出现互相矛盾的断言。
- **残留符号 grep**：`SkipMonth` / `_rest_to_skip_month` / `MAX_CONFIRMED_ROUNDS` 等
  被删符号在 `agent/` `assets/` `tests/` 全量搜一遍，确认 0 命中。
- **资源自检的正确参数是 `assets/resource/base`**（不是 `assets/resource`，
  后者不是合法 bundle 路径，必然失败 —— 排查时别被它误导）。

## 2026-11-04 — `.venv` 里没有 pytest，别硬试

项目测试是 `unittest` 风格，`python -m pytest` 会直接报 `No module named pytest`。
正确的 runner 是：

```bash
.venv/Scripts/python -m unittest discover -s tests -v
```

（注意：用户的全局 python 规则推荐 pytest，但本项目 venv 未安装，以项目实际为准。）

## 2026-11-09 — 拆分类时别丢框架基类；`run()` 局部变量 ≠ 实例属性

**场景**：把 3280 行的 `auto_fight_processor.py` 拆成 9 个模块（Mixin 组合），
编译通过、AST 比对全绿，但**运行期直接炸**：

```
AttributeError: 'AutoFightProcessor' object has no attribute 'c_handle'
  at maa/agent/agent_server.py -> register_custom_action -> action.c_handle
```

**根因**：`@AgentServer.custom_action(...)` 装饰器在 **import 时**就实例化
action 并调用 `register_custom_action`，后者读取 `action.c_handle`
（`CustomAction` 的 property）。原类是 `class AutoFightProcessor(CustomAction)`，
拆分后只继承了 Mixin —— **框架基类被悄悄丢掉了**。
`py_compile` 完全测不出来（它不执行装饰器），只有 import 才暴露。

**教训 1 —— 拆分类先抄基类列表**：
新类的 `class X(A, B, C)` 必须包含原类的**全部**基类（框架基类、ABC、mixin）。
MRO 用 C3 线性化，多继承完全兼容：

```python
class AutoFightProcessor(
    BattleCameraMixin, ..., BattleActionMixin, CustomAction,
):
```

**教训 2 —— 局部变量改实例属性会放大作用域**：
原实现把"跨回合敌人方向记忆"写成 `run()` 里的**局部变量**，每次调用
（含任务被恢复后重新调用）都从零开始。顺手改成 `self._last_known_enemy_direction`
后，记忆会**跨 `run()` 调用残留** —— 因为 `AgentServer` 复用的是同一个
action 实例（`_custom_action_holder`）。改实例属性时必须逐个确认
"这个变量原本的作用域是什么"，必要时在 `run()` 开头显式重置。

**教训 3 —— 拆成纯函数会打断 monkeypatch**：
把实例方法提成模块级函数后，`tests/` 里
`processor._xxx = lambda ...` 这类打桩会**静默失效**（不再被调用，
测试却可能因为断言变松而"通过"）。必须同步改 patch 目标到新的模块路径，
并确认断言仍然真的被执行。

**教训 4 —— 单测数量本身就是回归信号**：
拆分前 61 tests OK；拆分后 import 失败 → 只跑到 24 个用例（其余模块
import 失败被跳过）。跑测试时**先看用例总数**，和基线对得上再谈"通过"。

## 2026-11-10 — OCR 节点的 ROI 要整行包住；裸关键词的全屏 OCR 等于没有判据

**场景**：战斗胜利后弹出「表白失败」随机事件信纸，年度任务卡死，
并误报「战斗失败结算页，不自动重开战斗」。

**教训 1 —— ROI 必须完整包住文本行，不能切字**：
`Event_ConfessionFail` 的 ROI 从 x=195 开始，而横幅首字在 x=177 —— 切掉了第一个字。
结果不是「少认一个字」，而是**整行都读错**：同一行文字
`感a力b科虎佩公向肖恩表白失败！`(0.983) 在 `Event_ConfessionSuccess` 的 ROI 下读对了，
在 `Event_ConfessionFail` 的 ROI 下变成 `力b科度佩公向省恩表自失收！`(0.728)。
检测器在截断行上给出的框只有 14px 高（正常 25px），rec 模型拿到的是残缺字形。
**ROI 宁可大不要切字**；调 ROI 后要拿同一张截图对比 OCR 读数，不能只看命中与否。

**教训 2 —— 裸关键词的全屏 OCR 等于没有判据**：
`FightFail` 从 TemplateMatch 改成「全屏 OCR 找『失败』」之后，任何带「失败」两个字的
画面都成了失败结算页，包括「表白失败」。这种节点必须匹配**完整短语**
（`战斗失败`），让别的页面的「失败」匹配不上。识别节点要么用模板图，
要么用足够长的专有短语。

**教训 3 —— 「识别成功」不等于「功能可用」**：
跨 5 个日志文件统计，`Event_ConfessionFail` 识别过 174 次、命中 **0** 次。
**节点要统计命中率**：长期 0 命中说明 ROI / expected 配错了，
而不是「这个事件没出现过」。只在报错时才看日志，永远发现不了这种问题。

**教训 4 —— 同一类弹窗只留一条处理路径**：
原来有「战后中断事件」专用恢复分支，和通用的「回到大地图」各写一套。
特例分支反而成了故障点（点屏幕中心对模态信纸无效，只能空转）。
现在事件处理只有 `detect_and_manage_event` 一处，
由 `ensure_at_bigmap` 复用，战前 `preprocess_events` 也走它 ——
特例分支越少，能漏的路径就越少。
