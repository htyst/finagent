import { useEffect, useRef, useState } from 'react';
import type { AgentWorkspace } from './useAgentWorkspace';
import { labels, stepTitle } from './taskPresentation';
import ConditionDetails, { type Validation } from './ConditionDetails';
import AnalyticsReport from './AnalyticsReport';
import TaskDetailsForm from './TaskDetailsForm';
import TransactionEvidence from './TransactionEvidence';
import DetailDialog from './DetailDialog';
import MessageComposer from './MessageComposer';
import Icon from './Icon';
import ExecutionRecord from './ExecutionRecord';
import SubscriptionQueryConsent from './SubscriptionQueryConsent';
import ConversationAuthorizations from './ConversationAuthorizations';
import AnalysisWorkflowSummary from './AnalysisWorkflowSummary';

const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);
const shortcuts = [ ['transfer', '给小林转 200 元', '快速准备转账', '给小林转200元'], ['bill', '分析本月账单', '查看收支结构与异常', '分析本月账单'], ['wallet', '管理订阅代扣', '先查看续费与扣费记录', '先查看我的订阅扣费和自动续费情况'], ['calendar', '安排生日预算', '规划预算并设置安排', '安排生日预算'] ];

export function TaskContextPanel({ workspace: w, onNavigate, standalone = false }: { workspace: AgentWorkspace; onNavigate: (page: string) => void; standalone?: boolean }) {
  const t = w.task;
  if (!t) return <div className="empty compact">暂无当前对话任务。<button onClick={() => onNavigate('assistant')}>开始对话</button></div>;
  const p = t.proposal, finished = ['completed', 'cancelled', 'failed', 'expired'].includes(t.status);
  return <aside className={`task-context${standalone ? ' standalone' : ''}`} aria-label="当前任务">
    <div className="context-heading"><h2>当前任务</h2><span className={`tag ${t.status === 'awaiting_confirmation' ? 'warning' : ''}`}>{labels[t.status] ?? t.status}</span></div>
    {standalone&&<SubscriptionQueryConsent workspace={w}/>}
    {standalone&&<AnalysisWorkflowSummary workflow={t.analysis_workflow}/>}
    {standalone&&<ConversationAuthorizations workspace={w}/>}
    {t.control?.state && <p className="operation-note">{({ paused: '已暂停', handoff: '等待模拟坐席接管', awaiting_reverification: '待用户重新验证', running: '可以继续' } as Record<string, string>)[t.control.state] ?? t.control.state} {t.control.reason}{t.control.suggestion && ` · 坐席建议：${t.control.suggestion}`}</p>}
    <ol className="task-timeline">{t.steps.length ? t.steps.map(s => <li key={`${t.id}-${t.plan_version}-${s.ordinal}`} className={`step-${s.status}`}><span className="step-marker">{s.status === 'succeeded' ? '✓' : s.ordinal}</span><div><strong>{stepTitle(s)}</strong>{s.result.reason && <p>{s.result.reason}</p>}{s.result.message && <p>{s.result.message}</p>}</div></li>) : <li><span className="step-marker">1</span><div><strong>{labels[t.status] ?? t.status}</strong><p>等待补充需求或核对业务信息。</p></div></li>}</ol>
    {p && <div className="context-details"><h3>操作详情</h3><dl>{p.summary.target && <div><dt>收款人</dt><dd>{p.summary.target.alias} {p.summary.target.mask}</dd></div>}{p.summary.source && <div><dt>转出账户</dt><dd>{p.summary.source.alias} {p.summary.source.mask}</dd></div>}{p.summary.card && <div><dt>卡片</dt><dd>{p.summary.card.alias} {p.summary.card.mask}</dd></div>}{p.summary.amount_minor !== undefined && <div><dt>金额</dt><dd>{money(p.summary.amount_minor)}</dd></div>}<div><dt>操作单状态</dt><dd>{labels[p.status] ?? p.status}</dd></div></dl></div>}
    {!!t.requested_outcomes?.length && <details className="goal-details"><summary>完整目标与条件</summary><ul>{t.requested_outcomes.map((g, i) => <li key={i}>{typeof g === 'string' ? g : g.text}</li>)}</ul><ConditionDetails condition={p?.summary.condition} validation={p?.validation} /></details>}
    <p className="context-boundary"><Icon name="info" size={18} />暂停或取消不会撤回已完成交易。</p>
    <div className="task-controls"><button className="secondary" disabled={w.busy} onClick={w.retry}>重新查询状态</button>{!finished && <><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('pause')}>暂停任务</button><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('continue')}>恢复检查点</button><button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('handoff')}>请求人工接管</button></>}{t.status === 'partially_completed' && t.steps.some(s => s.tool_name === 'list_accounts' && s.status === 'failed') && <button disabled={w.unavailable} onClick={() => void w.taskAction('resume')}>重试余额查询</button>}{standalone && <button onClick={() => onNavigate('assistant')}>返回对话办理</button>}</div>
    {w.syncError && <p className="alert" role="alert">{w.syncError} 写操作已暂停。</p>}{w.error && standalone && <p className="alert" role="alert">{w.error}</p>}
    <details className="record-details"><summary>执行记录与版本</summary><p>任务 {t.id}</p><p>计划版本 {t.plan_version} · {t.mode.toUpperCase()}</p>{p && <p>操作单 {p.id}</p>}<p>仅展示服务端已保存的受限步骤，不代表通用多写计划。</p><ExecutionRecord key={t.id} taskId={t.id} /></details>
  </aside>;
}

function OperationSummaryCard({ workspace: w }: { workspace: AgentWorkspace }) {
  const p = w.task?.proposal;
  if (!p) return null;
  const pending = p.status === 'pending_confirmation';
  return <article className="proposal-card agent-proposal" data-testid="operation-card"><div className={`operation-card-heading ${pending ? 'pending' : ''}`}><strong><Icon name="shield" />{pending ? '待核对' : labels[p.status] ?? p.status}</strong><span className="tag">{pending ? '等级以安全核对为准' : '固定操作单'}</span></div><div className="operation-card-body">
    <h3>{p.kind === 'transfer' ? '模拟转账操作单' : '卡片操作单'}</h3><dl className="operation-fields">
      {p.summary.target && <div><dt><Icon name="user" />收款人</dt><dd><strong>{p.summary.target.alias}</strong><small>{p.summary.target.bank_name} {p.summary.target.mask}</small>{p.summary.target.phone_mask && <small>{p.summary.target.phone_mask}</small>}</dd></div>}
      {p.summary.source && <div><dt><Icon name="wallet" />转出账户</dt><dd><strong>{p.summary.source.alias}</strong><small>{p.summary.source.mask}</small></dd></div>}
      {p.summary.card && <div><dt><Icon name="wallet" />卡片操作</dt><dd>{p.summary.card.alias} {p.summary.card.mask}<small>{p.summary.action === 'freeze' ? '临时冻结' : '解冻'}</small></dd></div>}
      {p.summary.amount_minor !== undefined && <div className="amount-field"><dt><Icon name="transfer" />转账金额</dt><dd className="proposal-amount">{money(p.summary.amount_minor)}</dd></div>}
    </dl><p className="operation-note">{p.summary.impact}</p><ConditionDetails condition={p.summary.condition} validation={p.validation} />
    {p.summary.target?.account_number && <details><summary>核对完整收款账号</summary><p>{p.summary.target.account_number}</p></details>}<small>有效至 {new Date(p.expires_at_utc).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</small>
    {!pending && !p.receipt && <p className="coverage-warning">当前单{labels[p.status] ?? p.status}，不能继续确认。修改或重新提出需求后需核对新单。</p>}
    {pending && <><div className="proposal-actions"><button type="button" disabled={w.unavailable} onClick={() => void w.confirm(p)}>核对并继续</button><button className="secondary" disabled={w.unavailable} onClick={() => { w.setInput(''); document.getElementById('agent-message')?.focus(); }}>修改信息</button>{['investigate_card', 'conditional_transfer'].includes(w.task?.intent ?? '') && <button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('reject-operation')}>{w.task?.intent === 'investigate_card' ? '仅拒绝冻结，继续查余额' : '拒绝转账'}</button>}<button className="text-button" disabled={w.unavailable} onClick={() => void w.taskAction('cancel')}>取消整个任务</button></div><p className="caption">下一步查看服务端固定条款；需要强验证时，在独立窗口完成。</p></>}
    {p.receipt && <div className="receipt" role="status"><strong>{p.receipt.message}</strong>{p.receipt.amount_minor !== undefined && <p>金额 {money(p.receipt.amount_minor)}{p.receipt.source_balance_minor !== undefined && ` · 来源余额 ${money(p.receipt.source_balance_minor)}`}</p>}{p.receipt.recipient_credit_minor !== undefined && <p>收款人模拟入账 {money(p.receipt.recipient_credit_minor)} · 入账编号 {p.receipt.recipient_credit_id}</p>}{p.receipt.card_status && <p>卡片状态：{p.receipt.card_status === 'frozen' ? '已冻结' : '正常'}</p>}<small>{p.receipt.order_id && `回执 ${p.receipt.order_id} · `}{p.receipt.committed_at_utc && new Date(p.receipt.committed_at_utc).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}</small><small>操作单 {p.id}</small></div>}
  </div></article>;
}

export default function AssistantWorkspace({ workspace: w, accounts, mode, onNavigate, balance, home, onHomeChange }: { workspace: AgentWorkspace; accounts: Array<{ id: string; alias: string; account_mask: string; status: string }>; mode: string; onNavigate: (page: string) => void; balance?: number; home: boolean; onHomeChange: (home: boolean) => void }) {
  const [drawer, setDrawer] = useState(false), [collapsed, setCollapsed] = useState(false), [evidence, setEvidence] = useState(false);
  const scroll = useRef<HTMLDivElement>(null), nearBottom = useRef(true);
  const [unread, setUnread] = useState(false);
  const t = w.outgoing?.newTask?null:w.task, landing = home || (!t?.messages.length&&!w.outgoing);
  useEffect(() => { if (nearBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; else setUnread(true); }, [t?.id, t?.messages.length, t?.proposal?.status, w.busy,w.outgoing?.id]);
  useEffect(() => { nearBottom.current = true; if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [landing]);
  function send() { onHomeChange(false); nearBottom.current = true; void w.sendMessage(w.input.trim()); }
  function messageText(content: string) { if (!content.startsWith('选择交易 ')) return content; const c = w.task?.candidates.find(x => x.id === content.slice(5)); return c ? `选择 ${c.merchant || '商户未提供'} · ${money(c.amount_minor)} · 卡尾号 ${c.card_last4 ?? '未知'}` : '已选择要核对的交易'; }
  const reports = t?.steps.filter(s => s.status === 'succeeded' && ['summarize_spending', 'generate_statement', 'inspect_anomalies', 'list_transactions', 'analyze_finances', 'read_financial_history', 'read_subscriptions', 'identify_subscriptions'].includes(s.tool_name)) ?? [];
  const businessTarget = t?.service_request ? (t.service_request.kind==='cancel_contract'?'assets/subscriptions':'assets/cards') : (t?.birthday_request || t?.intent?.includes('birthday')) ? 'tasks/birthdays' : (t?.subscription_request || t?.intent?.includes('subscription')) ? 'assets/subscriptions' : (t?.wealth_order || t?.intent?.includes('wealth')) ? 'assets/wealth' : ['schedule_transfer', 'aa_collection'].includes(t?.intent ?? '') ? 'tasks/schedules' : (t?.card_request || t?.intent?.startsWith('card_')) ? 'assets/cards' : null;
  return <section id="assistant" className={`assistant-workspace ${landing ? 'is-home' : 'is-working'}${collapsed ? ' context-collapsed' : ''}`}>
    {landing ? <div className="assistant-home">{balance !== undefined && <button className="balance-glance" onClick={() => onNavigate('assets/accounts')}><span>可用余额 <Icon name="eye" size={16} /></span><strong>{money(balance)}</strong><Icon name="chevron" size={17} /></button>}<div className="home-content"><h1>把繁琐的金融操作，交给一句话。</h1><p className="home-subtitle">查账、转账、管理卡片，让每一步都清楚可控。</p>
      <MessageComposer value={w.input} onChange={w.setInput} onSend={send} disabled={w.unavailable} home />
      <div className="quick-grid">{shortcuts.map(([icon, title, sub, text]) => <button className="quick-card" key={icon} onClick={() => { w.setInput(text); document.getElementById('agent-message')?.focus(); }}><span className="round-icon"><Icon name={icon} size={24} /></span><span><strong>{title}</strong><small>{sub}</small></span><Icon name="chevron" size={18} /></button>)}</div>
      {t && <div className="continue-section"><div className="section-heading"><h2>继续办理</h2><button className="text-button" onClick={() => onNavigate('tasks/current')}>查看任务 <Icon name="chevron" size={16} /></button></div><button className="continue-task" onClick={() => onHomeChange(false)}><span className="round-icon"><Icon name="tasks" size={24} /></span><span><strong>{t.proposal?.kind === 'transfer' ? '转账办理' : '最近对话任务'} <span className="tag">{labels[t.status] ?? t.status}</span></strong><small>{t.messages.find(m => m.role === 'user')?.content ?? '查看服务端保存的当前任务'}</small></span><Icon name="chevron" /></button></div>}
      <p className="home-footnote"><Icon name="info" size={17} />查询直接返回，涉及资金或安全的操作会先请你确认。</p>
    </div></div> : <><div className="conversation-panel"><header className="conversation-heading"><div><h2>{t?.proposal?.kind === 'transfer' ? `转账给${t.proposal.summary.target?.alias ?? '收款人'}` : '对话助手'}</h2><span>{w.busy ? '正在处理，请稍候…' : w.loading ? '正在恢复任务…' : w.syncError ? '状态待核实' : '服务端状态已同步'}</span></div><div><button className="secondary context-toggle" onClick={() => setDrawer(true)}>当前任务</button><button className="text-button" onClick={() => onHomeChange(true)}>助手首页</button></div></header>
      <div className="conversation-scroll" ref={scroll} onScroll={e => { const el = e.currentTarget; nearBottom.current = el.scrollHeight - el.clientHeight - el.scrollTop < 90; if (nearBottom.current) setUnread(false); }}>
        <div className="agent-messages" aria-live="polite">{w.task?.messages.map((m, i) => <div key={`${w.task!.id}-${i}`} className={`message-row ${m.role === 'user' ? 'from-user' : 'from-assistant'}`}><span className="message-avatar"><Icon name={m.role === 'user' ? 'user' : 'chat'} /></span><div className="message-bubble">{messageText(m.content)}</div></div>)}{w.outgoing&&<div className="message-row from-user" data-testid="outgoing-message"><span className="message-avatar"><Icon name="user"/></span><div className="message-bubble"><div>{w.outgoing.text}</div><small className="outgoing-status" role="status">{w.outgoing.status==='sending'?'正在发送…':w.outgoing.status==='failed'?'未确认送达，请核对错误后重试。':'送达状态待核实，未自动重发。'}</small>{w.outgoing.status!=='sending'&&<button className="secondary" disabled={w.busy} onClick={w.retry}>核对发送状态</button>}</div></div>}</div>
        {t&&(t.authorization_operations?<ConversationAuthorizations workspace={w}/>:<OperationSummaryCard workspace={w} />)}
        {t&&<SubscriptionQueryConsent workspace={w}/>}
        <AnalysisWorkflowSummary workflow={t?.analysis_workflow}/>
        {t?.birthday_details && <TaskDetailsForm taskId={t.id} kind="birthday" value={t.birthday_details} accounts={accounts} disabled={w.unavailable} onSubmit={w.submitDetails} />}
        {t?.transfer_details && <TaskDetailsForm taskId={t.id} kind="transfer" value={t.transfer_details} accounts={accounts} disabled={w.unavailable} onSubmit={w.submitDetails} />}
        {!!t?.candidates.length && <div className="agent-candidates"><h3>待核对的候选交易</h3>{t.query_range && <p className="caption">北京时间 {t.query_range.map(s => new Date(s).toLocaleString('zh-CN')).join(' 至 ')}（结束时刻不含）</p>}{t.candidates.map(c => <article className="agent-candidate" key={c.id}><strong>{c.merchant || '商户未提供'} · {money(c.amount_minor)}</strong><p>{new Date(c.posted_at_utc).toLocaleString('zh-CN')} · 卡尾号 {c.card_last4 ?? '未知'}</p>{t.status === 'gathering_info' && <button disabled={w.unavailable} onClick={() => void w.sendMessage(`选择交易 ${c.id}`)}>选择这笔交易</button>}</article>)}</div>}
        {t?.steps.filter(s => s.result.available_balance_minor !== undefined).map(s => <div className="balance-result" key={s.ordinal}><Icon name="wallet" /><div><small>{s.result.alias} {s.result.mask} · 查询时可用余额</small><strong>{money(s.result.available_balance_minor!)}</strong></div></div>)}
        {t?.steps.filter(s => s.result.condition && !t.proposal).map(s => <ConditionDetails key={s.ordinal} condition={s.result.condition} validation={s.result as Validation} />)}
        {!!reports.length && <button className="result-link secondary" onClick={() => setEvidence(true)}><Icon name="bill" />查看报告与逐笔证据 <Icon name="chevron" size={18} /></button>}
        {businessTarget && <button className="result-link secondary" onClick={() => onNavigate(businessTarget)}><Icon name="tasks" />查看办理单、状态与回执 <Icon name="chevron" size={18} /></button>}
        {t?.status === 'gathering_info' && !t.proposal && <button className="secondary" disabled={w.unavailable} onClick={() => void w.taskAction('cancel')}>取消整个任务</button>}
        {t && t.mode !== mode && <p className="coverage-warning">此历史任务来自 {t.mode.toUpperCase()} 模式，新任务使用 {mode.toUpperCase()} 模式。</p>}
        {w.busy && <p className="working-indicator" role="status">正在理解需求并核对业务状态…</p>}{w.notice && <p className="operation-notice" role="status">{w.notice}</p>}
      </div>
      {unread && <button className="new-message secondary" onClick={() => { if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; setUnread(false); nearBottom.current = true; }}>有新消息，查看最新</button>}
      <div className="conversation-composer"><MessageComposer value={w.input} onChange={w.setInput} onSend={send} disabled={w.unavailable} /><p className="caption">Enter 发送 · Shift + Enter 换行 · 金融操作需核对固定条款</p></div>
    </div>{t && <div className="desktop-context"><button className="context-collapse text-button" onClick={() => setCollapsed(v => !v)}>{collapsed ? '展开任务栏' : '收起任务栏'}</button>{!collapsed && <TaskContextPanel workspace={w} onNavigate={onNavigate} />}</div>}
    {drawer && <DetailDialog title="当前任务" onClose={() => setDrawer(false)}><TaskContextPanel workspace={w} onNavigate={onNavigate} /></DetailDialog>}</>}
    {w.error && <div className="alert workspace-alert" role="alert">{w.error}</div>}{w.syncError && <div className="alert workspace-alert" role="alert">{w.syncError} 写操作已暂停。<button className="secondary" onClick={w.retry} disabled={w.busy}>重新查询状态</button></div>}
    {evidence && <DetailDialog title="报告与业务证据" onClose={() => setEvidence(false)}>{reports.map(s => <div key={`${t?.id}-${s.ordinal}`}>{s.tool_name === 'list_transactions' && s.result.items ? <TransactionEvidence initial={{ items: s.result.items, total: s.result.total ?? s.result.items.length, limit: s.result.limit ?? 20, offset: s.result.offset ?? 0 }} filters={s.arguments ?? {}} /> : <AnalyticsReport value={s.result} filters={s.arguments??{}} />}</div>)}</DetailDialog>}
  </section>;
}
