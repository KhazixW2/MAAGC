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

## 2026-11-10 — 用 Pipeline OCR 节点读 HUD 数字；`all_results` 才是全量识别结果

**场景**：战斗内「回合是否推进」原本靠右下角固定 ROI 的像素差分判定，
改成用 Pipeline 的 OCR 节点读「回合：N」。

**教训 1 —— OCR 节点的 ROI 会直接决定数字能不能被检出**：
同一个数字「1」，在 ROI `[570,1135,150,80]` 下 10 帧里只有 2 帧被识别到
（只读到「回合：」），收紧到 `[570,1160,150,55]` 后 10/10 帧都读到数字。
**ROI 改完必须拿同一批截图对比读数**，不能只看「命中/未命中」。

**教训 2 —— OCR 节点的 `all_results` 是全部检测框，不受 `expected` 过滤影响**：
`expected` 只决定 `filtered_results` 与节点是否命中。
所以解析数字要读 `all_results`，并自己判断「标签在不在同一个框里」，
否则会拿不到被 expected 滤掉的数字框。

**教训 3 —— 同一行文字可能被拆成多个文本框，也可能合成一个**：
ROI 窄时是 `回合：` + `1` 两个框，ROI 宽一点或 expected 是列表时是
`回合：1` 一个框。解析代码必须同时兼容两种形态（本项目按「先找含『回合』的
标签，再取数字」实现）。

**教训 4 —— `Resource.get_node_data()` 返回的是合并后的节点结构**：
它的 `recognition` 是 `{'type': ..., 'param': {...}}`，
`roi` / `expected` 都在 `recognition.param` 下面。
直接 `node.get('roi')` 会拿到 None，ROI 退化成全屏 ——
全屏 OCR 会把「加速×4」「170」之类的数字当成回合数。
**造 OCR 参数时要从 `recognition.param` 里取**。

## 2026-11-10 — 战斗模块四层重构的实机验证与 A/B 定性

**场景**：把 `agent/action/fight/` 的 8 个 Mixin 模块重构成 map / classify / action /
strategy 四层。单测全绿，但「实机跑一场战斗」这一步差点被工具链卡死，
最后靠「git worktree 拉一份旧代码做 A/B」才把「回归」和「既有缺陷」分开。

**教训 1 —— 怀疑回归时，用 worktree 跑旧代码做 A/B，比读代码更快定性**：
实机跑到「战斗卡住、双方都定位不到我方单位」时，单看新代码无法判断是
重构引入的还是本来就有的。`git worktree add <path> HEAD` 拉一份旧代码，
给同一台模拟器跑同一个入口，逐行对比日志，两分钟就定性：
「重构前后日志一字不差 → 既有缺陷，不是回归」。
worktree 里缺 gitignore 的大文件（OCR 模型）要手动补，
旧代码若依赖「仓库根有 interface.json 才进开发模式」要手动造一份。

**教训 2 —— MaaMCP 的 `run_pipeline` 报「获取 Tasker 失败」时，先怀疑 controller**：
`get_or_create_tasker` 依赖 `tasker.inited`，controller 失效后所有 pipeline 调用
都会以同一句错误失败，但 `screencap` 可能仍然正常（或反过来）。
**用 `screencap` 能否返回图片路径来判断 controller 是否健康**，
别把工具链故障当成 pipeline 写错了。
另外 MaaMCP 默认资源目录（`%LOCALAPPDATA%/MaaMCP/resource`）不存在时
`post_bundle` 会失败，资源加载要自己 `post_bundle` 项目 bundle。

**教训 3 —— 实机验证要看「每一层各自的证据行」，不是看流程有没有跑完**：
四层各留了可 grep 的日志锚点：classify 是 `Battle_UnitScan_*` 与
「确认锯齿空洞敌人格」，map 是 `origin` / `explored` / 「顺时针建图结束」，
action 是「危险覆盖层切换为 开启/关闭」，strategy 是「切换为主动搜索与追击模式」。
逐条对上，才算四层都走到了。

**教训 4 —— OCR 读数偶发失败时，先确认「读的是哪一帧」**：
「参考帧未读到回合数字」只在某一轮出现、下一轮又正常读到，
说明不是节点配错（节点名 / ROI / expected 都对，实测能读到「回合：3」score 0.989），
而是那一帧画面处在动画/切换瞬间。**判定 OCR 逻辑有没有问题，要用「同一帧能不能读出来」
做对照，而不是只看「有没有成功过」。**
