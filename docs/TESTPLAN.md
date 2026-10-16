# FinAgent 测试方案

> 面向"直观感受整个项目 + 便于后续优化"的可执行测试方案。
> 编写日期：2026-10-01 ｜ 被测版本：`D:\finagent`（M7 已完成 + 2026-09-27 四项审查修复）
> 本方案不修改任何业务逻辑，只新增测试资产。

---

## 0. 先看结论：三条命令建立整体印象

如果你只想花 10 分钟感受这个项目，按顺序跑这三条：

```powershell
# ① 体检：全部 113 项后端测试 + 前端类型检查与构建（实测约 30–43 秒，不花钱、不调模型）
.\scripts\testplan.ps1 -Tier standard

# ② 看报告：自动打开可视化仪表盘（HTML，单文件，可发给别人）
#    testplan.ps1 跑完会自动打开 var\testplan\latest\dashboard.html

# ③ 上手摸：启动界面，按第 6 节的"手工演示脚本"逐条点
.\scripts\dev.ps1 -Mode mock
```

想省时间，先跑 7 秒的冒烟层：`.\scripts\testplan.ps1 -Tier quick`。

---

## 1. 这个项目是什么（测试对象画像）

FinAgent 是一个**基于合成数据的模拟银行智能助手**：React 工作台 + FastAPI 后端 + SQLite 账本 + DeepSeek 受限工具调用。

关键架构约束（决定了测试策略）：

| 约束 | 含义 | 测试上的后果 |
|---|---|---|
| 金额一律**整数分** | 无浮点误差 | 断言用精确整数相等，不用容差 |
| 模型**不能**决定身份、算钱、直接提交 | 模型只选工具、给参数 | 模型相关测试只验"参数与预期一致"，不验"模型很聪明" |
| 写业务必须**页面按钮确认** | 聊天文本不构成授权 | 所有端到端写测试必须走确认接口，不能走对话 |
| 写入口有**CSRF + 同源 + 一次性确认凭据** | 模型永远看不到凭据 | 安全测试要单独覆盖"模型/聊天拿不到凭据" |
| 业务在**单个 `BEGIN IMMEDIATE`** 事务内完成 | 条件重算 + 快照校验 + 扣款 + 回执 + 审计原子 | 并发/故障注入测试是重点，不是可选项 |
| 演示时间**冻结**在 `demo_date=2026-09-24`，时钟另有 `system_now` | 业务查询用 demo_date，会话/过期用真实时钟 | 测试必须区分这两个时间轴，否则过期用例会假通过 |

### 1.1 演示用户与账户（手工测试的核对基准）

| 用户 | 账户 | 余额 | 卡尾号 | 用途 |
|---|---|---|---|---|
| **林晓** `u1` | 活期 `4082` | 10,000.00 元 | 1234（日常借记卡） | 主演示 |
| | 储蓄 `6105` | 20,000.00 元 | 5678 | 转账目标 |
| 陈予 `u2` | 日常 `2201` | 6,800.00 元 | 2233 | 隔离对照 |
| | 备用 `2202` | 5,000.00 元 | 2244 | 隔离对照 |
| 周宁 `u3` | 工资 `3301` | 7,600.00 元 | 3355 | 已冻结卡 |
| | 停用 `3302` | 3,000.00 元（disabled） | 3366 | 停用资源 |

林晓的预置收款人：**张三 3821 / 张三 9156（同名，用于消歧）/ 李文 7072**。

### 1.2 手工核对的"标准答案"（已在真实库上实测复核）

| 口径 | 手算过程 | 期望值 | 实测 |
|---|---|---|---|
| 本月餐饮**净消费** | 1000.00 + 250.00 − 50.00 | **1200.00 元** | `120000` 分 ✅ |
| 上月同期餐饮净消费 | 900.00 − 100.00 | **800.00 元** | `80000` 分 ✅ |
| 环比增长 | 1200/800 − 1 | **50.0%** | 与 `DEMO.md` 一致 ✅ |
| 本月购物净消费 | 88.00 + 11.00（待入账 77.00 不计） | **99.00 元** | `9900` 分 ✅ |
| 本月交通净消费 | 24.00 | **24.00 元** | `2400` 分 ✅ |
| 本月未分类净消费 | 32.00 | **32.00 元** | `3200` 分 ✅ |
| 昨晚消费 | 前一日 18:00–当日 06:00 北京 | 1 笔待核对商户 88.00 元 购物 尾号 1234 | ✅ |
| 账本恒等式 | 余额 = 期初 + 已入账贷记 − 已入账借记 | 6/6 账户差额为 0 | ✅ |

> 这 8 行就是"人工核对期望值"的黄金标准。**任何测试如果出现别的数字，先怀疑实现，不要改期望值。**

---

## 2. 测试策略：四层金字塔

```
        ┌─────────────────────────────────────────┐
   T4   │  E2E 页面验收（Edge 真浏览器 + 真实库）    │  慢、脆、最有说服力
        │  browser-m3/m4/m5/m6、受控响应竞态回归     │  ~15–25 分钟
        ├─────────────────────────────────────────┤
   T3   │  模型行为评估（mock / live，40 条样例）    │  live 花钱、有随机性
        │  evaluate.ps1、model-smoke、排练录像       │  ~1–10 分钟
        ├─────────────────────────────────────────┤
   T2   │  集成测试（FastAPI TestClient + 临时库）   │  113 项，~30 秒，主力
        │  事务、并发、幂等、故障注入、权限、恢复     │
        ├─────────────────────────────────────────┤
   T1   │  领域单元测试（金额、统计、条件、时钟）     │  毫秒级，定位最快
        └─────────────────────────────────────────┘
```

**分层原则**

1. **T1/T2 是回归主力**：确定性、无网络、无模型、零成本。改代码后必须全绿。
2. **T3 是能力评估**，不是回归。它有随机性、要花钱，因此**永远不进 `check.ps1 -Stage All`**，必须显式触发。
3. **T4 是"用户真的能用"的证明**。T2 全绿不代表页面能点通——历史上出现过前端构建通过但页面流程失败的情况。
4. **明确不测**（见 `ARCHITECTURE.md` 当前限制）：真实身份认证、真实支付、生产规模/多租户、理财推荐、任意自然语言规划、人工效率结论。**不要为这些写测试，也不要声称已验收。**

### 2.1 每个用例的判定原则（来自 `TEST_MATRIX.md`，必须遵守）

- 断言**持久化状态**（数据库余额、卡状态、业务结果条数），**不以页面成功文案为准**。
- 期望值用**手算**，不得用被测函数自己算出的值当期望（防止"自己证明自己"）。
- 浏览器流程必须**从 UI 发起**，然后回数据库验证结果。
- 每次测试用**独立临时数据库**，不污染 `var/finagent.sqlite3`。

---

## 3. 测试资产的现状盘点

### 3.1 已有的确定性检查（不调模型、无费用）

| 入口 | 覆盖 | 规模 | 大致耗时 |
|---|---|---|---|
| `scripts\check.ps1 -Stage M1..M6\|All` | 后端累计 + 前端构建 + 浏览器流程 | 113 项后端 + 6 项页面回归 + 4 组页面验收 | All ≈ 20–30 分钟 |
| `pytest backend\tests` | 全部后端（含本轮新增 10 项） | **113 项** | 约 25–40 秒（实测，见 7.5） |
| `npm.cmd run build --prefix frontend` | TypeScript + Vite 构建 | — | 约 20–40 秒 |
| `npm.cmd run browser:regression --prefix frontend` | 受控响应竞态 | 6 项 | 约 1 分钟 |
| `scripts\browser-m3/m4/m5/m6.ps1` | M1–M6 页面验收 | 4 组 | 约 15–25 分钟 |
| `scripts\test-startup.ps1` | 中文/空格目录、旧库升级、启动失败清理 | 3 类 | 约 2–3 分钟 |
| `scripts\test-setup.ps1` | 安装脚本与失效虚拟环境 | — | 约 1–2 分钟 |

### 3.2 已有的模型评估入口（**会花钱**）

| 入口 | 说明 |
|---|---|
| `scripts\evaluate.ps1 -Mode mock\|live -Split dev\|holdout` | 30 条开发 + 10 条保留样例；独立临时库 |
| `scripts\evaluate.ps1 -Mode live -Split dev -Suite key -Repeat 3` | 关键集合三轮重复 |
| `scripts\model-smoke.ps1` | M1 DeepSeek 兼容性小试验（只读工具） |
| `scripts\rehearse.ps1 -Mode live -Repeat 3` | 自动录像排练（需 FFmpeg 组件） |
| `scripts\compare-process.ps1` | 表单 vs Agent 流程对照实验 |
| `scripts\reproduce.ps1 -Browser` | 独立副本复现（拷贝源码 + 新建 venv/node_modules） |

**费用护栏**：以上 live 入口**都不在默认检查里**。跑之前先确认 `.env` 里的 `MODEL_API_KEY`，并知道会产生真实调用费用。

### 3.3 本次新增的资产

| 文件 | 作用 |
|---|---|
| `docs\TESTPLAN.md` | 本文件：策略、分层、判定原则、优化建议 |
| `docs\TESTPLAN-CASES.md` | 用例清单：手工用例 MC-01…MC-20 + 自动化归属映射 |
| `docs\TESTPLAN-INVENTORY.md` | 能力↔用例↔证据的完整对照表（41 能力点 / 25 端点 / 17 缺口） |
| `scripts\install-testplan.ps1` | 把测试方案装进项目，并在需要时一次性修好目录写权限 |
| `scripts\testplan.ps1` | 一键测试运行器：分层执行、计时、收集证据、出报告 |
| `tools\gen_dashboard.py` | 把结果 JSON 渲染成单文件可视化仪表盘 |
| `tools\gen_inventory_doc.py` | 从机读清单生成可追溯性矩阵（防止文档与代码漂移） |
| `tools\probe_demo_facts.py` | 只读探查演示库，打印可用于手算核对的事实 |
| `tools\gap_priorities.json` | 覆盖缺口的人工分级（出现次数多 ≠ 风险高） |

### 3.4 安装到项目

```powershell
# 在项目根目录执行；脚本会自行定位项目，也可用 -ProjectRoot 指定
pwsh -File .\scripts\install-testplan.ps1
```

安装脚本做四件事：

1. 把 `docs\TESTPLAN*.md`、`scripts\testplan.ps1`、`tools\*.py` 复制进项目；
2. 若项目目录**不可写**，自动申请一次提权（会弹 UAC），只给当前用户补上 `<项目>` 目录的"修改"权限 —— **不夺取所有权、不改动任何内容**；
3. 复制后用真实的项目源码重新生成一次 `TESTPLAN-INVENTORY.md`；
4. 跑一次 `-Tier quick` 验证安装结果。

> 权限修复只作用于 `<项目>` 目录本身，并通过继承影响子目录；只在确实不可写时触发。
> 不想改权限也可以：直接用外部目录运行（加 `-ProjectRoot D:\finagent`），或手工复制文件。

---

## 4. 一键测试运行器：`scripts\testplan.ps1`

### 4.1 三层可运行档位

| 档位 | 做什么 | 耗时 | 何时用 |
|---|---|---|---|
| `-Tier quick` | 领域+核心 API 冒烟子集（5 项）+ 演示库事实探查 | **实测 ~7 秒** | 每次改完代码立刻跑 |
| `-Tier standard` | **全部 113 项后端** + 前端类型检查与生产构建 + 生成报告 | **实测 27–43 秒** | 提交前、日报 |
| `-Tier full` | standard + 受控响应回归 + M3–M6 浏览器验收 + 启动集成 | ~20–30 分钟 | 里程碑、交付前 |

### 4.2 用法

```powershell
.\scripts\testplan.ps1 -Tier quick
.\scripts\testplan.ps1 -Tier standard
.\scripts\testplan.ps1 -Tier full
.\scripts\testplan.ps1 -Tier standard -NoOpen          # 不自动打开浏览器
.\scripts\testplan.ps1 -Tier standard -OutDir 'D:\tmp\我的测试报告'
```

### 4.3 产出物（默认写进项目的 `var\testplan\latest\`）

```
var\testplan\latest\
├── dashboard.html      ← 可视化仪表盘（双击即可看，可转发）
├── results.json        ← 机器可读结果，供后续趋势对比
├── summary.md          ← 纯文本汇总，方便贴进 issue / 周报
├── demo-facts.json     ← 演示库事实快照（手算核对的依据）
├── junit-backend.xml   ← 后端逐项结果
└── logs\*.log          ← 每个步骤的完整原始输出
```

> **只读工作区的降级行为**：如果项目目录暂时不可写（例如权限被收回），运行器会自动把结果写到 `%USERPROFILE%\finagent-testplan\<时间戳>\`，并在控制台打印回填命令。不会因此失败。

---

## 5. 手工测试用例（MC 系列）

完整清单见 [TESTPLAN-CASES.md](TESTPLAN-CASES.md)。这里只列出每条的核心判定点，方便你照着点：

| 编号 | 名称 | 关键判定 |
|---|---|---|
| MC-01 | 会话与三用户隔离 | 切到陈予后看不到林晓的账户；`SESSION_CHANGED` 清空私有数据 |
| MC-02 | 账户/卡/收款人只读展示 | 金额、尾号、状态与 1.1 表一致；无任何写入口 |
| MC-03 | 本月餐饮统计（**核心数字**） | 净消费 1200.00，消费 2 笔，退款 1 笔，环比 +50.0% |
| MC-04 | 统计证据下钻 | 证据逐笔带正负号且合计等于 1200.00；分页不重复 |
| MC-05 | 覆盖不完整 | 周宁 `3302` 账户区间有缺口时标 `incomplete`，**不输出**总额与百分比 |
| MC-06 | 流水筛选与未分类 | `未分类` 只筛底层 category 为 NULL；收入/转账显示为"未分类"但**不计入消费** |
| MC-07 | 表单转账（含同名消歧） | 准备**不改余额**；确认后活期 −100、储蓄 +100，成对流水 + 审计 |
| MC-08 | 幂等与重复确认 | 重复提交只产生**一条**业务结果，余额只变一次 |
| MC-09 | 日报额与单笔上限 | 单笔 >5000 元拒绝；当日累计 >10000 元拒绝，均无写入 |
| MC-10 | 卡片冻结/解冻 | 准备不改卡状态；确认后变更；已处于目标状态时返回当前状态且**不新建**写操作 |
| MC-11 | 聊天"确认"不执行 | 对话里说"确认"只提示点按钮，数据库无变化 |
| MC-12 | 跨场景：陌生消费→冻结→查余额 | 多笔候选先选择；余额与冻结结果逐项真实 |
| MC-13 | 仅拒绝冻结、继续查余额 | 卡保持正常，余额成功返回，任务为 `partially_completed` |
| MC-14 | 条件转账成立 | 条件 `1200 > 500` 成立，扣款 100 元，余额 9900/20100 |
| MC-15 | 条件转账不成立 | 插入退款使净额降至 400 后确认，返回 `CONDITION_NOT_MET`，**不扣款** |
| MC-16 | 操作单过期（5 分钟真实时钟） | 超时确认被拒；不复用过期单 |
| MC-17 | 刷新/重启恢复 | 刷新后读回同一回执，**不重复扣款** |
| MC-18 | 权限与 CSRF | 无 CSRF、跨源、跨用户 ID 一律拒绝，且不泄露资源存在性 |
| MC-19 | 前端类型/构建 | `npm run build` 退出码 0，无 TS 错误 |
| MC-20 | 页面同步与故障恢复 | 轮询约 2 秒、终态停止；断网后停自动轮询并禁用确认 |

---

## 6. 五分钟手工演示脚本（照着敲就能感受全貌）

先用 `.\scripts\reset-demo.ps1 -Date 2026-09-24` 回到干净基线，再 `.\scripts\dev.ps1 -Mode mock`，打开 `http://127.0.0.1:5173`，选**林晓**。

| 步骤 | 你输入 / 你点击 | 你应该看到（判定点） |
|---|---|---|
| 1 | 看顶部 | 模式 **MOCK**、演示日期 **2026-09-24**、活期 10,000.00 / 储蓄 20,000.00、卡尾号 1234 **正常** |
| 2 | 输入"这个月餐饮消费多少？" | 净消费 **1200.00 元**；消费 2 笔 / 退款 1 笔；环比上月同期 800.00 元，**+50.0%** |
| 3 | 展开消费证据 | 3 条：1000.00(+) / 250.00(+) / 50.00(−)，合计正好 1200.00 |
| 4 | 输入"昨晚有笔不认识的消费，帮我查是哪张卡；如果是尾号1234，帮我临时冻结它，再告诉我账户可用余额。" | 出现待核对商户 88.00 元、北京时间 21:10、卡尾号 **1234**；给出**待确认**操作单 |
| 5 | **先别确认**，看一眼余额 | 余额**还没变**（准备不写业务）|
| 6 | 点击页面"确认" | 卡 1234 变**已冻结**；活期仍 **10,000.00**（冻结卡≠冻结账户）；逐步结果与真实回执一致 |
| 7 | 输入"如果本月餐饮超过500元，就从活期转100元到储蓄账户。" | 显示出**固定条件**（净消费 1200.00 > 500.00）与目标活期→储蓄 |
| 8 | 点击"确认" | 活期 **9,900.00** / 储蓄 **20,100.00**；刷新后回执仍在 |
| 9 | 输入"确认"（聊天） | 只提示"请在页面点击确认"，**数据库无变化** |
| 10 | 输入"从活期账户给张三转100元" | **追问**选择尾号（3821 / 9156），不猜 |
| 11 | 取消操作单，看余额 | 余额回到未扣款状态；已执行业务**不会**被撤销 |
| 12 | 切到**陈予** | 林晓的账户/任务/操作单全部消失；旧任务按不存在处理 |

> 这套 12 步覆盖了：只读查询、精确统计、证据下钻、消歧追问、准备与确认分离、跨场景编排、条件复核、幂等、聊天不授权、用户隔离。是"直观感受整个项目"性价比最高的一条路径。

---

## 7. 覆盖缺口与后续优化建议

下面的缺口**不是猜的**：每一条都经过源码逐文件核对，确认"文档承诺了 / 代码实现了，但没有任何测试断言它"。

**本轮成果**：新增 `backend/tests/test_gap_findings.py`（10 项用例），一次性关闭 **8 条**缺口
（G01、G02、G03、G04、G07、G10、G11、G15）。后端用例总数从 **103 → 113**，全绿。
仍待补 **9 条**，其中 P0 只剩 **2 条**（G08 前端单元测试、G17 前端金额解析）。
完整 17 条及其关闭情况见 [TESTPLAN-INVENTORY.md](TESTPLAN-INVENTORY.md) 第七节。

### 7.0 已关闭的 8 条（本轮）

| 编号 | 缺口 | 关闭用例 |
| --- | --- | --- |
| **G01** | `/api/health`、`GET /api/demo/session` 零 pytest 覆盖 | `test_health_and_demo_session_restore` |
| **G02** | `API.md` 的 `PROPOSAL_CANCELLED` 无实现产生 | `test_cancelled_proposal_confirm_returns_invalid_proposal_state`、`test_api_md_lists_error_code_no_backend_module_emits` |
| **G03** | `INSUFFICIENT_FUNDS` 无断言（准备+确认） | `test_prepare_rejects_insufficient_funds_without_writes`、`test_confirm_rejects_insufficient_funds_without_business_writes` |
| **G04** | 会话 8 小时过期分支从未触发 | `test_expired_demo_session_blocks_reads_and_writes` |
| **G07** | HTTP 层 `PROPOSAL_EXPIRED` 分支未覆盖 | `test_http_layer_reports_proposal_expired_after_token_window` |
| **G10** | 日额度上海日切边界无测试 | `test_daily_transfer_cap_uses_shanghai_midnight_window` |
| **G11** | `evidence_offset>0` 翻页与未知类别无测试 | `test_spending_evidence_offset_pages_do_not_overlap` |
| **G15** | 升级用例在项目 `var/` 内建临时目录 | `test_upgrade_regression_uses_project_var_temp_dir` |

**关闭过程中修正了两个错误假设**（这正是"缺口清单必须被源码复核"的价值）：

1. **确认阶段的 `INSUFFICIENT_FUNDS` 是 HTTP 400，不是 409**。`operations.py` 抛该错误时没传 `http_status`，
   于是走了 `errors.py` 的默认值 400；而同期的 `INVALID_PROPOSAL_STATE` / `PROPOSAL_EXPIRED` / `STALE_RESOURCE`
   都显式传了 409。**这是一个真实的状态码不一致**，建议统一（见下方 P1）。
2. **确认阶段的 `LIMIT_EXCEEDED` 同样会返回 400**（同一个 `except` 分支），目前只有代码路径推断，未单独断言。

### 7.1 P0：仍待补（2 条）

| 编号 | 缺口 | 为什么重要 | 建议补法 |
| --- | --- | --- | --- |
| **G08** | 前端 `useTaskSync.ts` / `api.ts` 的轮询间隔、终态停止、失效读取丢弃、写不自动重发、15s/150s 超时、`session-lost` 过滤**只由 Playwright 脚本覆盖**；项目没有 JS 单元测试框架 | M6 的核心价值（同步与恢复）无法在无 Edge、无空闲端口的环境里验证，回归成本高且进不了纯 CI | 把 api/timer/document 抽象成可注入依赖，用 `node:test`（项目已有）写纯单元测试：终态后不再 `setTimeout`、写请求期间不轮询、序号丢弃过期响应、只有 `SESSION_CHANGED` 才派发 `session-lost` |
| **G17** | 前端金额解析与上限拒绝（`OperationPanel.minorFromYuan`，0.01–5,000 元、最多两位小数）只在 UI 层实现，**没有自动化测试**；后端对应规则有 pytest | 前端是用户输入的第一道闸门，规则漂移会让用户提交后才发现错误 | 把 `minorFromYuan` 抽成独立模块，用 `node:test` 覆盖 `'100.00'→10000`、`'0'/'0.00'/'-1'/'1.234'/'5001.00'` 抛错、`'5000.00'→500000` 的边界 |

### 7.2 P1：影响可维护性（5 条）

| 编号 | 缺口 | 建议 |
| --- | --- | --- |
| **G06** | 一批错误码从未被断言：`INVALID_ACCOUNT_STATE`、`INVALID_RECIPIENT_STATE`、`INVALID_TASK_STATE`、`TOOL_BUDGET_EXCEEDED` | 按错误码建一张"触发条件 → 期望码"的表，逐条补齐 |
| **G05** | `RETRYABLE_CONFLICT`（`confirm_proposal` 捕获 SQLite `OperationalError` 的转换分支）无任何用例 | 注入一次写冲突，断言返回可重试语义且**无写入** |
| **G14** | 会话 Cookie 属性（`httponly`、`samesite=lax`、`secure=False`、`path=/`、8 小时 `max_age`）无测试 | 断言 `Set-Cookie` 各项属性，防止被无意改动 |
| **G12** | `FINAGENT_ALLOWED_ORIGIN` 的允许来源分支只在浏览器脚本里隐含 | 在 pytest 里显式构造同源/跨源 Origin，断言放行与拒绝 |
| **★ 新** | **确认阶段的状态码不一致**：`INSUFFICIENT_FUNDS` 与 `LIMIT_EXCEEDED` 走默认 400，而同类拒绝（`INVALID_PROPOSAL_STATE` / `PROPOSAL_EXPIRED` / `STALE_RESOURCE`）是显式 409 | 统一为 409 并断言；或在 `API.md` 里写清"准备阶段 400 / 确认阶段 400 或 409"的规则。**这是本轮关闭 G03 时发现的新问题** |

### 7.3 P2：交付与体验（3 条）

| 编号 | 缺口 | 建议 |
| --- | --- | --- |
| **G09** | 真实模型（live）行为**完全没有自动化准确率断言** —— 所有 pytest 的 live 路径都用 `httpx.MockTransport` 或 patch `interpret`；live 只能靠 `evaluate.ps1` / `model-smoke.ps1` / `browser-*-live` | 这不是缺陷而是**成本与稳定性的取舍**；建议在报告里明确写"live 未见自动化护栏"，避免误读为已验收 |
| **G13** | 全部 UI 流程依赖本机 Edge（`FINAGENT_BROWSER_PATH` 或固定路径）与 8000/5173 空闲端口；`check.ps1` 只在 `-Stage M6/All` 才跑浏览器脚本 | 在 CI/受限环境明确降级为 `-Tier standard`，并在报告里标注"未跑 UI 层" |
| **G16** | `POST /api/tasks/{id}/reject-operation` 对非跨场景任务返回 409 `INVALID_TASK_STATE` 无覆盖 | 补一条越界调用断言 |

> **已关闭的 8 条**（G01、G02、G03、G04、G07、G10、G11、G15）见 7.0 节，实现位于
> `backend/tests/test_gap_findings.py`。

### 7.4 关于"领域层缺纯函数测试"的更正

第一次阅读文档时我以为 `money.py` / `spending.py` / `conditions.py` 的边界没有测试。**逐文件核对后确认是错的**：
`test_m2.py::test_money_and_two_clocks` 与 `test_spending_hand_calculated_refunds_and_evidence_pagination`
已经在覆盖整数分、双时钟、退款归属与手算统计。当时判定的真实缺口是 G10（日切边界）与 G11（分页边界）
——**这两条本轮已经补上并关闭**。

> 这条更正是本方案方法论的一部分：**缺口清单必须能被源码复核，不能靠印象**。

### 7.5 测试耗时：实测数据与一个值得注意的现象

**实测（可复现）**：113 项后端用例约 **23–41 秒**（在项目目录内运行时偏慢，因为 `var\` 下已有数据库与日志），
其中绝大部分是 pytest 报告的测试时间。`standard` 档位（后端 + 类型检查 + 生产构建）端到端约 **29–43 秒**。

**但第一次运行时观察到 8 分 20 秒**（约 19 倍差距），且无法复现。当时为同一命令、同一解释器、
同一份代码。现在每次都在 25 秒左右。最可能的解释是**首次运行时杀毒软件对新建的临时 SQLite
文件逐个实时扫描**——测试为每个用例创建临时库，文件访问次数极多，实时扫描会线性放大耗时。
第二次以后文件已在扫描缓存里，代价消失。

**这条现象的工程含义比数字本身更重要**：

1. 写性能基线时**必须报告第几次运行**，否则会得出错误结论（把"首次扫描"当成"测试很慢"）。
2. 如果 CI 或同事机器上首次跑很慢，**先加杀毒软件排除项**（把临时目录与 `.venv` 排除），
   再考虑优化 fixture——否则会在错误的方向上花时间。
3. `testplan.ps1` 的 `results.json` 会记录每次耗时，仪表盘显示与上一次的差值，便于识别这类变化。

**真正值得做的优化**（在排除杀毒干扰后仍有价值）：

1. **session 级空库模板**：只建一次已迁移的空 SQLite，用例用复制或 `ATTACH` 派生。
2. **只读用例共享快照**：`GET` 类断言不改变状态，可复用同一份已灌种子的库（会话隔离用例除外）。
3. **用 pytest marker 取代关键词选择**，让 `-Tier quick` 的清单不会因测试改名而漂移。
4. **加一条性能软基线**（例如 1,000 条流水下 `/api/spending` < 200 ms），防止引入 N+1 查询。

---

## 8. 明确不测的范围（避免过度声称）

依据 `ARCHITECTURE.md` 与 `EVALUATION.md` 的自我限制：

- ❌ 真实身份认证、真实支付通道、真实资金
- ❌ 生产部署、多租户、隐私合规、风控、运维
- ❌ 任意自然语言规划与开放式自由对话准确率
- ❌ 人类效率提升 / 用户完成率（自动化耗时不含人的阅读与思考）
- ❌ 跨物理机器的复现（独立副本共享同一台机器的 Python/Node/Edge 与下载缓存）
- ❌ 实际 API 费用账单（当前费用标为未知/null）

**写报告时的措辞纪律**：可以说"在已测试场景下无未确认写入、无重复业务结果、UI 与持久状态一致"；不能说"所有输入都正确"。

---

## 9. 快速排错

| 现象 | 原因 / 处理 |
|---|---|
| `请先运行 .\scripts\setup.ps1` | 缺 `.venv`；跑 `.\scripts\setup.ps1` |
| 端口 8000/5173 被占用 | 停掉之前的 dev 窗口；**不要**直接杀不确定归属的进程 |
| 浏览器流程失败 | 看 `var\screenshots\m6-failure-*.png`；确认本机 Edge 存在或设置 `FINAGENT_BROWSER_PATH` |
| 测试后行为异常 | 新开一个 PowerShell，避免遗留 `FINAGENT_*` 环境变量 |
| 中文输出乱码 | 脚本必须以**带 BOM 的 UTF-8** 保存；控制台先 `[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding` |
| 结果目录不可写 | 运行器会自动降级到 `%USERPROFILE%\finagent-testplan\`，按提示回填 |
| live 报错 | 检查 `.env` 的 `MODEL_API_KEY` 与网络；**不会**自动降级到 mock，这是有意设计 |
| **`pytest` 显示全部通过但退出码为 1** | 见下方 9.1，这是本机环境的一个真实陷阱，运行器已规避 |

### 9.1 已知陷阱：pytest "全绿但退出码 1"

**现象**：`pytest backend\tests -q` 打印 `113 passed`，但 `$LASTEXITCODE` 是 **1**，
CI 或封装脚本据此判定失败。

**原因**：pytest 默认在**共享的系统临时目录**下维护
`%TEMP%\pytest-of-<用户>\pytest-current`（一个指向最新 `pytest-N` 的重解析点/junction）。
当这个重解析点残留、或指向已被删除的目标时，pytest 在**会话收尾清理**
（`tmp_path_factory` 的 finish 钩子）阶段对它调用 `os.stat` 会抛
`PermissionError: [WinError 5]`。此时测试结果早已统计完毕，所以摘要仍显示全绿，退出码却是 1。

**本机确实处于这个状态**（`%TEMP%\pytest-of-32473\pytest-current` 是坏链接），
而且**无法在非提权会话里删除它**（`del` / `rd` / `Directory.Delete` / `attrib` 全部 Access denied）。
历史上它已经造成过一次困扰，见 `docs/evidence/review-2026-09-26/review-before-backend.log`。

**规避方式**：把 pytest 的 `--basetemp` 固定到本次运行的专属可写目录，绕开那个共享链接。
`scripts\testplan.ps1` 已经这样做（`--basetemp=<报告目录>\pytest-tmp`，并把 `TEMP` 也指过去）。

**如果你手工跑 pytest**，请加上：

```powershell
$env:TEMP = 'D:\finagent\var\pytest-tmp'; $env:TMP = $env:TEMP
.\venv\Scripts\python.exe -m pytest backend\tests -q -p no:cacheprovider --basetemp=$env:TEMP
```

**想彻底根治**（需要管理员）：删除 `%TEMP%\pytest-of-<用户>` 整个目录后再跑一次，让 pytest 重建。
这一步不是必须的 —— 加了 `--basetemp` 之后就不再受影响。
