> 更新：当前安装以根目录 README 为准（年度数据用 import-financial-demo.ps1，迁移 head 为 0018_directory）。以下保留 2026-10-16 云部署准备及历史验证；后续合并结果见 [上传与 Actions 验收](GitHub-上传与Actions验收-20261018.md)。

# GitHub 与云端使用

准备日期：2026-10-16。源码仓库为 [htyst/finagent](https://github.com/htyst/finagent)，公开仓库由所有者指定。本次准备源码发布和云部署，未创建付费服务；没有在用户电脑安装 Docker、后台托管服务或内网穿透软件。真实上线仍需云平台账号。

## 两种使用方式

| 使用者 | 运行位置 | 是否需要作者电脑开机 |
|---|---|---|
| 下载源码，在自己电脑运行 | 使用者自己的电脑 | 不需要 |
| 打开部署后的 HTTPS 网站 | 云托管平台；浏览器只加载页面 | 不需要 |

GitHub 保存和分发源码，不负责运行 Python API。[GitHub Pages 不支持 Python 等服务端语言](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)。不应只上传 frontend/dist 后声称整个项目已上线。

项目根目录是 `finagent` 文件夹内部，GitHub 仓库根目录应直接出现 README.md、backend、frontend、render.yaml，而非再嵌套一层 finagent。真实 `.env`、密钥、SQLite 数据、依赖、约1GB历史测试产物与本机 exe 均被 Git 忽略。源码中的 references 本机参考译文/提示词不参与发布；原文件保留。

## 他人下载运行

### Windows 原生方式

安装 Python 3.12—3.14（建议3.13）、Node.js24，下载仓库 ZIP 解压或 clone，进入包含 README.md 的目录：

```powershell
git clone https://github.com/htyst/finagent.git
cd finagent
```

```powershell
.\scripts\setup.ps1
# 仅首次下载、没有任何需要保留的数据时初始化
.\scripts\reset-demo.ps1 -Date 2026-09-24
.\scripts\dev.ps1 -Mode mock
```

打开 http://127.0.0.1:5173。已有数据库直接启动，不重置。源码仓库不携带编译 exe；需要启动器时运行 `scripts/build-launcher.ps1`，或直接用上述脚本。

Live：从 `.env.example` 复制为 `.env`，由使用者填写自己的 MODEL_API_KEY，再用 `dev.ps1 -Mode live`。作者电脑的模型密钥不会随仓库分发，也不会自动供下载者使用。

### 已有 Docker 的 Windows / macOS / Linux

```sh
docker compose up --build -d
```

打开 http://127.0.0.1:8080，默认 Mock。本配置维持内部隔离网络，不调用 DeepSeek。停止用 `docker compose down`；不要加 `-v`，否则会删除持久数据卷。作者电脑不需要安装 Docker，只有选择这种运行方式的人需要安装。

### macOS / Linux 原生 Live

在新 clone 的根目录安装 Python3.13、Node24 后：

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r backend/requirements.lock
python -m pip install --no-deps -e backend
cd frontend
npm ci
cd ..
# 只在新 clone 且没有旧数据时执行
python -m app.data.cli reset --date 2026-09-24
```

仅在 `.env` 不存在时复制 `.env.example` 为 `.env` 并填写自己的密钥。一个终端 `python -m app.run --mode live`，另一个终端进入 frontend 运行 `npm run dev`；访问 http://127.0.0.1:5173。两个终端 Ctrl+C 停止。源码路径按目录定位，不依赖作者的 D 盘或 Codex 安装路径。

## 省事的云部署准备：Render 单个 Docker 服务

已经准备 `Dockerfile.cloud` 与 `render.yaml`。这是受访问保护的合成数据演示站，不是真实银行、正式注册登录或每个访客独享账户的数据产品。

该方案在云端进行 Node 构建，运行时只保留 Python＋前端静态资源，由同一 HTTPS 网址提供。后台预约、订阅提醒与生日任务在这个云实例中运行；作者电脑关机不影响它。

1. 注册 [Render](https://dashboard.render.com/)，用 GitHub 登录并允许访问目标仓库。
2. New → Blueprint，选择仓库并读取仓库根目录 `render.yaml`。
3. **先检查套餐与收费页面，不要未经确认创建付费服务。** 模板为 `0.5c-512mb` 单实例＋1GB持久磁盘，未加入付费工作区、额外数据库或自定义域名。实例、磁盘、出站流量等按平台当前规则计费；DeepSeek API 用量另算。本次没有开通收费服务。
4. 费用确认后才创建。首次默认 Mock，不需要模型密钥。平台提供 `https://服务名.onrender.com`，无需先购买域名。
5. 在服务 Environment 查看平台生成的 FINAGENT_ACCESS_PASSWORD，用户名 guest。把网址和演示访问凭据交给需要体验的人；凭据不要放进 GitHub、URL 或前端。
6. 默认关闭自动部署，先等待 GitHub Actions 通过，再手动部署确认版本。平台区域和境外访问质量需要实际验证。

持久磁盘挂载 `/app/var`，保留银行数据库、后台任务、模拟认证文件和云端额度计数。第一次空磁盘初始化合成数据；后续只迁移，不重置。镜像入口只调整挂载目录属主，随后降到 UID/GID10001运行。

[Render 持久磁盘需要付费实例](https://render.com/docs/disks)。[免费实例的本地文件会在重启、部署或休眠后丢失，且不支持持久磁盘](https://render.com/docs/free)，因此不把它当作本项目稳定托管路径。模板规格按 [当前 Blueprint 文档](https://render.com/docs/blueprint-spec) 编写，具体金额以 [平台收费页面](https://render.com/pricing) 和创建页为准，本次未获取个人账户报价。

## 云端开启 DeepSeek

先确认 Mock 页面、数据持久化和访问保护正常。随后在 Render 的 Environment 配置：

```dotenv
FINAGENT_MODE=live
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-flash
MODEL_BASE_URL=https://api.deepseek.com
MODEL_API_KEY=只在云平台秘密配置中填写
```

其他环境变量由模板准备：`FINAGENT_ACCESS_USER=guest`、生成的访问密码、每分钟6次/每天100次新对话或探测请求。额度是**站点全局已接受的聊天/探测HTTP请求数**，不是金额、token或实际模型请求数；每条仍受原每轮模型调用上限约束。日期以UTC计数，**不改变银行安全日累计的上海时间口径**。额度持久化、同实例一次只处理一个模型对话；用尽时原任务查询、取消及固定单确认仍由原接口办理。

平台自动提供 `RENDER_EXTERNAL_URL` 和 `PORT`。自定义域名时将 FINAGENT_ALLOWED_ORIGIN 设置为实际 HTTPS origin（不能带业务路径）。云入口启用 Secure 会话 Cookie；本地 HTTP 保持原行为。密钥不进镜像、Git、前端或日志。

## 独立验证、审批、备份

站点访问密码只限制演示站访问，不替代黄色确认、红色强验证、模拟审批或坐席凭据。访客选择的是共享的合成演示用户；不能据此宣称真实多租户身份隔离。不要输入真实银行卡、身份证或家庭地址。

原红色验证仍通过实例所有者在 Render Shell 读取：

```sh
python scripts/local-auth.py sms --challenge 实际挑战编号
python scripts/local-auth.py operator
python scripts/cloud-approver.py
```

验证码和角色凭据不开放公开下载/API，不自动替访客验证。远程访客没有真实短信接入，不能独立完成所有红色业务；完整真实身份认证是后续工作。

实例所有者在线备份：`python scripts/backup-database.py`，生成 `/app/var/backups` 下的 SQLite 在线备份。需定期下载到实例之外保存；同一磁盘内备份不等于异地备份。不要对运行中的SQLite仅复制主文件；不要为了升级执行reset。

## 检验与尚未完成

新增8项云网关检查与原4项HTTP安全回归共12项通过（14.33秒，2条既有配置/依赖告警），见 [JUnit](evidence/cloud-gateway.xml)。检查访问失败关闭、健康探测例外、API路径保持、静态路由、额度重启保留、UTC换日、缺密码/缺构建拒绝启动，并在实际银行API验证 Secure Cookie、CSRF、订阅黄色要求、大额红色规则及未验证不能执行。

已初始化本地Git，发布范围经过 [扫描与准备记录](evidence/cloud-preparation.json) 核对。扫描对历史测试中的两种无效测试凭据仅按原文件路径和精确SHA-256放行，其他位置或新值仍拒绝；模式扫描不证明任意秘密绝对不存在。未读取真实`.env`，未提交密钥、数据库、依赖或exe。

`.github/workflows/ci.yml` 在 GitHub 云端运行源码扫描、后端完整回归、前端测试构建、Docker云镜像构建和健康/访问保护/卷重启检查。三个作业分别运行，后端失败仍保留红色整体状态，不阻止收集前端与容器的独立结果。它不申请云服务，也不自动花费 DeepSeek 额度。

2026-10-16已上传403个文件至main，逐文件Git blob及完整文件树与本地发布提交一致。首次Linux完整回归为293项通过、33项失败、48项准备错误、4个子测试通过（73.38秒）。其中出现旧迁移head断言、旧权限/模型替身契约及规划器测试与主入口不匹配；不能仅据分类断言这些都没有业务影响，需逐项修复验收。原安全测试保留，未跳过或改为允许失败。详见 [首次发布记录](evidence/github-publication.json) 和对应 Actions 日志。全量未通过前，不执行网站正式部署。

当前电脑没有Docker，本次未安装。在 [第二次GitHub检查](https://github.com/htyst/finagent/actions/runs/38854342239) 中，14项前端API测试、完整类型检查、生产构建、Linux云镜像构建、空磁盘启动、未授权401/授权页面访问及重启后数据卷标记保留均已通过。该结果来自GitHub云端，未在用户电脑启动服务。完整后端失败仍保留，不能将镜像检查通过称作所有业务通过。Render首次部署、平台磁盘权限、外网HTTPS/浏览器访问、云端真实DeepSeek和云端红色完整流程仍未实测；本轮未重跑浏览器或真实模型测试。

GitHub源码入口为 https://github.com/htyst/finagent；自动检查结果在仓库 Actions 页面查看，不能以本地旧报告替代新提交的结果。云平台账号和实际收费确认待用户完成。项目尚未选择发布许可证，公开源码不自动等于授予任意复制/再分发许可；由所有者决定许可证。
