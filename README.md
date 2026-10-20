# FinAgent-03

**当前使用版本：FinAgent-03；默认分支：`main`。** 直接克隆本仓库或在 GitHub 选择 `main` → **Code → Download ZIP**，获取的就是当前版本，无需另外查找 `finagent-03` 文件夹或切换分支。

本仓库根目录只有一套应用源码：`backend/`、`frontend/` 和 `scripts/`。历史提交、旧测试分支及历史验收文档用于追溯，不会作为另一套应用被默认启动。`datasets/demo_financial_v2/` 是当前应用需要的合成数据集，名称中的 `v2` 不代表旧版项目。

一个使用合成数据的 AI 银行助手，支持账户与账单查询、转账、模拟理财、卡片、订阅代扣和生日安排。Live 使用 DeepSeek 动态工具循环，并记录目标、类型化执行 DAG、检查点和回执；查询、权限、确认、模拟强验证和提交由服务端控制。

本教程面向**从 GitHub 全新拉取源码的人**。不需要作者电脑上的数据库、密钥、虚拟环境或 EXE。银行资金、审批、短信和采购均为本地模拟；只有 Live 的模型调用是真实 API。

本版发布与当前验证范围见 [FinAgent-03 发布记录](docs/FinAgent03-发布与验收-20261020.md)。

## 1. 准备环境

已验证的本地入口：Windows x64、PowerShell、Python 3.12—3.14（本次使用 3.13）、Node.js 24。首次安装需要连接 Python 包源、npm 和 Node 下载站。

- 安装 Git 和受支持的 Python，安装时启用 Python Launcher，或准备好 Python 解释器的实际路径。
- 没有 Node 时，可使用下面的项目内安装脚本；不要求全局安装 Node。
- GitHub 的 Linux 环境已通过完整后端、前端和 `Dockerfile.cloud` 启动验证；macOS、Linux 原生完整浏览器安装与 `compose.yaml` 仍未逐项验收，不代表 Live 公网部署已经上线。

## 2. 拉取源码并找到应用根目录

```powershell
git clone --branch main --single-branch https://github.com/htyst/finagent.git
cd finagent
```

执行上面的克隆命令后，`finagent` 就是当前 FinAgent-03 的应用根目录，后续命令均在这里执行；不需要进入另一个版本目录。下载 ZIP 时，解压进入 `finagent-main`，确认其中同时包含 `backend`、`frontend`、`scripts`、`datasets`。

```text
backend/                       后端、领域服务、数据库迁移与测试
frontend/                      React 页面与 npm 锁文件
scripts/                       安装、导入、启动和验证脚本
datasets/demo_financial_v2/     可重新生成演示库的合成 CSV 与校验清单
docs/                          架构、开发记录、验收证据和已知限制
.env.example                   不含密钥的配置模板
```

## 3. 首次安装并运行 Mock

在应用目录打开 PowerShell：

```powershell
# 已有 Node 24 时可跳过这一行；下载会校验官方 SHA-256。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-node.ps1

# 创建项目自己的 .venv，按锁文件安装 Python/npm 依赖。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1

# 只在首次运行、没有演示库时导入年度合成数据。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\import-financial-demo.ps1

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Mode mock
```

打开 **http://127.0.0.1:5173**，选择演示用户“林晓”。后端地址为 http://127.0.0.1:8000；`/api/health` 和 `/api/demo/info` 可用于检查服务。

这些命令只为各自进程指定执行策略，不修改系统执行策略。若组织策略禁止执行脚本，应遵循本机管理要求。

如果找不到 Python，指定已安装解释器，而不是复制作者的 `.venv`：

```powershell
.\scripts\setup.ps1 -Python 'C:\你的Python安装目录\python.exe'
```

初始数据为 20 名用户、25,471 笔现金/账户流水及单独的信用卡负债事件，观察期为 **2025-09-25 至 2026-09-24**。首尾自然月不完整，快照不能当作今天的实时行情。详细口径见 [数据集说明](datasets/demo_financial_v2/README.md)。

## 4. 接入自己的 DeepSeek，运行 Live

先完成上述安装和首次导入，再停止 Mock（原终端按 `Ctrl+C`）。

仅在 `.env` 不存在时创建本地配置：

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
notepad .env
```

在编辑器中填写自己的密钥并保存：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-flash
MODEL_BASE_URL=https://api.deepseek.com
MODEL_API_KEY=在本地编辑器填写自己的密钥
FINAGENT_MODE=mock
FINAGENT_AGENT_ENGINE=dynamic
```

`MODEL_NAME` 需为自己的 API 账户可用模型；以上是本项目当前验证配置。不要把密钥发到聊天框、前端或提交到 Git。`.env.example` 不包含可用密钥，拉取者必须自行配置；Live 调用产生的用量由该 API 账户承担。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Mode live
```

`-Mode live` 优先于 `.env` 中的默认模式。修改配置后重启。在页面“演示工具”中的模型连接检查入口检查连接，再开始对话。模型连接失败会明确报错，不静默切成 Mock。

Mock 使用本地规则，适合先验证安装与界面；Live 才使用 DeepSeek 的动态工具选择。两种模式使用同一份本地模拟数据库；旧任务保留创建时模式，切换模式后点击“新对话”。

## 5. 试用、确认与停止

选“林晓”后可以尝试：

1. “查看我的账户余额。”
2. “分析 2026 年 8 月的账单。”
3. “从活期账户转 100 元到储蓄账户。”核对固定单，再使用页面按钮确认。
4. Live 中继续追问、修改预算或查询回执，观察目标与实际工具记录；点击“新对话”另开任务。

聊天中的“确认”“同意”不授权付款。黄色操作需明确页面确认；红色操作需独立模拟验证。收到页面挑战后，在**另一个终端**运行：

```powershell
.\scripts\auth-code.ps1
```

只把本机模拟收件箱显示的验证码输入安全弹窗。不要输入对话框；它不是真实银行短信认证。订阅查询需要黄色授权，解除代扣另需固定单；查询确认不包含取消授权。

在启动服务的 PowerShell 中按 **Ctrl+C** 停止该脚本启动的前后端。预约、提醒和生日任务依赖后端持续运行；关网页不会停止后端，停止后端后不会按时执行。日常再次运行只需：

```powershell
.\scripts\dev.ps1 -Mode mock
# 或配置完成后：
.\scripts\dev.ps1 -Mode live
```

仓库保留三个 Windows 启动器，均使用所在目录的同一套当前源码，**不代表三个项目版本**。源码运行不要求 EXE；推荐使用上面的 `dev.ps1 -Mode mock` 或 `dev.ps1 -Mode live`。需要本地 Windows 图形启动器时，可在安装后运行 `.\scripts\build-launcher.ps1` 编译，再双击生成的 `FinAgent.exe`。

## 6. 数据保存、升级与常见问题

| 情况 | 处理 |
| --- | --- |
| 8000 或 5173 已占用 | 先在原 FinAgent 启动终端按 Ctrl+C；不要结束来源不明的进程，也不要同时启动两套模式。 |
| 未找到演示数据库 | 完成 setup 后首次运行 import-financial-demo.ps1；缺少 datasets 时重新拉取完整源码。 |
| 依赖缺失或复制来的 .venv 不可用 | 在本机运行 setup.ps1；不要提交或复用另一台电脑的虚拟环境/node_modules。 |
| Node 未找到或版本不符 | 运行 setup-node.ps1，再运行 setup.ps1。 |
| Live 密钥/网络/模型出错 | 检查本地 .env、API 账户和网络，重启后检查连接；Mock 可用于独立检查安装。 |
| 导入时 CSV 校验不符 | 完整拉取 datasets；不要用表格编辑器改写或重新保存 CSV。仓库 .gitattributes 保留原始字节。 |
| 无法判断账单趋势或旅行费用 | 查看数据覆盖范围；未知成本需要本人补充，不按零处理，不虚构报价。 |
| 启动后退出 | 查看 var/logs/backend.err.log、frontend.err.log 及对应 out.log。分享日志前移除个人信息。 |

运行数据库保存在 `var/finagent.sqlite3`，密钥保存在本机 `.env`。正常启动只升级数据库结构，不清空数据；当前迁移 head 为 **0019_workflow_orchestration**。升级前应停止服务并用 SQLite backup API 备份数据库。

**不要把重新导入当作日常启动命令。** 重新导入会先备份再替换演示库；旧 `reset-demo.ps1` 会恢复三人样本并清空后续业务记录，已有数据时不要运行。

## 7. 测试与当前边界

基础检查不调用真实模型：

```powershell
.\.venv\Scripts\python.exe -B scripts/check-github-readiness.py
.\.venv\Scripts\python.exe -B scripts/check-static.py
.\.venv\Scripts\python.exe -B scripts/check-current-frontend.py
```

后端完整回归使用隔离数据库和模拟资金：

```powershell
.\.venv\Scripts\python.exe -B scripts/test-current-regression.py
```

完整回归会从随仓库 CSV 自动创建 TEST-ONLY 年度基线，不读取或重置运行库；也可先单独执行 `python scripts/prepare-test-data.py`，为独立测试或评测准备数据。首次检出验证方式和限制见 [GitHub 分发验证记录](docs/GitHub-分发与首次运行验收-20261018.md)。历史测试结果不代表新机器已通过，完整回归通常需要十几分钟。

真实 DeepSeek 评测会消耗模型用量；参见 [DAG 与恢复](docs/G01-持久检查点与局部重规划.md)。已有动态 DAG 为有界串行执行，支持局部失效与恢复。本版继续修复跨轮状态、取消和恢复行为；真实模型及复杂条件规划仍需按当前用例验证，不能以替身回归代替真实 API 或全场景验收。

本地开发服务器仅绑定回环地址。GitHub 保存源码；若希望其他人不安装、直接在浏览器打开，需要另行部署后端、数据库和前端。上传 GitHub 不会自动变成可公开访问的完整银行助手。

## 8. GitHub Actions、云部署与分发

[查看 Actions](https://github.com/htyst/finagent/actions)：每次提交运行完整后端回归、前端测试／类型检查／构建，以及云镜像访问保护、健康检查、卷重启验证。检查不使用真实 API Key，不消耗 DeepSeek 用量。绿色 CI 证明列出的检查通过；公网网站还需要部署到云托管账号。

现有 `Dockerfile.cloud`、`render.yaml` 和 [云部署教程](docs/GitHub与云部署使用说明.md) 保留；访问密码、API Key 均只设置在云平台，运行库使用持久卷。Render 配置会产生云托管费用，应用不会自动创建付费服务。使用者在云端部署后，不需要作者电脑持续运行。

本次 FinAgent-03 发布与验证见 [发布与 Actions 验收](docs/FinAgent03-发布与验收-20261020.md)；[2026-10-18 上传记录](docs/GitHub-上传与Actions验收-20261018.md)仅保留作历史参考。

建议以本目录作为仓库应用根目录，保留 README、backend、frontend、scripts、datasets、docs、依赖锁文件和迁移。 `.gitignore` 排除本机 `.env`、运行数据库、认证收件箱、依赖目录、日志、缓存和其他生成物；原仓库三份指定启动器作为明确例外保留。不要强制添加私密或运行文件。

发布前运行 `check-github-readiness.py`，再用 `git status`／`git diff --cached` 核对真实待提交内容。若已有远端历史，先克隆远端到新目录再合并本次源码，保留原分支与历史，不使用强推覆盖。

生成可核对 SHA-256 的源码包：

```powershell
.\.venv\Scripts\python.exe -B scripts/package-source.py --name FinAgent03-source-20261020.zip
```

输出到 `var/artifacts/`，包含可重新生成演示库的合成数据，不包含作者密钥、已安装依赖或运行数据库。

## 9. 版本与历史记录

`main` 是当前发布入口。`fix/*`、`publish/*` 是历史修复或发布检查分支，普通使用者无需切换。Git 默认克隆 `main` 时不会把它们作为额外的项目目录检出；上面的 `--single-branch` 命令进一步只拉取 `main` 分支。

已有克隆且工作区没有本地修改时，可执行 `git switch main`、`git pull --ff-only origin main` 获取当前发布内容。已有业务数据时先备份，再按本文正常启动完成迁移；已有本地源码修改时先保存修改并检查差异，不要直接重置或覆盖。

历史验收文档与旧迁移保留，以便核对升级和恢复；安装、启动及部署统一以本 README 为准。

**FinAgent-03 问答修复（2026-10-19）**：修复“取消最贵订阅”对象解析、聊天确认误取消目标、待确认单据失效及查询冒充办理完成等问题；本次发布的回归结果、范围和仍存限制见 [测试修复记录](docs/FinAgent03-发布与验收-20261020.md)。

**DAG问题修复（2026-10-19）**：修复会话外键、状态与恢复、条件分支和参数失效问题；支持等待确认时继续已规划的独立查询，新增自然语言只读复查草稿及页面确认入口。修复范围、回归证据和仍存在的能力边界见 [修复记录](docs/FinAgent03-发布与验收-20261020.md)。

**跨场景联动增量（2026-10-19）**：任务中心新增联动计划、真实目标历史、明确授权的只读资金复查，以及回执后持久续跑队列。生日作为生活安排的一种保留，使用统一调度入口。迁移、授权边界和验证方式见 [改进说明](docs/FinAgent03-发布与验收-20261020.md)。
