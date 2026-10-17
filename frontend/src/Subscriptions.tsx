import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import './Subscriptions.css';
import ImportedSubscriptions, {type ImportedData} from './ImportedSubscriptions';

type Evidence = { id: string; merchant: string; amount_minor: number; posted_at_utc: string };
type Candidate = {
  id: string; merchant: string; amount_minor: number; period: string; period_label: string;
  next_charge_at_utc: string; timezone: string; suspected: boolean; authorization_id: string | null; evidence: Evidence[];
};
type Authorization = {
  id: string; account_id: string; merchant: string; amount_minor: number; period: string; charge_day: number;
  next_charge_at_utc: string; timezone: string; status: string; version: number; revoked_at_utc: string | null; payment_card_id?: string | null; background_execution_authorized?: boolean; background_remaining?: number; background_authorization_expires_at?: string | null;
};
type Reminder = {
  id: string; merchant: string; amount_minor: number; charge_at_utc: string; fire_at_utc: string;
  timezone: string; status: string; delivered_at_utc: string | null;
};
type Notification = { id: string; title: string; body: string; scheduled_at_utc: string; delivered_at_utc: string; recorded_at_utc: string };
type Request = {
  id: string; kind: string; label: string; status: string; expires_at_utc: string; completed_at_utc: string | null;
  summary: { merchant: string; amount_minor: number; next_charge_at_utc: string; fire_at_utc?: string;
    days_before?: number; hour?: number; minute?: number; timezone: string; scope?: string };
  receipt: { message: string; completed_at_utc?: string; next_charge_at_utc?: string } | null;
};
type Charge = {
  id: string; merchant: string; amount_minor: number; due_at_utc: string; status: string;
  error_code: string | null; transaction_id: string | null; balance_minor: number; message?: string;
};
type Data = { imported?: ImportedData; rules: string; candidates: Candidate[]; authorizations: Authorization[]; reminders: Reminder[];
  notifications: Notification[]; requests: Request[]; charges: Charge[] };

const money = (minor: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(minor / 100);
const local = (value: string | undefined | null) => value
  ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '待确定';
const requestLabels: Record<string, string> = {
  pending_confirmation: '待确认，尚未办理', executed: '已办理', cancelled: '已取消', invalidated: '已失效', expired: '已过期',
};
const reminderLabels: Record<string, string> = { scheduled: '已设置，等待提醒', delivered: '已送达站内收件箱', cancelled: '已取消' };
const chargeMessage = (charge: Charge) => charge.message || ({
  posted: '模拟代扣已入账', blocked: '银行代扣已解除，本次未扣款', failed: '本次代扣未完成，未扣款',
} as Record<string, string>)[charge.status] || '请核对本次扣费记录';

export default function Subscriptions({ revision, onChanged }: { revision: number; onChanged: () => Promise<void> }) {
  const [data, setData] = useState<Data | null>(null);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [syncError, setSyncError] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const mounted = useRef(false), sequence = useRef(0), writing = useRef(false), readController = useRef<AbortController | null>(null);
  const changed = useRef(onChanged), financialState = useRef<string | null>(null);
  changed.current = onChanged;

  async function load(force = false): Promise<Data | undefined> {
    if (!mounted.current || writing.current) return;
    if (readController.current) {
      if (!force) return;
      readController.current.abort();
    }
    const controller = new AbortController(), current = ++sequence.current;
    readController.current = controller;
    try {
      const next = await api<Data>('/api/subscriptions', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
      if (!mounted.current || current !== sequence.current) return;
      setData(next); setLoading(false); setSyncError('');
      const signature = JSON.stringify(next.charges.filter(c => c.status === 'posted').map(c => c.id).sort());
      if (financialState.current !== null && financialState.current !== signature) void changed.current().catch(() => {});
      financialState.current = signature;
      return next;
    } catch (e) {
      if (mounted.current && current === sequence.current) {
        setLoading(false); setSyncError(e instanceof Error ? e.message : '订阅状态读取失败');
      }
    } finally { if (readController.current === controller) readController.current = null; }
  }
  useEffect(() => {
    mounted.current = true; void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 2000);
    const resume = () => { if (!document.hidden) void load(true); };
    window.addEventListener('focus', resume);
    window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      mounted.current = false; sequence.current++; readController.current?.abort();
      window.clearInterval(timer); window.removeEventListener('focus', resume); window.removeEventListener('online', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, []);
  useEffect(() => { void load(true); }, [revision]);

  async function post<T>(path: string, payload?: unknown): Promise<T> {
    if (!mounted.current) throw Error('会话已变化，请重新核对办理记录');
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    if (!mounted.current) throw Error('会话已变化，请重新核对办理记录');
    return api<T>(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
  }
  async function act(work: () => Promise<string>, requestId?: string) {
    if (writing.current || !mounted.current) return;
    writing.current = true; sequence.current++; readController.current?.abort(); setBusy(true); setError(''); setNotice('');
    let failed = false;
    try { const message = await work(); if (mounted.current) setNotice(message); }
    catch (e) {
      failed = true;
      if (mounted.current) {
        setError(e instanceof Error ? e.message : '办理响应未完整收到');
        setNotice('正在读取持久记录核对结果，请按下方最新状态继续。');
      }
    } finally {
      writing.current = false;
      if (mounted.current) {
        const latest = await load(true);
        const recovered = requestId && latest?.requests.find(r => r.id === requestId);
        if (failed && recovered && recovered.status === 'executed') {
          setError(''); setNotice(recovered.receipt?.message || '已读取持久回执，办理已完成。');
        }
        if (mounted.current) {
          await changed.current().catch(() => {});
          if (mounted.current) setBusy(false);
        }
      }
    }
  }
  async function confirm(request: Request): Promise<string> {
    const id = encodeURIComponent(request.id);
    const { confirmation_token } = await post<{ confirmation_token: string }>('/api/subscriptions/requests/' + id + '/confirmation-token');
    if (!mounted.current) throw Error('会话已变化，请重新核对办理记录');
    const completed = await post<Request>('/api/subscriptions/requests/' + id + '/confirm', { confirmation_token });
    return completed.receipt?.message || '已读取办理结果，请核对下方记录。';
  }
  const unavailable = busy || loading || !data || !!syncError;
  return <section className="section subscription-management" id="subscriptions">
    <div className="section-heading"><div><p className="eyebrow">SUBSCRIPTIONS & DIRECT DEBITS</p><h2>订阅与银行代扣</h2></div>
      <button type="button" className="secondary" disabled={busy} onClick={() => void load(true)}>刷新订阅记录</button></div>
    <p className="operation-note">在对话中识别疑似订阅、设置提醒或提出解除代扣，核对这里的固定办理单后确认。候选来自重复流水，尚不能据此确定商户订阅合同。</p>
    <p className="operation-note">{data?.rules}</p>
    <div className="task-sync"><span role="status">{busy ? '正在办理并核对结果…' : loading ? '正在读取订阅记录…' : syncError ? '状态读取失败，确认已暂停' : '订阅和站内通知每 2 秒更新'}</span><span>时间均为北京时间</span></div>
    {syncError && <div className="alert" role="alert">{syncError} 请恢复最新状态后再确认。</div>}
    {syncError && <div><p>黄色查询范围：本人疑似订阅、代扣授权、提醒、扣费记录及站内通知；确认有效5分钟。</p><button disabled={busy} onClick={() => void act(async () => { await post('/api/subscriptions/query-confirmation'); return '订阅查询已确认'; })}>明确确认订阅查询</button></div>}
    {error && <div className="alert" role="alert">{error}</div>}
    {notice && <p className="operation-notice" role="status">{notice}</p>}

    <ImportedSubscriptions data={data?.imported} refreshKey={JSON.stringify(data?.charges.map(c=>[c.id,c.status]))} />
    <div className="subscription-candidates"><h3>流水识别的疑似订阅</h3>
      {!loading && !data?.candidates.length && <p className="operation-note">还没有识别记录。在对话中提出识别需求后，这里会展示候选和逐笔依据。</p>}
      <div className="subscription-grid">{data?.candidates.map(candidate => <article className="subscription-candidate" key={candidate.id} data-candidate-id={candidate.id}>
        <div className="subscription-card-heading"><h4>{candidate.merchant}</h4><span className="tag warning">疑似订阅</span></div>
        <p className="subscription-amount">{money(candidate.amount_minor)} <span>· {candidate.period_label || '周期待核实'}</span></p>
        <p>预计下一次扣费 {local(candidate.next_charge_at_utc)}（北京时间）</p>
        <p className="operation-note">根据以下重复流水推测；实际续费条件需核对商户合同。</p>
        <div className="analytics-table-wrap subscription-evidence"><table><caption>重复交易依据</caption><thead><tr><th>交易时间（北京时间）</th><th>商户与金额</th><th>流水编号</th></tr></thead>
          <tbody>{candidate.evidence.map(evidence => <tr key={evidence.id}><td>{local(evidence.posted_at_utc)}</td><td>{evidence.merchant} · {money(evidence.amount_minor)}</td><td>{evidence.id}</td></tr>)}</tbody></table></div>
      </article>)}</div>
    </div>

    <div className="subscription-authorizations"><h3>银行代扣授权</h3>
      <p className="operation-note">这里显示银行侧授权。解除后阻止此授权的后续模拟代扣；商户会员、订阅合同及其他付款渠道需要另行处理。</p>
      {!loading && !data?.authorizations.length && <p>暂无可管理的银行代扣授权。</p>}
      <div className="subscription-grid">{data?.authorizations.map(authorization => <article className="subscription-authorization" key={authorization.id} data-authorization-id={authorization.id}>
        <div className="subscription-card-heading"><h4>{authorization.merchant}</h4><span className={authorization.status === 'active' ? 'tag good' : 'tag'}>{authorization.status === 'active' ? '银行代扣有效' : '银行代扣已解除'}</span></div>
        <p>{money(authorization.amount_minor)} · {authorization.period === 'monthly' ? '每月' : authorization.period}</p>
        <p>下一扣费时点 {local(authorization.next_charge_at_utc)}（北京时间）</p><p>后台执行：{authorization.background_execution_authorized ? `已明确授权，剩余 ${authorization.background_remaining ?? 0} 次` : '尚未明确授权'}{authorization.payment_card_id && ` · 绑定卡 ${authorization.payment_card_id}`}</p>
          <p>扣费账户编号：{authorization.account_id}；每月{authorization.charge_day}日09:00（不足该日则为月末）</p>
        {authorization.status === 'active' && <><p>后台专项授权范围：此账户、此商户、固定金额，每月一次，最多12次且366天内有效；撤销代扣将停止执行。</p><button disabled={unavailable} onClick={() => void act(async () => { await post(`/api/subscriptions/authorizations/${authorization.id}/authorize-background`, { expected_version: authorization.version }); return '已确认此固定范围的模拟后台代扣授权'; })}>确认授权此范围内的后台代扣</button></>}
        {authorization.revoked_at_utc && <p>解除时间 {local(authorization.revoked_at_utc)}（北京时间）</p>}
      </article>)}</div>
    </div>

    <div className="subscription-requests"><h3>固定办理单与回执</h3>
      {!loading && !data?.requests.length && <p>暂无提醒或解除代扣办理单。</p>}
      {data?.requests.map(request => <article className="subscription-request" key={request.id} data-request-id={request.id}>
        <div className="subscription-card-heading"><h4>{request.label}</h4><span className="tag">{requestLabels[request.status] || request.status}</span></div>
        <dl className="subscription-summary-grid"><div><dt>商户</dt><dd>{request.summary.merchant}</dd></div><div><dt>预计续费金额</dt><dd>{money(request.summary.amount_minor)}</dd></div>
          <div><dt>下一扣费时点</dt><dd>{local(request.summary.next_charge_at_utc)}（北京时间）</dd></div>
          {request.kind === 'reminder' && <div><dt>提醒时间</dt><dd>{local(request.summary.fire_at_utc)}（北京时间）<br />续费前 {request.summary.days_before} 天 {String(request.summary.hour ?? 9).padStart(2, '0')}:{String(request.summary.minute ?? 0).padStart(2, '0')}</dd></div>}
          {request.kind === 'revoke' && <div><dt>本次办理范围</dt><dd>仅解除此商户的银行代扣授权</dd></div>}
        </dl>
        {request.kind === 'reminder' && <p className="operation-note">确认后在此站内收件箱接收提醒；取消提醒不改变银行代扣授权。</p>}
        {request.kind === 'revoke' && <p className="operation-note">这次办理不终止商户会员或订阅合同。其他商户授权保持原状态，商户自动续费需另行核对。</p>}
        {request.status === 'pending_confirmation' && <><p>尚未办理，确认有效至 {local(request.expires_at_utc)}（北京时间）。</p>
          {['reminder', 'revoke'].includes(request.kind) && <button type="button" disabled={unavailable} onClick={() => void act(() => confirm(request), request.id)}>{request.kind === 'reminder' ? '确认续费提醒' : '确认取消代扣'}</button>}
          <button type="button" className="secondary" disabled={unavailable} onClick={() => void act(async () => {
            await post('/api/subscriptions/requests/' + encodeURIComponent(request.id) + '/cancel'); return '本次办理单已取消。';
          })}>取消本次办理</button></>}
        {request.receipt && <div className="subscription-receipt"><strong>{request.receipt.message}</strong>
          {(request.receipt.completed_at_utc || request.completed_at_utc) && <p>完成时间 {local(request.receipt.completed_at_utc || request.completed_at_utc)}（北京时间）</p>}</div>}
        <small>办理编号 {request.id}</small>
      </article>)}
    </div>

    <div className="subscription-reminders"><h3>已设置的续费提醒</h3>
      <p className="operation-note">离开对话后仍会生成站内通知。后台服务需要保持运行；停止期间可能延后，重新启动后会补发到期提醒。</p>
      {!loading && !data?.reminders.length && <p>暂无已设置的提醒。</p>}
      {data?.reminders.map(reminder => <article className="subscription-reminder" key={reminder.id} data-reminder-id={reminder.id}>
        <div className="subscription-card-heading"><h4>{reminder.merchant} · {money(reminder.amount_minor)}</h4><span className="tag">{reminderLabels[reminder.status] || reminder.status}</span></div>
        <p>提醒时间 {local(reminder.fire_at_utc)}（北京时间） · 预计扣费 {local(reminder.charge_at_utc)}（北京时间）</p>
        {reminder.delivered_at_utc && <p>送达时间 {local(reminder.delivered_at_utc)}（北京时间）</p>}
        {reminder.status === 'scheduled' && <button type="button" className="secondary" disabled={unavailable} onClick={() => void act(async () => {
          await post('/api/subscriptions/reminders/' + encodeURIComponent(reminder.id) + '/cancel'); return '续费提醒已取消，银行代扣授权保持原状态。';
        })}>取消续费提醒</button>}
      </article>)}
    </div>

    <div className="subscription-notifications subscription-inbox" role="region" aria-label="续费提醒站内收件箱"><h3>站内通知收件箱</h3>
      {!loading && !data?.notifications.length && <p>暂无续费通知。</p>}
      {data?.notifications.map(notification => <article className="subscription-notification" key={notification.id} data-notification-id={notification.id}>
        <h4>{notification.title}</h4><p>{notification.body}</p>
        <small>计划提醒 {local(notification.scheduled_at_utc)}（北京时间） · 实际送达 {local(notification.delivered_at_utc)}（北京时间）</small>
      </article>)}
    </div>

    <div className="subscription-charges"><h3>到期扣费结果</h3>
      {!loading && !data?.charges.length && <p>暂无到期扣费记录。</p>}
      {data?.charges.map(charge => <article className="subscription-charge" key={charge.id} data-charge-id={charge.id}>
        <h4>{charge.merchant} · {money(charge.amount_minor)}</h4><p><strong>{chargeMessage(charge)}</strong></p>
        <p>扣费时点 {local(charge.due_at_utc)}（北京时间） · 处理后账户余额 {money(charge.balance_minor)}</p>
        {charge.transaction_id && <small>关联流水 {charge.transaction_id}</small>}
      </article>)}
    </div>
  </section>;
}
