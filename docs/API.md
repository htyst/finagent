# API 状态与契约

状态：M1–M6 接口已实现并验收；M7 补齐评估和交付材料。所有业务资源从服务端会话确定归属；客户端请求不得指定 `user_id` 来取得权限。响应统一含 `status`、`data`、`error_code`、`message`、`request_id`，并使用相符的 HTTP 状态码。跨用户 ID 与不存在 ID 均以不泄露资源存在性的方式拒绝。

## 会话与只读接口

| 方法与路径 | 输入要点 | 返回要点 | 阶段 |
| --- | --- | --- | --- |
| `POST /api/demo/session` | 演示用户 ID | 新会话、用户摘要、有效期；切换使旧会话失效 | M1 |
| `GET /api/demo/session` | 当前 cookie | 当前演示用户与会话有效性；刷新恢复 | M1 |
| `GET /api/accounts` | 当前会话 | 本人账户 ID、掩码、余额分、状态、版本 | M1 |
| `GET /api/cards` | 当前会话 | 本人卡 ID、尾号、关联账户、状态、版本 | M1 |
| `GET /api/recipients` | 可选姓名关键词 | 本人预置收款人 ID、姓名、尾号、状态；保留同名 | M1 |
| `GET /api/transactions` | 包含两端的日期、账户/卡、类别、偏移量 | 本人流水、总数、覆盖范围与关联证据 | M1/M2 |
| `GET /api/spending` | 时间、账户范围、类别、对比区间 | 整数分净额、消费/退款笔数、比较与覆盖状态、证据链接 | M2 |

## 任务与准备

| 方法与路径 | 输入要点 | 返回要点 | 阶段 |
| --- | --- | --- | --- |
| `POST /api/tasks` | `message`，当前会话、同源与 CSRF | 新建单场景对话任务，保存首条消息、状态与结构化步骤 | M4 |
| `POST /api/tasks/{id}/messages` | `message`，当前会话、同源与 CSRF | 追问、修订或受限工具结果；真实改参使旧操作单失效 | M4 |
| `GET /api/tasks/current`、`GET /api/tasks/{id}` | 当前会话或任务 ID | M3 结构化任务或 M4 对话任务、操作单状态、消息、步骤与实际回执 | M3/M4 |
| `GET /api/agent/tasks/current` | 当前会话 | 最近单场景对话任务，支持刷新恢复 | M4 |
| `POST /api/transfers/prepare` | 来源账户 ID、互斥目标 `{type, account_id|recipient_id}`、整数金额分、可选备注 | 不扣款；创建或复用固定操作单和任务上下文 | M3 |
| `POST /api/cards/{id}/actions/prepare` | `freeze` 或 `unfreeze` | 不修改卡；操作单或已处于目标状态 | M3 |
| `GET /api/proposals/{id}` | 操作单 ID | 脱敏固定摘要、状态、实际过期时间、结果引用 | M3 |

## UI 专用写入路径

| 方法与路径 | 输入要点 | 约束与结果 | 阶段 |
| --- | --- | --- | --- |
| `GET /api/demo/csrf` | 当前会话 | 不缓存的页面 CSRF 凭据；随会话失效 | M3 |
| `POST /api/proposals/{id}/confirmation-token` | 操作单 ID，当前同源会话及 CSRF 上下文 | 不缓存；仅 UI 得到一次性凭据，模型工具永远不可见 | M3 |
| `POST /api/proposals/{id}/confirm` | 操作单 ID、确认凭据；拒绝金额/目标等覆盖字段 | 事务内重验并执行；重复确认返回已存原结果 | M3 |
| `POST /api/proposals/{id}/cancel` | 操作单 ID | 仅取消未执行单；已执行返回原状态 | M3 |
| `POST /api/tasks/{id}/cancel` | 任务 ID | M4 取消未执行对话任务及其待确认单；不得撤销已完成业务 | M4/M5 |

## 模型工具与错误

模型工具仅有 `list_accounts`、`list_transactions`、`summarize_spending`、`list_recipients`、`list_cards`、`prepare_transfer`、`prepare_card_action`、`get_task_status`。所有入参使用严格 JSON schema，拒绝多余关键字段；不包含 `user_id` 或确认凭据。`commit_transfer` 与 `commit_card_action` 仅为确认接口内部领域服务。

常见错误码：`INVALID_INPUT`、`UNSUPPORTED_CURRENCY`、`RESOURCE_NOT_FOUND`、`SESSION_EXPIRED`、`INSUFFICIENT_FUNDS`、`LIMIT_EXCEEDED`、`STALE_RESOURCE`、`PROPOSAL_EXPIRED`、`PROPOSAL_CANCELLED`、`CONFIRMATION_INVALID`、`CONDITION_NOT_MET`、`DATA_INCOMPLETE`、`ACTIVE_WRITE_TASK_EXISTS`、`RETRYABLE_CONFLICT`、`MODEL_UNAVAILABLE`。运行后访问本机 `/docs` 或 `/openapi.json` 查看精确请求 schema 与 HTTP 状态。

配置键：`MODEL_PROVIDER`、`MODEL_NAME`、`MODEL_BASE_URL`、`MODEL_API_KEY`；运行模式由启动参数显式选择。数据库默认 `var/finagent.sqlite3`，测试只用临时路径。M1–M6 已实现范围如上；DeepSeek `deepseek-flash` 严格受限工具调用与 M4 live 页面流程已实测。M3 本地页面来源由 `FINAGENT_ALLOWED_ORIGIN` 显式配置，所有 M3 POST 需当前会话、同源 Origin/Referer 与 `X-FinAgent-CSRF`。

### GET /api/spending（M2 已实现）

- 默认：当前演示日期所在月的 1 日至演示当日，比较上月对应日期；全部本人账户。category 可为“餐饮”等已知类别或“未分类”，留空/“全部”为全部类别。
- 自定义日期：start、end 同时提供，格式 YYYY-MM-DD，API 包含两端日期；服务内转换为上海时区的左闭右开 UTC 区间。自定义主区间默认不比较。
- comparison_mode：none、previous_corresponding、previous_full_month、custom。两种默认月比较仅适用于默认本月主区间；custom 须同时提供 compare_start、compare_end。
- account_id 限当前会话用户；evidence_limit 为 1—100，evidence_offset 非负。完整匹配数据先聚合，随后才对本期证据分页。
- data.current.totals 返回消费分、退款分、净额分及两类笔数；is_complete=false 时净额只是已知小计。coverage.status=incomplete 列出缺口账户。data.comparison 提供基准净额、差额、按十进制四舍五入的一位百分比字符串；基准非正或任一区间不完整时百分比为空并给出原因，不完整时差额也为空。
- data.current.evidence.items 提供交易 ID、关联原消费 ID、类别、金额与带正负号的统计贡献；另有总数、页码参数。顶层包含 rules_version=spending-v1 和 dataset_version。所有数字由后端计算，不调用模型。

- `GET /api/transactions?category=全部` 与省略类别均不筛选；`category=未分类` 选择底层类别为 NULL 的所有本人流水，包括收入和转账；返回时这些记录都显示“未分类”。`GET /api/spending?category=未分类` 仍只将已入账消费和退款计入净额。

M2 的账本写方法仅在后端领域层和测试中使用；对外没有消费、退款、转账或卡状态裸写 API。

### M3 结构化操作接口

- 准备转账输入 `source_account_id`、带鉴别字段的 `target`、`amount_minor`（整数分）、可选 `currency=CNY`、`note`、`task_id`。准备卡片输入 `action` 和可选 `task_id`。不接受额外字段或客户端用户 ID；同一会话同时仅有一个待确认写任务。`task_id` 指向当前任务且改参时，旧操作单失效。
- 准备返回 `proposal` 和 `reused`；卡已处于目标状态则返回 `already_in_state`。操作单包含脱敏摘要、状态、实际 UTC 到期时间和成功后持久回执。有效期为真实时钟 5 分钟。
- 页面先 `GET /api/demo/csrf`，写请求携带 `X-FinAgent-CSRF`。确认前调用 `POST /api/proposals/{id}/confirmation-token` 获取只在该响应中出现的一次性凭据，再以 `confirmation_token` 调用 `/confirm`。确认不接受金额、目标等覆盖字段。成功、重试、刷新均可按任务或操作单读回同一持久结果。
- `POST /api/proposals/{id}/cancel` 仅改变未执行单状态；已成功业务不会被撤销。切换用户使旧会话操作单失效；重置清除旧会话与操作单。前端代理访问时设置 `FINAGENT_ALLOWED_ORIGIN=http://127.0.0.1:5173`。

### M4 单场景对话

- 任务模式由显式启动的 `FINAGENT_MODE=mock|live` 决定，创建后固定；回复包含 `mode`、`status`、`messages`、`steps` 和可选 `proposal`。跨用户任务 ID 按不存在处理。
- 模型工具白名单仅有八项只读/准备工具；金额、目标和确认资格由服务端校验。live 输出与服务端从用户要求解析出的参数不一致时拒绝，不改写已核实金额。数值说明由业务结构化结果生成，模型自由文本不会变为成功回执。
- 同名收款人必须选择尾号；缺金额、缺付款账户、非法日期和模糊交易会追问。聊天“确认”只提示页面按钮。任务更改真实业务参数时旧操作单失效；未改参追问保留原单。
- M4 每次处理最多一个模型工具请求，低于配置的 8 次预算；请求超时 30 秒，连接或临时服务失败最多重试一次。live 失败明确标记，绝不自动转 mock。


### M5 跨场景任务（已实现）

- `POST /api/tasks` 和 `/messages` 接收两条受限跨场景需求；返回 `plan_version`、`plan`、`requested_outcomes`、当前版本 `steps`，以及候选 `candidates` 和固定 `query_range`。步骤包含依赖、失败策略、结果和结果引用。历史版本仍保留在数据库。
- 多笔昨晚消费必须通过消息 `选择交易 <候选ID>` 选定；UI 提供按钮并显示商户、金额、北京时间、卡尾号。昨晚固定为演示前一天 18:00 至当日 06:00，右端不含。
- `POST /api/tasks/{id}/reject-operation` 仅拒绝当前待执行写操作，保留独立后续只读步骤；`POST /api/tasks/{id}/cancel` 停止整个任务。两者均要求当前会话、同源和 CSRF。已执行业务不能撤销或改报取消。
- `POST /api/tasks/{id}/resume` 重试失败的独立余额读取；不会重新写业务。GET 任务在持久回执基础上恢复流程，不调用模型；确认成功但响应丢失仍读回原回执。
- 操作单 `summary.condition` 是固定结构，`validation` 为最近持久核验结果。消费条件含类型、固定起止日期、类别、账户范围及整数分阈值；卡条件固定交易、卡和账户的关联与金额/时间。条件参与摘要哈希，确认不能覆盖。
- 条件由受限编排在服务端产生。M3 表单准备接口仍拒绝客户端额外 condition 字段；模型写工具 schema 不接收条件表达式。
- 确认在同一 `BEGIN IMMEDIATE` 中先重核条件，再核资源快照，随后业务、流水、回执、审计及对应 Agent 写步骤一起提交。条件不满足返回 409 `CONDITION_NOT_MET`；不完整返回 `DATA_INCOMPLETE`；关联变动返回 `TRANSACTION_CHANGED`；条件成立但版本变化返回 `STALE_RESOURCE`，均无写业务。
- M5 跨场景终态含 `partially_completed`；拒绝冻结但余额成功、写成功后读取失败均逐项展示。每次消息至多两次模型工具请求，每次最多一次临时故障重试；每个计划不超过 12 步及一个写操作。


### M6 页面同步与会话一致性

- 前端私有请求携带 `X-FinAgent-Expected-User`，服务端在会话认证后比对该页预期用户。不一致返回 409 `SESSION_CHANGED` 且不返回私有数据。该头用于检测跨标签页共享 Cookie 变化，不是认证凭据，也不能选择执行用户。
- `SESSION_CHANGED` 或当前会话的 `SESSION_EXPIRED` 使页面清空账户、任务、操作单。客户端还按会话代次过滤延迟错误，防止同一用户重新登录后被旧错误清除。
- 活动任务使用已有 GET 接口约每 2 秒查询；终态停止，隐藏页面暂停请求，恢复可见/联网/焦点时重新读取。请求失败后停止自动轮询，显示状态可能过时并禁用确认，保留只读恢复按钮。
- 开始写请求、用户切换、组件卸载及更新任务时废弃旧读取；账户刷新另有加载批次，防止确认前旧余额覆盖确认后新余额。读超时 15 秒，写请求等待上限 150 秒；不自动重发 POST。
- M6 不修改确认事务、条件核验与业务幂等语义，无新增数据库迁移。

## 交付核对

- 演示重置仅提供本地脚本 `scripts/reset-demo.ps1`，没有 `/api/admin/reset` HTTP 接口；旧版文档的该接口是草案，现已移除。
- `/api/health` 的 phase=M6 表示最后一个功能阶段；M7 是交付与评估阶段，没有新增业务 API。
- `GET /api/demo/info` 返回演示日期、种子、数据版本和 mock/live；`GET /api/demo/users` 返回演示用户选择列表。
- 真实授权仍由会话决定；预期用户请求头只用于防止跨标签页串显。

## 2026-09-26 复查修正

参数 schema 校验失败（如非法分页数、缺失或多余字段）返回 HTTP 422 和统一的 `error_code=INVALID_INPUT`、可读 message、request_id，不回显原始输入值。日期运算或时区转换超出范围时返回 HTTP 400 / INVALID_INPUT，不再产生 500。正常统计口径和业务写入规则没有变化。

数据库结构迁移是本地 `python -m app.data.cli migrate` 动作；必须存在数据库并位于项目 var 范围内，升级不会重置数据。`dev.ps1` 已调用该动作；直接运行 uvicorn 不隐式迁移。

## 2026-09-27 对话修复

- 任务响应 `steps[].arguments` 返回该步骤实际查询参数；流水证据仍为 `steps[].result.items/total/limit/offset`。页面使用同一 `start/end/category/account_id/card_id` 调用现有流水分页接口。
- 消费条件类型新增 `spending_lt`（严格小于），保留 `spending_gt`（严格大于）。修改条件使旧操作单失效；无法识别的修改返回追问，不保留可确认的旧单。普通转账可通过同一 `/messages` 追加消费条件，无需先取消重建。
- live 单场景模型结果迟到时，若取消已落库或有更新消息，原 `/messages` 请求返回当前持久任务，不再生成旧回复或旧操作单。取消后的任务保持取消；确认仍必须走既有专用接口。
