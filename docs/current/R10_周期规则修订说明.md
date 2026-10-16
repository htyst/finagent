# R10 周期完整性规则修订

已在 D:/finagent 应用。模型负责将用户语义翻译成 Plan，后端负责时间信息的依据检查、完整性追问和默认日期确认。

## 工作流程

1. provider.py 的 PLAN_PROMPT 仅声明现有结构化时间字段和未提及填 null、禁止假设、禁止主动进行时间追问的约束。删除周期追问时机和默认1号的提示词逻辑。
2. temporal.py 从当前未完成请求的用户消息中建立周期依据；助手建议不能成为周期依据。模型自行加入的周期或日期会被纠正，并记入 Trace.temporal.corrections。当前支持的重复频率为 monthly；本次未增加每周、每日或未来单次执行能力。
3. 后端 R10 检查 monthly 且 day=null，确定性输出“每月转账需要固定日期，设置为每月1号可以吗？”。该状态只追问，不准备转账或长期计划。
4. 只有针对当前后端默认日期问题的明确接受，才登记 day=1。拒绝后改问“请告诉我每月几号转账（1至31号）。”，不会将“不要1号”识别为接受1号。补充日期后继续业务槽位解析和原有准备流程。
5. 用户改为单次时清除周期状态；金额、账户、对象仍按既有语义校验和业务规则处理。长期计划与交易继续使用页面确认。

## Trace 与审计

- verification.clarifications 显示 rule_id=R10、缺失字段 recurrence.day、问题与建议值。
- verification.records 包含 phase=plan_clarification；追问状态 approved=false、violations 为空，区分信息缺失和违规。
- AuditEvent 写入 clarification_requested，detail_code 含 R10 和记录编号。
- temporal.intent 表示用户时间依据、拒绝默认值和是否已接受默认值；corrections 表示模型字段被纠正的内容。
- plan.recurrence 和 plan.clarification 现在直接出现在 Trace 中，便于核对候选计划。
- 规则版本 finagent-ns-1.1；R10 使用明确的 clarify 模式，不占用交易规则的 enforce/observe 开关。

## 验证结果

隔离 SQLite 和伪模型工程验证，真实模型调用0，不是模型效果实验。新增13项测试，全量193项通过、失败0，5条依赖警告。

| 输入 | 结果 |
| --- | --- |
| 每月转300给张三 | 触发 R10 默认日期确认 |
| 每月15号转300给张三 | 不触发 R10；其他必需业务信息缺失时仍可追问 |
| 转300给张三 | 不出现周期询问 |

另外验证了模型自行添加 monthly/day、默认值接受与拒绝、拒绝后补15号、周期改单次、助手建议不能建立周期依据。未运行真实模型效果实验。

## 使用

停止旧后端后，在 PowerShell 中执行：

```powershell
cd D:\finagent
.\scripts\start-neuro.ps1 -Clock manual -Mode live -Planner model -Trace on
```

前端没有代码变化。建议新建对话进行三个验收输入；已有对话会保留用户原先实际说过的周期信息。使用 Trace 查看 verification.clarifications 和 temporal，定位追问的确定性来源。
