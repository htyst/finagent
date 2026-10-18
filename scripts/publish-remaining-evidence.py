"""Publish current checked evidence; never fabricate missing reports or touch runtime data."""
import hashlib,json,os,shutil,sqlite3,sys,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/os.getenv('FINAGENT_REVIEW_DIR','var/reviews/remaining-20261018/release');DOC=ROOT/'docs';E=DOC/'evidence'
def read(name):
 p=OUT/name
 if not p.exists():raise SystemExit('Evidence pending: '+name)
 return json.loads(p.read_text(encoding='utf-8'))
backend=read('backend-summary.json');manifest=read('backend-tested-source.json')
if not backend['passed'] or backend['failures'] or backend['errors'] or backend['skipped']:raise SystemExit('Complete backend regression is not clean')
if any(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=v for n,v in manifest.items()):raise SystemExit('Source changed after tests')
front=read('frontend.json');browser=read('browser.json');live=read('live.json');recovery=read('live-recovery.json');scale=read('d01.json');coverage=read('d01-correctness.json');writers=read('d01-writers.json')
frontend_manifest=read('frontend-source.json')
if any(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()!=sha for n,sha in frontend_manifest.items()):raise SystemExit('Frontend source changed after tests')
app_manifest={n:sha for n,sha in manifest.items() if n.startswith('backend/app/')}
if scale['source_version']!=hashlib.sha256(json.dumps(app_manifest,sort_keys=True).encode()).hexdigest():raise SystemExit('Scale benchmark used different application source')
if not front['passed'] or not browser['passed'] or not browser['services_stopped'] or not scale['passed'] or not coverage['passed'] or not writers['passed']:raise SystemExit('Required deterministic/browser/data checks did not pass')
for report in (browser,live,recovery,writers):
 if report['source_version']!=backend['source_version']:raise SystemExit('Mismatched source version')
# Every model run is reported, including any failed semantic sample. Financial
# authorization safety is never averaged into a language-quality success rate.
if any(not i.get('checks',{}).get('no_unauthorized_write',False) for i in live['results']):raise SystemExit('Live unauthorized-write assertion failed')
if any(not t['checks']['no_write'] for t in recovery['turns']):raise SystemExit('Live consultation wrote money')
path=ROOT/'var/finagent.sqlite3'
with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
 current=dict(transactions=db.execute('select count(*) from transactions').fetchone()[0],migration=db.execute('select version_num from alembic_version').fetchone()[0],sha256=hashlib.sha256(path.read_bytes()).hexdigest())
before=json.loads((OUT.parent/'before.json').read_text(encoding='utf-8'))['runtime_database']
if current!=before:raise SystemExit('Runtime database changed: review before publishing')
E.mkdir(parents=True,exist_ok=True)
for filename,name in [('backend-tested-source.json','remaining-backend-source.json'),('backend-summary.json','remaining-backend-summary.json'),('frontend.json','remaining-frontend.json'),('frontend-source.json','remaining-frontend-source.json'),('live.json','remaining-live.json'),('live-recovery.json','remaining-live-recovery.json'),('d01.json','remaining-d01.json'),('d01-correctness.json','remaining-d01-correctness.json'),('d01-writers.json','remaining-d01-writers.json')]:shutil.copy2(OUT/filename,E/name)
shutil.copy2(OUT/'backend-full.xml',E/'remaining-regression.xml')
shutil.copy2(OUT/'backend-full.log',E/'remaining-regression.log')
if (OUT/'d01-during-regression.json').exists():shutil.copy2(OUT/'d01-during-regression.json',E/'remaining-d01-during-regression.json')
browser_run=Path(browser['output']);browser_details=json.loads((browser_run/'report.json').read_text(encoding='utf-8'))
(E/'remaining-browser.json').write_text(json.dumps(dict(browser,details=browser_details),ensure_ascii=False,indent=2),encoding='utf-8')
for name in ('multiple-desktop.png','multiple-mobile.png','receipt-desktop.png','multiple-forms-mobile.png','long-response-mobile.png','ledger-list-mobile.png'):shutil.copy2(browser_run/name,E/('remaining-'+name))
for name in ('transfer','bills','wealth','cards','subscriptions','birthday'):
 trace=OUT.parent/'contest'/(name+'.json')
 if not trace.exists() or trace.stat().st_mtime<(OUT/'backend-tested-source.json').stat().st_mtime:raise SystemExit('Six-scene trace is stale: '+name)
 d=json.loads(trace.read_text(encoding='utf-8'));d['source_version']=backend['source_version'];(E/('remaining-scene-'+name+'.json')).write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
trace=OUT.parent/'complete-recovery-trace.json'
if not trace.exists() or trace.stat().st_mtime<(OUT/'backend-tested-source.json').stat().st_mtime:raise SystemExit('Combined recovery trace is stale')
shutil.copy2(trace,E/'remaining-complete-recovery.json')
project_files=[*sorted((ROOT/'backend/app').rglob('*.py')),*sorted((ROOT/'backend/tests').glob('test_*.py')),*sorted((ROOT/'backend/evals').glob('*.py')),*sorted((ROOT/'frontend/src').rglob('*')),*sorted((ROOT/'scripts').glob('*.py'))]
project={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in project_files if p.is_file()};(E/'remaining-project-source.json').write_text(json.dumps(project,indent=2),encoding='utf-8')
cases=list(ET.parse(OUT/'backend-full.xml').getroot().iter('testcase'));s01=sum('test_s01_authorization' in c.get('classname','') for c in cases);g2=sum('test_g01_recovery' in c.get('classname','') for c in cases)
safety=[
 ('S01-01','聊天同意不能替代固定单',['test_chat_agreement_is_not_fixed_form_authorization']),
 ('S01-02','订阅黄色查询与取消授权隔离',['test_subscription_query_grant_does_not_authorize_cancellation']),
 ('S01-03','关键条款更改使旧单/凭据失效',['test_modification_revokes_old_form_token_and_proof','test_plan_version_change_revokes_action_and_independent_proof']),
 ('S01-04','本人跨账户、北京时间自然日、含本笔阈值',['test_beijing_midnight_and_inclusive_threshold','test_preissued_yellow_tokens_recheck_after_first_commit']),
 ('S01-05','600/500 预发黄色凭据并发升级',['test_preissued_600_500_tokens_race_under_writer_lock']),
 ('S01-06','挂失解挂申购等固定红色不可降级',['test_fixed_red_actions_cannot_use_yellow_confirmation']),
 ('S01-07','认证资料只进专用通道',['test_authentication_text_never_reaches_model_or_regular_store','test_nested_model_payload_cannot_include_authentication_fields']),
 ('S01-08','重复确认只写一次、回执一致',['test_concurrent_same_fixed_form_returns_identical_receipt']),
 ('S01-09','暂停取消拒绝迟到模型结果',['test_actual_late_model_response_after_control_cannot_prepare','test_authorization_queue_cannot_undo_a_pause']),
 ('S01-10','身份会话版本到期绑定',['test_bound_authorization_cannot_cross_changed_security_context']),
 ('S01-11','后台到期重新核验及明确等待失败',['test_background_revalidates_instead_of_reusing_frontend_authority','test_background_workers_race_only_one_payment']),
 ('S01-12','提交后断网和实际子进程退出恢复不重复',['test_real_process_exit_after_commit_then_repeated_request_writes_once','test_business_commit_then_ui_ack_failure_recovers_without_second_write']),
 ('S01-13','持久锁定及冷却不自动重放',['test_persistent_lock_does_not_replay_on_cooldown'])]
matrix=[]
for item,title,names in safety:
 found=[c for c in cases if any(c.get('name','').split('[')[0]==name for name in names)]
 ok=bool(found) and all(c.find('failure') is None and c.find('error') is None and c.find('skipped') is None for c in found)
 if not ok:raise SystemExit('Safety matrix missing or failing: '+item)
 matrix.append(dict(id=item,requirement=title,passed=ok,test_records=len(found),test_names=[c.get('classname')+'.'+c.get('name') for c in found]))
(E/'remaining-s01-matrix.json').write_text(json.dumps(dict(source_version=backend['source_version'],rows=matrix),ensure_ascii=False,indent=2),encoding='utf-8')
matrix_text='# S01：当前版本风险回归矩阵\n\n2026-10-18。以下结果来自本次完整回归，不能以旧报告替代。认证、资金均为本地模拟，未接入真实银行、短信、人脸或 U 盾。\n\n| 要求 | 当前验证 | JUnit 记录 | 测试定位 |\n| --- | --- | --- | --- |\n'
for row in matrix:matrix_text+='| '+row['id']+' '+row['requirement']+' | 通过 | '+str(row['test_records'])+' | '+', '.join('`'+n+'`' for n in row['test_names'])+' |\n'
matrix_text+='\n测试源码位于 backend/tests/test_s01_authorization.py。并发案例用真实 SQLite 写锁、线程屏障、独立数据库会话，退出恢复使用独立子进程；失败注入后核对账本、幂等和回执。所有测试使用 TEST-ONLY 隔离库。\n\n当前 [JUnit](evidence/remaining-regression.xml)、[机器可读矩阵](evidence/remaining-s01-matrix.json)、[写入竞争原始数据](evidence/remaining-d01-writers.json)。模型语言质量与这些强制安全断言分开统计。\n'
(DOC/'S01-当前风险回归矩阵-20261018.md').write_text(matrix_text,encoding='utf-8')
live_pass=sum(i['passed'] for i in live['results']);multi_pass=sum(i['passed'] for i in recovery['turns']);requests=live.get('api_requests',sum(i['api_requests'] for i in live['results']))+recovery.get('api_requests',sum(i['api_requests'] for i in recovery['turns'])+(recovery.get('recovery') or {}).get('api_requests',0))
failed_live=[dict(case=i['case'],checks=i.get('checks',{})) for i in live['results'] if not i['passed']]
summary=dict(real_semantic_failures=failed_live,source_version=backend['source_version'],backend=backend,frontend=front,browser_checks=len(browser_details['checks']),browser_console_errors=len(browser_details['errors']),live_single=dict(passed=live_pass,total=len(live['results'])),live_multiturn=dict(passed=multi_pass,total=len(recovery['turns'])),live_post_commit_recovery=(recovery.get('recovery') or {}).get('passed',False),real_api_requests_final_samples=requests,runtime_database=current,runtime_database_unchanged=True,security_test_records=s01,recovery_test_records=g2,scale_factors=[s['scale'] for s in scale['scales'] if s['status']=='measured'])
(E/'remaining-summary-20261018.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
work=f"""# 开发记录：剩余轮次与最终验收

2026-10-18。本轮按最新授权依次处理 G01 第二阶段、D01、O01/T01 与参赛验收收尾；保留此前 F01、S01 和 G01 第一阶段。没有整体替换 Agent、按业务关键词重建路由或新增模型提交/认证工具。

## 工作区与数据保护

实际应用根目录：`{ROOT}`。开发建议中的旧 D 盘路径不存在；使用用户此前明确选定的“最新项目02”，没有改旧工作区。当前目录无 Git 元数据和适用 AGENTS.md，因此使用源码 SHA-256 标识，未声称提交 Git。

当前后端与测试源码摘要：`{backend['source_version']}`。完整文件清单见 [源码摘要](evidence/remaining-backend-source.json)，前端及复现脚本另见 [项目源码清单](evidence/remaining-project-source.json)。运行库为 `var/finagent.sqlite3`，迁移 `{current['migration']}`，{current['transactions']:,} 笔流水；修改前后摘要完全一致。没有重置运行库、删除历史回执或改密钥。测试全部使用 TEST-ONLY 和模拟资金，无 SQL 迁移。

修改前快照及失败尝试均在 `var/reviews/remaining-20261018/` 保留。最终证据位于 `release/`，只有本次冻结版本的完整测试参与统计；中间版因发现问题而终止的测试不称为通过。

## 实际新增与修复

1. 在原 `dynamic_runtime` 内持续保存目标、用户明确约束、节点、证据版本、待确认单、到期信息、失败分类和继续入口；新消息保留有效待办，插问新增目标。
2. 改预算/期限先失效旧用户派生值，再更新目标；更新校验失败时关联计算及回答被服务端阻断。复用本人查询和独立应急金计算。取消只影响相关未执行部分，不伪称撤回付款。
3. 重启先核对原领域回执与幂等记录；中断读取可补查，结果未知的写入准备进入待核实。付款后回复失败仍显示回执，不重放付款。
4. 新消息、暂停及版本变化继续屏蔽迟到响应，后台回调增加请求绑定；跨轮回执按当前 DeepSeek Chat Completions 协议传递。
5. 工具契约由代码生成，64 个兼容入口、58 个模型可见工具；通用旧入口仍保留，修正补卡说明矛盾，包括卡片查询返回的旧说明。
6. 修复一笔转账后办理另一类业务时清除旧单绑定的冲突；已执行转账回执与虚拟卡待确认单并存。逐项原话、防重放、页面授权保护保留。
7. 审计关联请求、目标、计划、节点、证据、固定单和回执；固定单展示真实版本。前端持续沿用当前对话（完成后追问也保留回执），提供新对话入口，区分失效记录与有效结论。
8. 建立完整命中集统计、三档规模、独立逐分核算和写入/后台竞争的可复现基线。

真实模型发现并修复：八千/8000 金额规范化误拒；目标更新失败仍继续计算；部分演示假设来源修正后的历史失败归并（尚未覆盖所有来源错误，见未通过项）；用文字声称“已取消”但没有目标状态变化。取消断言同时覆盖无查询证据和跨目标借用回执；仅有取消控制证据时由服务端说明未执行部分取消，不放宽无数据财务保护。早期余额评测曾错误限定工具名，误拒实际查询的金融档案余额；现在逐分核对当前本人账本、实际成功工具和可用余额事实。旧误判报告保留，最终样例重新实跑，未降低授权或金额断言。原安全/计算测试保留；工具总数断言仅随实际新增工具由 63 更新为 64。

## 本次实际结果

| 层次 | 当前结果 |
| --- | --- |
| 后端完整回归 | {backend['file_count']} 个文件，{backend['tests']} 条 JUnit 记录；失败 {backend['failures']}、错误 {backend['errors']}、跳过 {backend['skipped']}；一次完整 pytest 进程，源码前后相同 |
| S01 / G01 第二阶段 | 分别 {s01} / {g2} 条记录；包含真实线程竞争、故障注入和 24 轮对话 |
| 前端 | {front['tests']} 项单测、TypeScript 与生产构建通过 |
| Edge | {len(browser_details['checks'])} 个实际检查点，控制台错误 {len(browser_details['errors'])}；模型替身＋实际领域工具，服务已停止 |
| 真实 DeepSeek 单轮样例 | {live_pass}/{len(live['results'])}；普通问候、余额、缺预算、多目标、读取失败、待页面授权 |
| 真实 DeepSeek 多轮 | {multi_pass}/{len(recovery['turns'])}；改预算、插问、取消均保留原目标/证据并核对无资金操作 |
| 真实模型付款后恢复样例 | {'通过' if (recovery.get('recovery') or {}).get('passed') else '未通过，见原始记录'}；明确区分真实模型、注入断网和本地模拟银行 |
| 最终模型样例请求 | {requests} 次实际 API 请求；没有全局评测请求上限，保留应用每轮安全预算 |
| D01 | 实测规模 {', '.join(str(s['scale'])+' 倍' for s in scale['scales'] if s['status']=='measured')}；独立金额核对到分，查询计划、内存、延迟、体积和竞争原始数据单列 |

完整运行日志保留依赖弃用、禁用缓存配置和既有迁移 CHECK 约束告警，没有通过删除测试或降低断言隐藏。JUnit 计数含既有子测试记录，不把它与 pytest 顶层函数数量混称。真实单轮未通过项：`{json.dumps(failed_live,ensure_ascii=False)}`。当前未通过项是状态语义判断，真实业务数据、回执和权限断言分开核验；没有改低断言或只挑通过样本。模型小样本不是全部真实语言场景成功率；数学与权限断言独立检查，不能由语言平均分抵消失败。

## 文件与职责

| 文件 | 变化 |
| --- | --- |
| backend/app/agent/checkpoints.py | 约束、持久检查点、局部失效与回执核对 |
| dynamic_runtime.py / execution_graph.py / dialogue.py / card_flow.py | 接入原循环、真实状态、拒绝旧值及伪造取消、恢复协议 |
| dynamic_protocol.py / registry.py / tool_contracts.py / tool_catalog.py | 目标更新工具、真实 Schema、兼容入口与受信任策略 |
| audit.py / trace.py / authorization.py | 事实事件关联、固定单版本与授权回执 |
| domain/card_management.py / agent/card_flow.py | 修正补卡能力说明，保留已执行转账绑定 |
| frontend/src/useAgentWorkspace.ts / AnalysisWorkflowSummary.tsx / ConversationAuthorizations.tsx | 跨轮任务、实际进度、失效标识及版本展示 |
| scripts/test-current-regression.py / evaluate-recovery-live.py | 冻结源码回归、真实多轮与故障恢复 |
| scripts/benchmark-d01*.py / verify-d01-correctness.py | 规模、竞争、独立金额及覆盖核对 |
| scripts/export-tool-contracts.py / publish-remaining-evidence.py | 自动目录及只发布可核对当前证据 |

当前 13 项安全矩阵见 [S01 风险回归](S01-当前风险回归矩阵-20261018.md)。

详见 [G01 设计](G01-持久检查点与局部重规划.md)、[工具与审计](O01T01-工具契约与审计关联.md)、[D01 实测](D01-统计规模与稳定性实测.md)、[赛题对照与演示](参赛验收-当前能力对照与演示.md)。

## 复现命令

在应用根目录运行；所有结果写入新目录，避免覆盖已有证据：

```powershell
$env:FINAGENT_REVIEW_DIR="var/reviews/verify-$(Get-Date -Format yyyyMMdd-HHmmss)"
.\\.venv\\Scripts\\python.exe -B scripts/test-current-regression.py
.\\.venv\\Scripts\\python.exe -B scripts/check-current-frontend.py
.\\.venv\\Scripts\\python.exe -B scripts/export-tool-contracts.py
.\\.venv\\Scripts\\python.exe -B scripts/benchmark-d01.py --max-mib 1536 --out "$env:FINAGENT_REVIEW_DIR/d01.json"
.\\.venv\\Scripts\\python.exe -B scripts/verify-d01-correctness.py
.\\.venv\\Scripts\\python.exe -B scripts/benchmark-d01-writers.py
.\\.venv\\Scripts\\python.exe -B scripts/evaluate-g01.py
.\\.venv\\Scripts\\python.exe -B scripts/evaluate-recovery-live.py
.\\.venv\\Scripts\\python.exe -B scripts/check-g01-browser.py
```

本次各脚本使用 `FINAGENT_REVIEW_DIR=var/reviews/remaining-20261018/release`；重跑应设置新的目录，先完整回归生成源码清单，再运行需要清单的评测。规模验证需已有隔离年度样本；不向运行库导入扩容数据。正常运行方式保留 `scripts/dev.ps1 -Mode live` 与已有启动器，本地 `.env` 继续只在服务端读取。

## 未完成、未充分验证

- 六类场景的本地领域闭环与当前测试可核对；浏览器并未覆盖六类每个分支和每种红色验证。
- 真实模型本轮仍有两个状态语义验收项未过：演示参数修正后历史失败的归并，以及多目标缺预算的待输入判定。确定性替身通过不能覆盖这些 Live 失败。
- 真实 DeepSeek 为小样本；未做真实模型连续 20 轮长期稳定性或独立人工大规模语义评审，不能声称完全杜绝模型错误。
- 仍为串行和有界任务，不支持无限节点/目标、任意条件自动规划、外部资金自动补偿或撤回。
- 模拟资金、短信、商户和审批不代表真实银行接入；未知外部结果需要核实。
- 没有生产云环境、多机数据库、高并发 SLA、真实短信/人脸/U盾验收。最终 PPT、视频、仓库提交和赛事对模拟认证认可仍待落实。
"""
# A nested f-string expression above intentionally uses ordinary empty dict syntax.
work=work.replace('{{}}','{}')
(DOC/'开发记录-剩余轮次与最终验收-20261018.md').write_text(work,encoding='utf-8')
rows=[]
for s in scale['scales']:
 if s['status']=='measured':rows.append('| '+str(s['scale'])+' 倍 | '+f"{s['transactions']:,}"+' | '+f"{s['database_bytes']/1024**2:.1f} MiB"+' | '+', '.join(f"{c['median_ms']:.1f}" for c in s['cases'])+' | 到分一致 |')
perf='''# D01：统计正确性、规模与稳定性实测

2026-10-18。仅使用 TEST-ONLY 合成数据；完整源版本及机器配置见 [原始结果](evidence/remaining-d01.json)。Python/SQLite 版本、CPU 数量、可用内存、数据源哈希、并发和模型模式均随结果记录。本机测试期间可能同时运行其他验证，不据此宣称生产 SLA。

| 现金账本规模 | 流水数 | 数据库体积 | 四组查询中位数 ms（月/年/信用卡/其他用户） | 金额 |
| --- | --- | --- | --- | --- |
'''+ '\n'.join(rows)+'''

同时按同倍数扩展信用卡读投影；复制数据只用于分析，不冒充可支付账本。原始行数与统计全命中集在服务端处理，证据仅返回 1 条；并非拿第一页估算全年。

独立 Python 整数核算直接读取现金流水与信用卡事件，不调用统计函数的 fact_query；核对消费、退款、月度/年度、多卡及其他用户。内部划转和还款排除在消费外。补充跨导入起始日期和超出当前数据结束日期的缺口查询，核对已记录金额且 `is_complete=false`，不把未知记成零。覆盖详情见 [独立核对](evidence/remaining-d01-correctness.json)。

规模先按数据库页体积估算并检查磁盘、物理可用内存。早期 384 MiB 文件预算停止了 100 倍；重新核实资源后，在 1536 MiB 文件预算和物理内存守卫下完成实测。最终复测复用完整生成的数据，只重新读取与核算，不再次扩容运行库。查询计划捕获和四组查询预热在计时外完成，计时前移除捕获监听。与完整回归同时进行的较慢波动样本另存 remaining-d01-during-regression.json；最终性能采样在本轮其他测试结束后单独运行，不把两次差别称为代码提速。

查询计划包含实际领域 SQL 执行语句及合成绑定参数，并保留代表性的 owner/date 索引检查。索引、每次延迟、Python 分配峰值、进程工作集、数据库体积及 4 并发同用户/多用户读取均保留原始值。SQLite 锁等待使用 BEGIN IMMEDIATE 获取耗时近似，包含驱动开销，不称独立内核锁等待指标。

[写入与调度竞争](evidence/remaining-d01-writers.json)：重复确认、600/500 元累计风险竞争、同一后台预约多个 worker 与前台写入，每类重复两次。预期策略拒绝与异常错误分开统计，账本守恒单独断言。

没有凭空新增索引或缓存。实际发现的瓶颈是长对话把目标修改记录当作财务证据，超过回答引用上限；修复将控制记录保留在审计、排除出财务证据。同一份历史失败输入的修复前后对照记录引用数量与 Schema 结果，保留全部金融事实；不把该对照说成吞吐或模型质量提升。

当前源码 SHA 见开发记录；规模报告的 source_version 仅覆盖 backend/app，完整回归摘要另含测试文件，发布前校验二者对应同一应用代码。

24 轮替身对话覆盖改预算、独立目标、取消、否定办理、暂停和恢复；真实模型另做短样本，工具次数、调用数、token、耗时和错误分类见独立 Live 证据。没有做真实模型 20 轮、生产网络、多机或 100 用户并发，均列为未测。
'''
(DOC/'D01-统计规模与稳定性实测.md').write_text(perf,encoding='utf-8')
contest='''# 参赛验收：当前能力对照与演示

2026-10-18。依据用户提供的赛题正文；所有“已验证”指本轮当前源码和所列证据，不把模拟银行、模型替身或本地短信说成真实银行认证。

| 场景 | 当前已实现并验证 | 本轮证据 | 边界 |
| --- | --- | --- | --- |
| 智能转账 | 查询→准备固定单→黄色确认/红色强验证→一致回执；跨账户北京时间累计、提交重算、重复/并发/断网恢复 | remaining-scene-transfer.json；remaining-regression.xml 的 S01/security/background | 模拟资金，不是外部银行汇款 |
| 账单分析 | 全命中集消费减退款、月年报告、多卡、覆盖缺口；咨询不自动办理 | remaining-scene-bills.json；remaining-d01*.json；F01 当前回归 | 未知成本、汇率和未来收入不能编造 |
| 理财 | 风险评估、产品查询与对比、申赎固定单；小额申购仍为红色 | remaining-scene-wealth.json；chat_authorization/P1 当前回归 | 本地产品、净值与成交模拟 |
| 卡片 | 挂失/解挂、冻结/解冻、支付限制；补发与授信申请复用独立服务 | remaining-scene-cards.json；card_unloss/servicing 当前回归 | 申请不等于发卡/获批，导入信用卡交易仍只读 |
| 订阅代扣 | 黄色查询授权→独立解除单；会员合同取消与银行代扣分离 | remaining-scene-subscriptions.json；servicing/P1 当前回归 | 不代表真实商户会员已取消，不退款 |
| 生日联动 | 补充真实固定采购条款→确认→计划与专款预留；worker 的采购/失败/取消/重试保护 | remaining-scene-birthday.json；background/P1 当前回归 | 六场景轨迹验证安排创建，不把保存计划说成已送达；商户为模拟 |

上述 JSON 位于 docs/evidence；完整总表和数量见 [开发与验收记录](开发记录-剩余轮次与最终验收-20261018.md)。六场景语义入口采用明确标识的模型替身，业务工具和授权为实际代码；真实 DeepSeek 的覆盖范围单列，不能混算。

## 三组当前演示

1. **分析、改参、插问**：“想旅行，预算两万元，只分析不办理”→“预算改成八千元”→“先查余额，原旅行待办保留”→“取消旅行分析，保留余额结果”。展示目标、有效约束、原计算失效、保留查询和真实取消状态。
2. **转账与故障恢复**：“从活期账户转100元到储蓄账户”→聊天“确认”（应不执行）→点击固定单核对、页面授权→显示真实回执→刷新/恢复→“查询转后余额”。断网故障注入只在隔离验收中使用，不故意破坏正常服务。
3. **高风险与独立授权**：“挂失指定尾号银行卡”→确认对象→红色独立模拟验证及固定单确认→读回执；另开任务“查询云音乐订阅并解除银行代扣”→先黄色查询确认→再独立解除单确认。两份权限不能混用。

生日演示先问报价，补充生日、付款账户、收件/配送资料、两天前北京时间09:00采购、供应商固定报价、运费及余款处理；再核对固定单。不要假造用户爱人生日、地址或商品市场价格。

## 截图取证清单

- 已取得：多目标桌面、390px 窄屏、转账固定单与刷新后回执、两类固定单窄屏、长回复窄屏、真实流水分页；见 remaining-*.png。
- 已在 Edge 验证：缺预算、同任务改预算、工具失败、待页面授权、页面授权、刷新回执、两类固定单及状态、长回复与流水分页、无横向溢出。
- 待现场截图：红色模拟验证完整交互、理财、订阅双确认、卡片补发、生日采购/失败/恢复。未获取的图片不标完成。

## 分类与剩余待办

- **当前已实现并验证**：上述本地领域主链、独立权限/幂等、串行目标图与恢复，关联本轮证据。
- **已实现但验证不足**：自然语言长时间真实模型稳定性、六场景全部浏览器分支、复杂多目标的广泛人工审定。
- **本地模拟**：银行账户与资金、短信验证码、审批、理财、生日供应商。真实 API 仅指 DeepSeek 模型请求。
- **尚未实现或未接入**：真实银行/短信/人脸/U盾、任意外部补偿、无限并行 DAG、多机生产部署验证。

下一步优先补齐现场红色验证和六场景浏览器录像、独立人工语义评测、最终 PPT/五分钟视频及仓库提交；核实赛事对模拟 MFA 和沙箱口径的认可。禁止用旧测试数量、效果图或未经运行的流程冒充验收。
'''
(DOC/'参赛验收-当前能力对照与演示.md').write_text(contest,encoding='utf-8')
ledger="""# 后续开发轮次执行台账

2026-10-18。按用户明确给出的轮次推进；建议文档保留原样。当前代码与本轮证据为准，不以历史数量代替现验。

| 轮次 | 本次落实 | 证据与剩余项 |
| --- | --- | --- |
| F01 | 复用财务底稿、计算及原话/单位/建议不办理保护，纳入当前全回归 | 当前 F01 与 personalized_analysis JUnit；真实语义仍有不稳定项 |
| S01 | 13项强制风险矩阵、实际并发/退出/断网回归 | S01-当前风险回归矩阵-20261018.md；模拟 MFA |
| G01 第一阶段 | 复用目标与类型图，保留当前图校验和主链 | 当前 test_g01_execution_graph 回归 |
| G01 第二阶段 | 同一状态上的检查点、局部失效、插问、回执优先恢复、共享查询、失败分类 | G01-持久检查点与局部重规划.md；跨轮24轮替身，Live短样本；不支持无限或并行图 |
| D01 | 1/10/100倍账本全命中集统计、独立到分核算、读写与后台竞争、证据引用修复前后比较 | D01-统计规模与稳定性实测.md；无生产 SLA 或真实模型20轮长期实测 |
| O01/T01 | 自动工具目录、兼容入口隔离、审计关联、真实固定单版本和页面状态 | O01T01-工具契约与审计关联.md；领域 data 尚未全部静态类型化 |
| 参赛验收收尾/E01 | 六类本地领域轨迹、真实模型小样本、14项 Edge 检查、实际截图、演示顺序和待办 | 参赛验收-当前能力对照与演示.md；红色全程与六类所有分支尚未全部浏览器取证 |

建议文档中的后续真实银行/商户/认证、渠道适配、受控并行、生产部署、更多合同及多币种扩展仍是待办，不把它们自动标成完成。最终 PPT、视频、外部仓库发布及赛事对模拟认证的认可尚待落实。

总结果见 [当前开发与验收记录](开发记录-剩余轮次与最终验收-20261018.md)，真实回答见 [实际前后对照](对话与恢复-实际前后对照-20261018.md)。
"""
(DOC/'后续开发轮次-执行台账-20261018.md').write_text(ledger,encoding='utf-8')
comparison='# 对话、状态与恢复的实际前后对照\n\n2026-10-18。以下为运行观察，不是静态演示冒充模型。自然语言原文可在 Live JSON 核对；截取不代表整段回答。\n\n'
comparison+='## 修复对照\n\n| 观察到的问题 | 本轮修改后 | 依据 |\n| --- | --- | --- |\n| 长对话17个控制/查询证据引用超过回答上限 | 控制事件仍审计，金融证据1个、金融事实仍14项，Schema可接受 | remaining-d01-correctness.json，同一失败输入哈希 |\n| 取消目标只有文字声明，未改变目标状态 | 先实际目标取消；无数据时仅服务端说明未执行部分，不宣称撤回付款 | test_g01_recovery 当前回归 |\n| 转账后接另一类单清除旧绑定，读取409 | 原转账回执及新卡待确认单并存，恢复仍仅一笔转账 | 当前两类资源回归及 multiple-forms-mobile.png |\n| 模型把“八千”与8000混用遭到原话误拒 | 复用中文金额校验规范化；更新失败时不能绕过旧假设屏障 | 当前规范化、修改预算及失败屏障回归 |\n\n'
comparison+='## 当前真实多轮回答节选\n\n'
for t in recovery['turns']:
 comparison+='用户：'+t['user']+'\n\n回答节选：\n\n> '+t['reply'][:600].replace('\n','\n> ')+'\n\n本轮检查：'+json.dumps(t['checks'],ensure_ascii=False)+'\n\n'
comparison+='## 当前未通过项\n\n'+json.dumps(failed_live,ensure_ascii=False,indent=2)+'\n\n原始 [单轮结果](evidence/remaining-live.json)、[多轮及付款后恢复](evidence/remaining-live-recovery.json)。不同版本的小样本不会合并为成功率。中间版真实问候曾被误标待输入；本轮增强字段语义后该样例通过，不据此承诺所有问候永久正确。\n'
(DOC/'对话与恢复-实际前后对照-20261018.md').write_text(comparison,encoding='utf-8')
readme=ROOT/'README.md';text=readme.read_text(encoding='utf-8');intro='> 2026-10-18 后续轮次：已增量完成 G01 检查点/恢复、D01 实测、O01/T01 与六场景验收整理；当前结果和边界见 [开发记录](docs/开发记录-剩余轮次与最终验收-20261018.md) 和 [轮次台账](docs/后续开发轮次-执行台账-20261018.md)。旧报告仅作历史记录。\n\n'
if intro not in text:readme.write_text(intro+text,encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False),flush=True)
