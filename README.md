# FinAgent

P1 候选版：2026-10-15；基于原包标注的提交 `7e16cd7f8b01b21c53c14ef79209d0778a7afd0f`。源码仓库：[htyst/finagent](https://github.com/htyst/finagent)。云端运行配置已准备，实际部署状态见下文。

**当前为P1候选版，尚非赛事最终提交。** 最新范围见 [P1验收记录](docs/P1-验收记录.md)；[P0记录](docs/P0-验收记录.md)保留历史。不能把有限场景通过等同于赛事所有要求已获认可。

**前端界面已按浅色助手参考图重构。** 智能助手、我的资产、任务中心、安全与记录四个入口，模拟操作独立放在演示工具；任务恢复、安全确认和原有业务接口保留。当前改动、截图与测试边界见 [前端重设计与验收](docs/前端重设计与验收.md)。

**本轮新增统一 DeepSeek 对话入口。** Live 普通聊天先做语义理解，再结合结构化草稿、上下文和受限读取工具办理；固定确认、独立验证和后台授权继续由原后端负责。配置、实际改动、三组多轮演示和本轮验收边界见 [统一语义入口改造与验收](docs/DeepSeek-统一语义入口改造与验收.md)。下面 136 项等统计属于前一阶段 P1 验收时点。

最新完整回归136项后端测试＋4个子测试、14项前端API、完整TypeScript类型检查、Vite生产构建均通过。Edge原4组及三主场景18组页面检查通过；Mock开发集27/27，真实DeepSeek查询及理财6/6（7次请求）分别统计。真实Live小样本不代表全场景/对抗通过；完整trace、干净机器独立复现、容器、最终PPT/视频和赛事提交仍待完成。

基于本地合成数据的模拟银行项目，支持账户/账单查询、转账与AA收款、预约转账、模拟理财、卡片管理、订阅代扣和生日安排。Mock使用本地规则；Live调用语言模型。资金、卡片审批、采购和送达均为模拟。

## 给其他人使用 / 云端部署

- **别人本地运行**：下载或clone仓库后按下方安装说明操作；Windows原生脚本，或已安装Docker的人运行 `docker compose up --build -d` 后访问 http://127.0.0.1:8080（Mock）。源码仓库不包含本地exe，启动器可用 `scripts/build-launcher.ps1` 自行构建。
- **打开网址使用**：已准备 `Dockerfile.cloud`、`render.yaml` 和云网关，可交给云托管平台在云端构建运行；作者电脑无需持续开机。尚未创建云服务或发布网址，不能把部署配置存在称为部署成功。
- 云模板默认Mock、访问密码保护、单实例和1GB持久磁盘。开启Live只在云平台填写自己的DeepSeek密钥；不会把本机密钥上传GitHub。
- 具体步骤、付费确认、模拟认证边界和当前验证状态见 [GitHub与云部署使用说明](docs/GitHub与云部署使用说明.md)。

## 首次运行

环境：Windows；Python3.12—3.14（推荐3.12）、Node.js24/npm11。首次安装依赖需要网络。

解压后进入 `finagent` 文件夹，打开PowerShell，依次运行：

```powershell
.\scripts\setup.ps1
.\scripts\reset-demo.ps1 -Date 2026-09-24
.\scripts\dev.ps1 -Mode mock
```

打开 <http://127.0.0.1:5173>，选择演示用户。
若setup找不到Python，可指定已安装的解释器：

```powershell
.\scripts\setup.ps1 -Python 'C:\实际安装路径\python.exe'
```

若没有全局Node，可先运行 `.\scripts\setup-node.ps1`，从官方站下载并校验项目内便携Node 24，同时按锁文件安装前端依赖。setup、dev和验收脚本会自动寻找该环境，不修改系统PATH。本机本轮已安装到 `var/tools/node-v24.22.0-win-x64/`，项目Python环境为 `.venv/`。

## 启动器

完成首次安装和演示数据初始化后，双击 `FinAgent.exe` 选择模式，或直接启动 `FinAgent-Mock.exe` / `FinAgent-Live.exe`。关闭启动器会停止它启动的服务；通过PowerShell启动时按Ctrl+C停止。

Live模式：仅在本地 `.env` 不存在时，从 `.env.example` 复制一份为 `.env`，填写自己的 `MODEL_API_KEY`。也可使用：

```powershell
.\scripts\dev.ps1 -Mode live
```

DeepSeek 接入配置示例（密钥仅填在本地 `.env`）：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-flash
MODEL_BASE_URL=https://api.deepseek.com
MODEL_API_KEY=在本地填写
```

`dev.ps1 -Mode live` 显式选择 Live，优先于 `.env` 中的默认模式。修改密钥或模型后须停止服务再启动；两个模式使用同一本地模拟数据库。若 PowerShell 阻止脚本，可在项目目录使用进程级命令 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Mode live`，不修改系统执行策略。

启动后打开 <http://127.0.0.1:5173>，选择用户，在“演示工具 → 运行状态”点击“检查 DeepSeek 连接”。该按钮发送一次简短真实请求，可能产生少量模型用量；检查成功再发送“看看我的余额”或“从活期账户转100元到储蓄账户”。模型理解交给现有业务服务查询或准备，操作仍须页面确认，红色操作仍须独立验证。订阅查询仍须先在订阅面板确认。旧任务保留其创建时的模式；切换模式后取消未完成旧任务，再发起新任务。

连接失败时会分别提示密钥无效、账户余额不足、限流、模型配置或网络问题。`.env` 已加入忽略规则，密钥不会返回前端；勿将密钥粘贴到对话框或公开仓库。完整改动与验证边界见 [DeepSeek接入与本地Live验收](docs/DeepSeek-接入与本地Live验收.md)。

## 数据与运行

演示数据库启动后保存在 `var/finagent.sqlite3`。首次运行reset建立数据，已有数据以后直接启动；重复reset会清空演示数据。正常启动自动进行数据库迁移，当前迁移head为 `0013_security`。新增安全状态、挑战、授权和任务检查点表；旧库升级须先备份并完成迁移回归，不能为了升级重置已有数据。

页面写操作需核对固定单并点击对应确认按钮。预约、订阅提醒和生日后台安排需要电脑及服务保持运行，关闭网页不影响后台任务。8000/5173端口被占用时，先停止之前的本项目服务。日志位于运行时生成的 `var/logs/`。

## 项目文件

- `backend/`：后端业务代码、数据库迁移、合成数据初始化及Python依赖配置。
- `frontend/`：前端源码、页面入口、构建配置及npm依赖锁文件。
- `scripts/`：安装、环境检查、数据初始化、服务启动及启动器编译代码。
- 根目录：部署配置、`.env.example`和本README；EXE启动器由源码自行编译，不随仓库分发。

本包不携带本机密钥、已安装依赖或运行数据库。依赖由setup安装，数据由首次reset创建。

## P0 安全操作与验收

- 转账按本人跨账户上海自然日累计含本笔分级：不超过1000元为黄色确认，超过1000元为红色独立验证。理财申购、挂失固定红色，解冻和放宽限制也按红色处理。
- 红色确认弹窗需要先获取模拟短信，在项目目录另一终端运行 `.\scripts\auth-code.ps1`，再把六位码填入弹窗。不得填入对话框。演示选用户与本机收件箱不是正式银行登录/MFA，赛方接受程度待确认。
- 订阅查询须点击专项查询确认；后台代扣还须核对商户、账户、金额、周期后单独授权。旧预约和生日任务如果缺少新授权契约会受控停止，需要重新办理。
- 对话面板提供暂停/恢复/转人工。模拟坐席凭据通过 `.\scripts\operator-key.ps1` 本机生成/读取；坐席只能建议、终止和恢复检查点，不能代替用户授权。
- 虚拟卡申请经用户确认后只到受理；独立模拟审批员通过 `.\scripts\approver-key.ps1` 取得另一份本机凭据，在卡管独立审批区处理。坐席凭据不能审批。这是本地模拟角色隔离，不是真实银行身份认证。
- 认证失败5次锁定15分钟；到期重新核验，不自动重放失败支付。具体边界见 [权限与安全设计](docs/P0-安全与权限设计.md) 和 [规划与恢复](docs/P0-规划与恢复.md)。

安装依赖后，从项目目录运行完整验收（只使用隔离测试库）：

```powershell
.\scripts\test-p0.ps1
```

脚本依次执行后端pytest、前端API回归、TypeScript类型检查、生产构建及浏览器测试，证据保存到 `var/TEST-ONLY/p0-*/`。浏览器测试使用本机Edge和隔离端口18000/15173，拒绝操作正常演示库。`-SkipBrowser` 只能用于部分检查，不能视作全量通过。Windows若禁止运行脚本，应按本机管理策略处理；验收脚本不会修改执行策略或自动安装依赖。

实际启动后端并验证绿色查询、黄色确认、红色模拟认证、幂等和锁定/撤销，可单独执行 `.venv\Scripts\python.exe scripts/smoke-backend.py`。脚本自建TEST-ONLY数据库、使用空闲本机端口并关闭自身服务；报告和日志保留在 `var/TEST-ONLY/server-smoke-*/`。它不替代浏览器测试。

本轮隔离验收入口为 `.venv\Scripts\python.exe scripts/run-browser-tests.py`，只创建TEST-ONLY数据库和临时端口，结束关闭自身服务；需要本机Edge和项目便携Node。对话评测入口为 `.venv\Scripts\python.exe scripts/evaluate-dialogue.py --mode mock`；使用 `--mode live --limit 3` 会发送3条合成查询并消耗实际模型用量，不确认金融写入。开发/保留样例目前为待团队人工复核草案。

算法、接口/数据状态、隐私与停机策略、安全自评、部署恢复及答辩稿见 `docs/P1-*.md`。用户已选择统一 DeepSeek 入口并结合工具和本地知识检索，原 [A/B/C方案](docs/对话智能提升方案-待选择.md) 作为选择前历史保留；实际实现见新的统一入口验收文档。

不依赖第三方Python包的基础检查可运行：

```powershell
python -m unittest discover -s backend/tests -p '*_stdlib.py' -v
python scripts/check-static.py
```

`compose.yaml` 是尚未实跑的Mock隔离部署候选配置（`docker compose up --build`），有只读根文件系统、非root、资源上限和禁止外网出口。模拟坐席接口只接受回环地址；容器反向代理部署尚不提供坐席接管演示，请使用本机Windows流程验收该功能。该容器配置不用于Live模型联网。

生成带逐文件SHA-256清单的候选源码包：`python scripts/package-source.py`。源码包排除依赖、数据库、运行日志、认证收件箱、真实`.env`及二进制启动器；可按 `scripts/build-launcher.ps1` 重新编译启动器。最终交付状态须以全量验收为准。
