import { useEffect, useState } from 'react';
import type { AgentWorkspace } from './useAgentWorkspace';
import { labels, stepTitle } from './taskPresentation';
import ConditionDetails from './ConditionDetails';
import ExecutionRecord from './ExecutionRecord';
import SubscriptionQueryConsent from './SubscriptionQueryConsent';
import ConversationAuthorizations from './ConversationAuthorizations';
import AnalysisWorkflowSummary from './AnalysisWorkflowSummary';
import AnalyticsReport from './AnalyticsReport';
import TransactionEvidence from './TransactionEvidence';
import DetailDialog from './DetailDialog';
import Icon from './Icon';
const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);

export default function TaskContextPanel({ workspace: w, onNavigate, standalone = true }: { workspace: AgentWorkspace; onNavigate: (page: string) => void; standalone?: boolean }) {
  const t = w.task;
  const [evidence, setEvidence] = useState(false);
  useEffect(() => setEvidence(false), [t?.id]);
  if (!t) return <div className="empty compact">暂无当前对话任务。<button onClick={() => onNavigate('assistant')}>开始对话</button></div>;
  const reports = t.steps.filter(s => s.status === 'succeeded' && ['summarize_spending', 'generate_statement', 'inspect_anomalies', 'list_transactions', 'analyze_finances', 'read_financial_history', 'read_subscriptions', 'identify_subscriptions'].includes(s.tool_name));
  const p = t.proposal, finished = t.execution_graph? t.execution_graph.goals.every(g=>['completed','cancelled'].includes(g.status)):['completed', 'cancelled', 'failed', 'expired'].includes(t.status);
  return <aside className={`task-context${standalone ? ' standalone' : ''}`} aria-label="当前任务">
    <div className="context-heading"><h2>当前任务</h2><span className={`tag ${t.status === 'awaiting_confirmation' ? 'warning' : ''}`}>{labels[t.status] ?? t.status}</span></div>
    {standalone&&<SubscriptionQueryConsent workspace={w}/>}
    {standalone&&<AnalysisWorkflowSummary workflow={t.analysis_workflow} graph={t.execution_graph}/>}
    {standalone&&<ConversationAuthorizations workspace={w}/>}
    {t.control?.state && <p className="operation-note">{({ paused: '已暂停', handoff: '等待模拟坐席接管', awaiting_reverification: '待用户重新验证', running: '可以继续' } as Record<string, string>)[t.control.state] ?? t.control.state} {t.control.reason}{t.control.suggestion && ` · 坐席建议：${t.control.suggestion}`}</p>}
    {!t.execution_graph&&<ol className="task-timeline">{t.steps.length ? t.steps.map(s => <li key={`${t.id}-${t.plan_version}-${s.ordinal}`} className={`step-${s.status}`}><span className="step-marker">{s.status === 'succeeded' ? '✓' : s.ordinal}</span><div><strong>{stepTitle(s)}</strong>{s.result.reason && <p>{s.result.reason}</p>}{s.result.message && <p>{s.result.message}</p>}</div></li>) : <li><span className="step-marker">1</span><div><strong>{labels[t.status] ?? t.status}</strong><p>等待补充需求或核对业务信息。</p></div></li>}</ol>}
    {p && <div className="context-details"><h3>操作详情</h3><dl>{p.summary.target && <div><dt>收款人</dt><dd>{p.summary.target.alias} {p.summary.target.mask}</dd></div>}{p.summary.source && <div><dt>转出账户</dt><dd>{p.summary.source.alias} {p.summary.source.mask}</dd></div>}{p.summary.card && <div><dt>卡片</dt><dd>{p.summary.card.alias} {p.summary.card.mask}</dd></div>}{p.summary.amount_minor !== undefined && <div><dt>金额</dt><dd>{money(p.summary.amount_minor)}</dd></div>}<div><dt>操作单状态</dt><dd>{labels[p.status] ?? p.status}</dd></div></dl></div>}
    {!!t.requested_outcomes?.length && <details className="goal-details"><summary>完整目标与条件</summary><ul>{t.requested_outcomes.map((g, i) => <li key={i}>{typeof g === 'string' ? g : g.text}</li>)}</ul><ConditionDetails condition={p?.summary.condition} validation={p?.validation} /></details>}
    <p className="context-boundary"><Icon name="info" size={18} />暂停或取消不会撤回已完成交易。</p>
    <div className="task-controls"><button className="secondary" disabled={w.busy} onClick={w.retry}>重新查询状态</button>{!finished && <><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('pause')}>暂停任务</button><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('continue')}>恢复检查点</button><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('handoff')}>请求人工接管</button><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('cancel')}>取消任务</button></>}{t.status === 'partially_completed' && t.steps.some(s => s.tool_name === 'list_accounts' && s.status === 'failed') && <button disabled={w.unavailable} onClick={() => void w.taskAction('resume')}>重试余额查询</button>}{standalone && <button onClick={() => onNavigate('assistant')}>返回对话办理</button>}</div>
    {w.syncError && <p className="alert" role="alert">{w.syncError} 写操作已暂停。</p>}{w.error && standalone && <p className="alert" role="alert">{w.error}</p>}
    {!!reports.length && <button className="secondary" onClick={() => setEvidence(true)}><Icon name="bill" />查看报告与逐笔证据</button>}
    <details className="record-details"><summary>执行记录与版本</summary><p>任务 {t.id}</p><p>计划版本 {t.plan_version} · {t.mode.toUpperCase()}</p>{p && <p>操作单 {p.id}</p>}<p>仅展示服务端已保存的受限步骤，不代表通用多写计划。</p><ExecutionRecord key={t.id} taskId={t.id} /></details>
    {evidence && <DetailDialog title="报告与业务证据" onClose={() => setEvidence(false)}>{reports.map(s => <div key={`${t.id}-${s.ordinal}`}>{s.tool_name === 'list_transactions' && s.result.items ? <TransactionEvidence initial={{ items: s.result.items, total: s.result.total ?? s.result.items.length, limit: s.result.limit ?? 20, offset: s.result.offset ?? 0 }} filters={s.arguments ?? {}} /> : <AnalyticsReport value={s.result} filters={s.arguments??{}} />}</div>)}</DetailDialog>}
  </aside>;
}

