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
