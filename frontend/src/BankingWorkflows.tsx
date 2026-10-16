import { useEffect, useRef, useState } from 'react';
import { api } from './api';

type Receipt = { message?: string; amount_minor?: number; recipient_credit_id?: string; transaction_id?: string; business_id?: string; committed_at_utc?: string };
type Schedule = { id: string; status: string; execute_at_utc: string; execute_at_local: string; timezone: string; error_code: string | null;
  summary: { source: { alias: string; mask: string }; target: { alias: string; mask: string }; amount_minor: number; impact: string }; receipt: Receipt | null };
type Member = { id: string; name: string; account_mask: string | null; is_self: boolean; share_minor: number; due_minor: number; paid_minor: number; status: string; receipt: Receipt | null };
type Collection = { id: string; status: string; title: string; total_minor: number; self_share_minor: number; expected_minor: number; received_minor: number; remaining_minor: number;
  destination_account: { alias: string; account_mask: string }; members: Member[]; remainder_rule: string; payment_channel_label: string };
const money = (minor: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(minor / 100);
const labels: Record<string, string> = { pending_confirmation: '待确认', scheduled: '已预约', executed: '已执行', collecting: '收款中', completed: '已收齐', cancelled: '已取消', expired: '已过期', failed: '执行失败', unpaid: '未付款', paid: '已付款', self_share: '本人承担' };
const localTime = (utc: string) => new Date(utc).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });

export default function BankingWorkflows({ revision, onChanged, demo = false }: { demo?: boolean; revision: number; onChanged: () => Promise<void> }) {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [syncError, setSyncError] = useState('');
  const [notice, setNotice] = useState('');
  const [payment, setPayment] = useState<{ collectionId: string; memberId: string } | null>(null);
  const mounted = useRef(true);
  const sequence = useRef(0);
  const writing = useRef(false);
  const lastFinancialState = useRef<string | null>(null);
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  async function load() {
    if (writing.current) return;
    const request = ++sequence.current;
    try {
      const [nextSchedules, nextCollections] = await Promise.all([api<Schedule[]>('/api/schedules'), api<Collection[]>('/api/collections')]);
      if (!mounted.current || request !== sequence.current) return;
      setSchedules(nextSchedules); setCollections(nextCollections); setSyncError(''); setLoading(false);
      const signature = JSON.stringify([nextSchedules.filter(s => s.status === 'executed').map(s => s.id).sort(), nextCollections.map(c => [c.id, c.received_minor]).sort()]);
      if (lastFinancialState.current !== null && lastFinancialState.current !== signature) void onChangedRef.current();
      lastFinancialState.current = signature;
    } catch (err) {
      if (mounted.current && request === sequence.current) { setSyncError(err instanceof Error ? err.message : '预约与收款状态读取失败'); setLoading(false); }
    }
  }
  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = window.setInterval(() => void load(), 2000);
    return () => { mounted.current = false; sequence.current++; window.clearInterval(timer); };
  }, []);
  useEffect(() => { void load(); }, [revision]);

  async function post<T>(path: string, payload?: unknown): Promise<T> {
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
  }
  async function act(action: () => Promise<void>, success: string) {
    if (writing.current) return;
    writing.current = true; sequence.current++; setBusy(true); setError(''); setNotice('');
    try { await action(); if (mounted.current) { setNotice(success); setPayment(null); } }
    catch (err) { if (mounted.current) setNotice('请核对下面的持久状态；请求结果可能已写入。'); if (mounted.current) setError(err instanceof Error ? err.message : '操作失败'); }
    finally {
      writing.current = false;
      if (mounted.current) { setBusy(false); await load(); await onChangedRef.current(); }
    }
  }
  const unavailable = busy || loading || !!syncError;
  return <section className="section workflow-section" id="banking-workflows">
    <div className="section-heading"><div><p className="eyebrow">SCHEDULES & AA COLLECTIONS</p><h2>预约与 AA 收款</h2></div><span>离开对话后仍可查看</span></div>
    <p className="operation-note">在对话中提出预约转账或 AA 收款，核对这里的明细后用按钮确认。预约按北京时间执行；启动器和后台服务需要保持运行，服务重启后会补执行已到期的预约。</p>
    <div className="task-sync"><span role="status">{busy ? '正在处理…' : loading ? '正在读取…' : syncError ? '状态读取或操作失败' : '每 2 秒更新状态'}</span><button type="button" className="secondary" disabled={busy} onClick={() => void load()}>刷新预约与收款</button></div>
    {syncError && <p className="alert" role="alert">{syncError} 请恢复状态后再操作。</p>}
    {error && <p className="alert" role="alert">{error}</p>}{notice && <p className="operation-notice" role="status">{notice}</p>}
    {!loading && !schedules.length && !collections.length && <p className="operation-note">还没有预约或 AA 收款请求。</p>}
    {!demo && schedules.map(schedule => <article className="proposal-card" key={schedule.id} data-schedule-id={schedule.id}>
      <div className="proposal-heading"><h3>预约转账</h3><span className="tag">{labels[schedule.status] ?? schedule.status}</span></div>
      <p>{schedule.summary.source.alias} {schedule.summary.source.mask} → {schedule.summary.target.alias} {schedule.summary.target.mask}</p>
      <p className="proposal-amount">{money(schedule.summary.amount_minor)}</p>
      <p><strong>执行时间：{localTime(schedule.execute_at_utc)}（北京时间 UTC+8）</strong></p><p>{schedule.summary.impact}</p>
      {schedule.status === 'scheduled' && <p className="operation-note">已授权预约；到点后后台自动执行一次，无需再次确认。后台每秒检查，忙碌或停止运行期间可能延迟。</p>}
      {schedule.status === 'pending_confirmation' && <button type="button" disabled={unavailable} onClick={() => void act(async () => {
        const { confirmation_token } = await post<{ confirmation_token: string }>(`/api/schedules/${schedule.id}/token`);
        await post(`/api/schedules/${schedule.id}/confirm`, { confirmation_token });
      }, '预约已授权，请核对持久状态。')}>确认预约</button>}
      {['pending_confirmation', 'scheduled'].includes(schedule.status) && <button type="button" className="secondary" disabled={unavailable} onClick={() => void act(async () => { await post(`/api/schedules/${schedule.id}/cancel`); }, '已核对取消结果。')}>取消预约</button>}
      {schedule.error_code && <p className="alert">执行失败：{schedule.error_code}，未完成转账。</p>}
      {schedule.receipt && <div className="receipt"><strong>{schedule.receipt.message}</strong><p>收款人模拟入账编号 {schedule.receipt.recipient_credit_id}</p><small>预约编号 {schedule.id}</small></div>}
    </article>)}
    {collections.map(collection => <article className="proposal-card" key={collection.id} data-collection-id={collection.id}>
      <div className="proposal-heading"><h3>{collection.title}</h3><span className="tag">{labels[collection.status] ?? collection.status}</span></div>
      <p>总额 {money(collection.total_minor)} · 本人承担 {money(collection.self_share_minor)} · 向其他成员收取 {money(collection.expected_minor)}</p>
      <p>收款账户：{collection.destination_account.alias} {collection.destination_account.account_mask}</p>
      <p><strong>已收 {money(collection.received_minor)} · 待收 {money(collection.remaining_minor)}</strong></p>
      <p className="operation-note">平均分摊，以分为单位；不能整除时按本人、成员顺序分配余数。</p>
      <div className="collection-members">{collection.members.map(member => <div className="collection-member" key={member.id} data-member-id={member.id}>
        <div><strong>{member.name} {member.account_mask}</strong><p>应分摊 {money(member.share_minor)} · {labels[member.status] ?? member.status}</p>
          {member.is_self ? <small>本人份额不扣款、不发起收款。</small> : <small>应收 {money(member.due_minor)} · 已收 {money(member.paid_minor)}</small>}
          {member.receipt && <p className="receipt">已入账：{member.receipt.transaction_id ?? member.receipt.business_id}</p>}
        </div>
        {demo && collection.status === 'collecting' && member.status === 'unpaid' && <div>
          {payment?.collectionId === collection.id && payment.memberId === member.id ? <>
            <p>确认模拟 {member.name} 支付 {money(member.due_minor)}？此入口会实际增加本人的演示账户余额。</p>
            <button type="button" disabled={unavailable} onClick={() => void act(async () => {
              const { payment_token } = await post<{ payment_token: string }>(`/api/collections/${collection.id}/members/${member.id}/payment-token`);
              await post(`/api/collections/${collection.id}/members/${member.id}/simulate-payment`, { payment_token, expected_amount_minor: member.due_minor, simulated: true });
            }, '模拟付款结果已读取，请核对成员状态和账户流水。')}>确认模拟付款</button>
            <button type="button" className="secondary" disabled={busy} onClick={() => setPayment(null)}>返回</button>
          </> : <button type="button" className="secondary" disabled={unavailable} onClick={() => setPayment({ collectionId: collection.id, memberId: member.id })}>模拟 {member.name} 付款</button>}
        </div>}
      </div>)}</div>
      <p className="operation-note">成员付款模拟入口位于演示工具；未连接真实银行或成员支付账户。</p>
      {!demo && collection.status === 'pending_confirmation' && <button type="button" disabled={unavailable} onClick={() => void act(async () => {
        const { confirmation_token } = await post<{ confirmation_token: string }>(`/api/collections/${collection.id}/confirmation-token`);
        await post(`/api/collections/${collection.id}/confirm`, { confirmation_token });
      }, 'AA 收款已发起；发起本身不会入账。')}>确认发起 AA 收款</button>}
      {!demo && ['pending_confirmation', 'collecting'].includes(collection.status) && <button type="button" className="secondary" disabled={unavailable} onClick={() => void act(async () => { await post(`/api/collections/${collection.id}/cancel`); }, '已核对取消结果；已经收到的款项保留。')}>取消 AA 收款</button>}
      <small>收款请求编号 {collection.id}</small>
    </article>)}
  </section>;
}
