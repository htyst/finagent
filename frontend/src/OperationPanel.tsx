import { useWorkspaceDraft } from './WorkspaceDrafts';
import { useEffect, useState } from 'react';
import { api } from './api';
import { useTaskSync } from './useTaskSync';
import ConditionDetails, { type Condition, type Validation } from './ConditionDetails';

type Account = { id: string; alias: string; account_mask: string; balance_minor: number; status: string };
type Card = { id: string; alias: string; card_mask: string; status: string; account_id: string };
type Recipient = { id: string; name: string; account_mask: string; phone_mask?: string | null; status: string };
type Receipt = { kind: string; message: string; amount_minor?: number; source_balance_minor?: number;
  recipient_credit_minor?: number; recipient_credit_id?: string; target_balance_minor?: number | null; card_status?: string; transaction_ids?: string[]; committed_at_utc: string };
type Proposal = { id: string; task_id: string; kind: string; status: string; expires_at_utc: string;
  summary: { action: string; source?: { alias: string; mask: string }; target?: { alias: string; mask: string; phone_mask?: string | null; bank_name?: string; account_number?: string };
    target_type?: string; amount_minor?: number; currency?: string; note?: string;
    card?: { alias: string; mask: string }; impact: string; condition?: Condition }; validation?: Validation | null; receipt: Receipt | null };
type Task = { id: string; kind: string; status: string; proposal: Proposal | null;
  events: Array<{ event: string; created_at_utc: string }> };

const money = (minor: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(minor / 100);

function minorFromYuan(value: string): number {
  if (!/^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$/.test(value)) throw new Error('金额须为最多两位小数的正数');
  const [yuan, fraction = ''] = value.split('.');
  const minor = Number(yuan) * 100 + Number(fraction.padEnd(2, '0'));
  if (!Number.isSafeInteger(minor) || minor < 1 || minor > 500_000) throw new Error('单笔金额须在 0.01 元至 5,000 元之间');
  return minor;
}

export default function OperationPanel({ accounts, cards, onChanged, agentProposalId, revision }: {
  accounts: Account[]; cards: Card[]; onChanged: () => Promise<void>; agentProposalId: string | null; revision: number;
}) {
  const [recipients, setRecipients] = useState<Recipient[]>([]);
  const { task, busy, setBusy, loading, syncError, refresh: refreshTask, retry } = useTaskSync<Task>('/api/tasks/current', onChanged, revision);
  const unavailable = busy || loading || !!syncError;
  const [kind, setKind] = useWorkspaceDraft<'transfer' | 'card_action'>('manual-kind', 'transfer');
  const [sourceId, setSourceId] = useWorkspaceDraft('manual-sourceId', '');
  const [targetType, setTargetType] = useWorkspaceDraft<'own_account' | 'saved_recipient' | 'external_account'>('manual-targetType', 'own_account');
  const [targetId, setTargetId] = useWorkspaceDraft('manual-targetId', '');
  const [recipientName,setRecipientName]=useWorkspaceDraft('manual-recipientName', '');
  const [bankName,setBankName]=useWorkspaceDraft('manual-bankName', '');
  const [accountNumber,setAccountNumber]=useWorkspaceDraft('manual-accountNumber', '');
  const [amount, setAmount] = useWorkspaceDraft('manual-amount', '100.00');
  const [note, setNote] = useWorkspaceDraft('manual-note', '');
  const [cardId, setCardId] = useWorkspaceDraft('manual-cardId', '');
  const [action, setAction] = useWorkspaceDraft<'freeze' | 'unfreeze'>('manual-action', 'freeze');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    let active = true;
    api<Recipient[]>('/api/recipients').then((list) => { if (active) setRecipients(list); })
      .catch((err: unknown) => { if (active) setError(err instanceof Error ? err.message : '操作状态加载失败'); });
    return () => { active = false; };
  }, []);

  function retryOperation() {
    retry();
    api<Recipient[]>('/api/recipients').then((list) => { setRecipients(list); setError(''); })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : '收款人加载失败'));
  }
  async function write<T>(path: string, payload?: unknown): Promise<T> {
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    return api<T>(path, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token,
    }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
  }

  async function prepare(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!setBusy(true)) return; setError(''); setNotice('');
    try {
      const activeTaskId = task?.proposal?.status === 'pending_confirmation' ? task.id : undefined;
      if (kind === 'transfer') {
        const target = targetType === 'own_account'
          ? { type: 'own_account', account_id: targetId }
          : targetType === 'external_account'
            ? {type:'external_account',name:recipientName.trim(),bank_name:bankName.trim(),account_number:accountNumber.trim()}
            : { type: 'saved_recipient', recipient_id: targetId };
        const result = await write<{ proposal: Proposal }>('/api/transfers/prepare', {
          source_account_id: sourceId, target, amount_minor: minorFromYuan(amount), currency: 'CNY',
          note, ...(activeTaskId ? { task_id: activeTaskId } : {}),
        });
        await refreshTask(result.proposal.task_id);
      } else {
        const result = await write<{ proposal?: Proposal; already_in_state?: boolean }>(
          `/api/cards/${cardId}/actions/prepare`, { action, ...(activeTaskId ? { task_id: activeTaskId } : {}) });
        if (result.already_in_state) setNotice('卡片已处于目标状态，无需新建操作单。');
        else if (result.proposal) await refreshTask(result.proposal.task_id);
      }
    } catch (err) { try { await refreshTask(); } catch { /* Preserve the request error. */ } setError(err instanceof Error ? err.message : '准备操作单失败'); }
    finally { setBusy(false); }
  }

  async function confirm(proposal: Proposal) {
    if (!setBusy(true)) return; setError(''); setNotice('');
    try {
      const { confirmation_token } = await write<{ confirmation_token: string }>(
        `/api/proposals/${proposal.id}/confirmation-token`);
      await write(`/api/proposals/${proposal.id}/confirm`, { confirmation_token });
      await refreshTask(proposal.task_id);
      await onChanged();
    } catch (err) {
      // The response may be lost after commit. Recover the durable result first.
      try {
        const recovered = await refreshTask(proposal.task_id);
        if (recovered?.proposal?.status === 'executed') {
          setNotice('已从服务端恢复成功回执。');
          await onChanged();
          return;
        }
      } catch { /* Keep the original request error. */ }
      setError(err instanceof Error ? err.message : '确认失败，请先查询原操作单状态');
    } finally { setBusy(false); }
  }

  async function cancel(proposal: Proposal) {
    if (!setBusy(true)) return; setError(''); setNotice('');
    try {
      await write(`/api/proposals/${proposal.id}/cancel`);
      await refreshTask(proposal.task_id);
    } catch (err) { try { await refreshTask(proposal.task_id); } catch { /* Offer read-only recovery. */ } setError(err instanceof Error ? err.message : '取消失败'); }
    finally { setBusy(false); }
  }

  const activeProposal = task?.proposal?.id === agentProposalId ? null : task?.proposal;
  const sourceChoices = accounts.filter((item) => item.status === 'active');
  const targetChoices = targetType === 'own_account'
    ? accounts.filter((item) => ['active', 'frozen'].includes(item.status) && item.id !== sourceId).map((item) => ({ id: item.id, label: `${item.alias} ${item.account_mask}` }))
    : recipients.filter((item) => item.status === 'active').map((item) => ({ id: item.id, label: `${item.name} ${item.account_mask}${item.phone_mask ? ` · ${item.phone_mask}` : ''}` }));

  return <section className="section operation-section">
    <div className="section-heading"><div><p className="eyebrow">SIMULATED OPERATIONS</p><h2>模拟操作</h2></div><span>每次写入均需查看操作单并点击确认</span></div>
    <details className="manual-form"><summary>手动填写操作（可选）</summary><form className="operation-form" onSubmit={(event) => void prepare(event)}>
      <label>操作类型<select aria-label="操作类型" value={kind} onChange={(event) => setKind(event.target.value as 'transfer' | 'card_action')}>
        <option value="transfer">模拟转账</option><option value="card_action">卡片冻结 / 解冻</option>
      </select></label>
      {kind === 'transfer' ? <>
        <label>付款账户<select aria-label="付款账户" value={sourceId} onChange={(event) => { setSourceId(event.target.value); setTargetId(''); }} required>
          <option value="">请选择</option>{sourceChoices.map((item) => <option key={item.id} value={item.id}>{item.alias} {item.account_mask} · {money(item.balance_minor)}</option>)}
        </select></label>
        <label>目标类型<select aria-label="目标类型" value={targetType} onChange={(event) => { setTargetType(event.target.value as 'own_account' | 'saved_recipient' | 'external_account'); setTargetId(''); }}>
          <option value="own_account">本人账户</option><option value="saved_recipient">已保存收款人</option><option value="external_account">新收款人（直接填写）</option>
        </select></label>
        {targetType === 'external_account' ? <>
          <label>收款户名<input value={recipientName} onChange={event=>setRecipientName(event.target.value)} maxLength={80} required /></label>
          <label>开户银行<input value={bankName} onChange={event=>setBankName(event.target.value)} maxLength={80} required /></label>
          <label>收款账号<input value={accountNumber} onChange={event=>setAccountNumber(event.target.value)} maxLength={50} autoComplete="off" placeholder="完整账号，不是尾号" required /></label>
        </> : <label>收款目标<select aria-label="收款目标" value={targetId} onChange={(event) => setTargetId(event.target.value)} required>
          <option value="">请选择</option>{targetChoices.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select></label>}
        <label>金额（元）<input value={amount} onChange={(event) => setAmount(event.target.value)} inputMode="decimal" required /></label>
        <label>备注（可选）<input value={note} onChange={(event) => setNote(event.target.value)} maxLength={120} /></label>
      </> : <>
        <label>卡片<select aria-label="卡片" value={cardId} onChange={(event) => setCardId(event.target.value)} required>
          <option value="">请选择</option>{cards.map((item) => <option key={item.id} value={item.id}>{item.alias} {item.card_mask} · {item.status === 'frozen' ? '已冻结' : '正常'}</option>)}
        </select></label>
        <label>动作<select aria-label="动作" value={action} onChange={(event) => setAction(event.target.value as 'freeze' | 'unfreeze')}>
          <option value="freeze">冻结</option><option value="unfreeze">解冻</option>
        </select></label>
      </>}
      <button type="submit" disabled={unavailable}>准备操作单</button>
    </form></details>
    <p className="operation-note">演示限额：单笔 5,000 元、每付款账户每日 10,000 元。准备不会扣款；改动表单并重新准备会使当前待确认单失效。</p>
    {task?.proposal?.id === agentProposalId && <p className="operation-note">当前操作单已在<a href="#assistant">对话助手</a>中展示，请在那里核对和确认。</p>}
    <div className="task-sync"><span role="status">{busy ? '正在处理…' : loading ? '正在恢复操作状态…' : syncError ? '同步已暂停，确认操作暂不可用' : '操作状态已同步'}</span><button type="button" className="secondary" disabled={busy} onClick={retryOperation}>重新查询操作状态</button></div>
    {syncError && <div className="alert" role="alert">{syncError}</div>}
    {error && <div className="alert" role="alert">{error}</div>}
    {notice && <div className="operation-notice" role="status">{notice}</div>}
    {activeProposal && <article className="proposal-card">
      <div className="proposal-heading"><div><p className="eyebrow">CONFIRMATION</p><h3>{activeProposal.kind === 'transfer' ? '模拟转账操作单' : '卡片操作单'}</h3></div><span className="tag">{({ pending_confirmation: '待确认', executed: '已执行', cancelled: '已取消', expired: '已过期', invalidated: '已失效', failed: '未完成' } as Record<string, string>)[activeProposal.status] ?? activeProposal.status}</span></div>
      {activeProposal.kind === 'transfer' ? <>
        <p><strong>{activeProposal.summary.source?.alias} {activeProposal.summary.source?.mask}</strong> → <strong>{activeProposal.summary.target?.alias} {activeProposal.summary.target?.mask}</strong></p>
        {activeProposal.summary.target?.bank_name && <p>开户银行 {activeProposal.summary.target.bank_name}</p>}
      {activeProposal.summary.target?.account_number && <details><summary>核对完整收款账号</summary><p>{activeProposal.summary.target.account_number}</p></details>}
      {activeProposal.summary.target?.phone_mask && <p>收款人手机号 {activeProposal.summary.target.phone_mask}</p>}
        <p className="proposal-amount">{money(activeProposal.summary.amount_minor ?? 0)} · CNY</p>
        {activeProposal.summary.note && <p>备注：{activeProposal.summary.note}</p>}
      </> : <p><strong>{activeProposal.summary.card?.alias} {activeProposal.summary.card?.mask}</strong> · {activeProposal.summary.action === 'freeze' ? '冻结' : '解冻'}</p>}
      <p>{activeProposal.summary.impact}</p>
      <ConditionDetails condition={activeProposal.summary.condition} validation={activeProposal.validation} />
      <small>演示会话当前用户 · 到期时间 {new Date(activeProposal.expires_at_utc).toLocaleString('zh-CN')}</small>
      {activeProposal.status === 'pending_confirmation' && <div className="proposal-actions">
        <button type="button" disabled={unavailable} onClick={() => void confirm(activeProposal)}>核对并继续</button>
        <button type="button" className="secondary" disabled={unavailable} onClick={() => void cancel(activeProposal)}>取消操作单</button>
      </div>}
      {activeProposal.receipt && <div className="receipt" role="status">
        <strong>{activeProposal.receipt.message}</strong>
        {activeProposal.receipt.recipient_credit_minor !== undefined && <p>收款人模拟入账 {money(activeProposal.receipt.recipient_credit_minor)} · 入账编号 {activeProposal.receipt.recipient_credit_id}</p>}
        {activeProposal.receipt.amount_minor !== undefined && <p>金额 {money(activeProposal.receipt.amount_minor)} · 来源余额 {money(activeProposal.receipt.source_balance_minor ?? 0)}{activeProposal.receipt.target_balance_minor !== null && activeProposal.receipt.target_balance_minor !== undefined ? ` · 目标余额 ${money(activeProposal.receipt.target_balance_minor)}` : ''}</p>}
        {activeProposal.receipt.card_status && <p>当前卡状态：{activeProposal.receipt.card_status === 'frozen' ? '已冻结' : '正常'}</p>}
        <small>业务时间 {new Date(activeProposal.receipt.committed_at_utc).toLocaleString('zh-CN')} · 操作单 {activeProposal.id}</small>
      </div>}
    </article>}
  </section>;
}
