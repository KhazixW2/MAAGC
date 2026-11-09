# Custom 节点（Custom Actions）

> 本文档列出本仓库**已实现**的 8 个 Custom Action、1 个待补全的声明，以及新增 Custom Action 的标准流程。**仅覆盖本仓库现状**，协议级节点（`SubTask` / `ExpressionRecognition` / `ClearHitCount` / Custom Recognition）项目暂未使用，不展开。

## 声明方式

本仓库用 Python（Maafw 绑定 `maa` 库）实现 Custom Action，装饰器由 `AgentServer` 提供：

```python
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

@AgentServer.custom_action("XxxProcessor")
class XxxProcessor(CustomAction):
    def run(self, context, argv) -> bool:
        # 业务逻辑
        return True
```

## 8 个已实现的 Custom Action

| Action 名 | 实现文件 | 一句话功能 | Pipeline 引用点 |
|-----------|----------|-----------|----------------|
| `TaskProcessor` | [`agent/action/fight/fight_processor.py:298`](../../../agent/action/fight/fight_processor.py) | 单月任务调度：回大地图 → 处理本月节日 → `fight_utils.start_task` | `auto_task.json:30` |
| `YearlyTaskProcessor` | [`agent/action/fight/fight_processor.py:315`](../../../agent/action/fight/fight_processor.py) | 推年总入口：读 `CustomTaskBlacklist` → 加黑名单 → 循环推月份 | `auto_task.json:37` |
| `AutoFightProcessor` | [`agent/action/zshg/auto_fight_processor.py:71`](../../../agent/action/zshg/auto_fight_processor.py) | 战斗主循环：扫 BattleGrid、检测/行动阶段、识别格子状态、执行攻击。能力按职责拆到 `battle_camera.py`（镜头）、`battle_threat.py`（威胁层）、`battle_planner.py`（地图合并）、`battle_decision.py`（单角色决策）、`battle_action.py`（点击与校验）、`battle_vision.py` / `battle_geometry.py`（纯函数） | `fight_utils.json:75` |
| `ChildRec` | [`agent/action/zshg/child.py:326`](../../../agent/action/zshg/child.py) | 识别子项：父母姓名/爵位/佣兵团、六维属性、血脉、特性；可选好苗子提醒 | `child_info.json:283` |
| `DailyTaskProcessor` | [`agent/action/zshg/daily_task.py:11`](../../../agent/action/zshg/daily_task.py) | 每日任务调度：遍历 `BigMapMarket` / `BigMapMall` / `BigMapRewardToken` 按 `enabled` 标志执行 | `event_utils.json` 多处 |
| `MarryProcessor` | [`agent/action/zshg/marry.py:33`](../../../agent/action/zshg/marry.py) | 联姻/相亲主流程：进大厅、识别血统、姓名库匹配、自动确认或换下一个 | `marry.json` 多处 |
| `WeddingProcessor` | [`agent/action/zshg/marry.py:903`](../../../agent/action/zshg/marry.py) | 婚礼流程：OCR 爵位、按爵位优先级处理、确认婚礼 | `marry.json` |
| `TestFunc` | [`agent/action/zshg/daily_task.py:49`](../../../agent/action/zshg/daily_task.py) | **调试用** —— cv2 读本地 `1.jpg`，**生产请勿在 Pipeline 中调用** | — |

## TODO：已声明未实现

| Action 名 | 引用位置 | 缺失实现 |
|-----------|----------|---------|
| `LaunchShopping` | [`assets/resource/base/pipeline/event_utils.json:158`](../../../assets/resource/base/pipeline/event_utils.json) | `agent/` 下**零实现**（`grep -rn "LaunchShopping" --include="*.py" agent/` 无结果） |

**风险**：运行到引用此 Action 的节点时会抛"未注册 custom_action"异常。

**修复方向**：新建 `agent/action/event/launch_shopping.py`，按下方"新增 Custom Action 的标准流程"实现。

## 不展开的协议级节点

以下节点在 MaaFramework 协议中存在，但本仓库**当前未使用**——本文档不展开，需要时请参考 MaaFramework 官方协议：

- `SubTask`（顺序执行子任务列表）
- `ClearHitCount`（清除节点命中计数）
- `ExpressionRecognition`（布尔表达式识别）
- Custom Recognition（`custom_recognition` 注册的自定义识别器）

## 新增 Custom Action 的标准流程

1. **确定命名**：以业务域为前缀，PascalCase + `Processor`/`Rec` 等后缀（如 `MarryProcessor`、`ChildRec`）。
2. **新建文件**：在 `agent/action/{域}/` 下创建 .py（如 `agent/action/event/launch_shopping.py`）。
3. **继承基类**：
   ```python
   from maa.custom_action import CustomAction
   from maa.agent.agent_server import AgentServer

   @AgentServer.custom_action("XxxProcessor")
   class XxxProcessor(CustomAction):
       def run(self, context, argv) -> bool:
           # argv 是从 pipeline 传入的 JSON 字符串
           return True
   ```
4. **保证被加载**：在 [`agent/agent_allfile.py`](../../../agent/agent_allfile.py) 中 import 该模块（即使不直接使用类），否则 `@AgentServer.custom_action` 装饰器不会执行，注册不生效。
5. **Pipeline 引用**：
   ```jsonc
   "MyNode": {
       "recognition": "DirectHit",
       "action": "Custom",
       "custom_action": "XxxProcessor",
       "custom_action_param": { "key": "value" }
   }
   ```
6. **测试**：用 [node-testing.md](./node-testing.md) 流程验证，**严禁**在有资源消耗的节点上点确认。

## 已实现但未在 Pipeline 引用

- `TestFunc`（`agent/action/zshg/daily_task.py:49`）—— 本地 cv2 调试用，**不应**被任何生产 pipeline JSON 引用。
- `agent/action/zshg/role_utils.py` 与 `agent/action/fight/fight_utils.py` **import 了 `CustomAction` 但未声明任何 `custom_action` 装饰器**——仅为未来扩展预留。
