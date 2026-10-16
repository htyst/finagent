import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import './Birthdays.css';

type Item = { item_key: string; supplier: string; amount_minor: number };
type FundEvent = { id: string; kind: string; delta_minor: number; amount_minor: number; remaining_minor: number;
  created_at_utc: string; message?: string; transaction_id?: string | null; request_id?: string | null; order_id?: string | null };
type Plan = { id: string; birthday_date: string; birthday_source?: string; order_at_utc: string; delivery_date: string;
  budget_minor: number; account_id?: string; source_account_id?: string; account_alias?: string; recipient: string; address: string;
  item_keys: string[]; items?: Item[]; spent_minor: number; released_minor: number; reserved_minor: number;
  status: string; version: number; paused?: boolean; pending_request_id?: string | null; fund_events: FundEvent[] };
type Summary = { birthday_date: string; birthday_source?: string; order_at_utc: string; delivery_date: string;
  budget_minor: number; account_id?: string; source_account_id?: string; account_alias?: string; recipient: string; address: string;
  item_keys: string[]; items: Item[]; release_remainder: boolean; previous_budget_minor?: number;
  previous_reserved_minor?: number; new_reserved_minor?: number; reservation_delta_minor?: number;
  release_amount_minor?: number; item_key?: string };
type Request = { id: string; plan_id: string | null; kind: string; label?: string; status: string; summary: Summary;
  expires_at_utc?: string; completed_at_utc?: string | null; receipt: { message?: string; completed_at_utc?: string } | null };
type Receipt = { id: string; supplier: string; simulated: boolean; status: string; delivery_date: string;
  delivered_at_utc: string; recorded_at_utc: string };
type Order = { id: string; plan_id: string; item_key: string; supplier?: string; amount_minor: number;
  payment_status: string; status: string; transaction_id: string | null; delivery_receipt: Receipt | null;
  error_code?: string | null; message?: string; delivery_date?: string; address?: string; recipient?: string; attempt_count?: number };
type Account = { account_id: string; alias?: string; account_alias?: string; balance_minor: number; reserved_minor: number; available_minor: number };
type Notification = { id: string; plan_id: string; title: string; body: string; created_at_utc: string };
type Data = { rules: string; business_now_utc: string; accounts: Account[]; plans: Plan[]; requests: Request[]; orders: Order[]; notifications: Notification[] };
type Recovery = { requestId: string; status: string } | { orderId: string };

const money = (minor: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(minor / 100);
const local = (value: string | undefined | null) => value && Number.isFinite(new Date(value).getTime())
  ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '待确定';
function businessDate(value?: string) {
  if (!value || !Number.isFinite(new Date(value).getTime())) return '';
  const parts = new Intl.DateTimeFormat('en', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value));
  return ['year', 'month', 'day'].map(type => parts.find(part => part.type === type)?.value).join('-');
}
const itemLabel = (key: string) => ({ flowers: '鲜花', cake: '蛋糕' } as Record<string, string>)[key] || key;
const birthdaySource = (value?: string) => value === 'user_provided' ? '用户主动提供' : value || '用户提供';
const confirmationLabels: Record<string, string> = { create: '确认生日安排', change: '确认修改生日安排', cancel: '确认取消生日安排',
  cancel_item: '确认取消蛋糕部分', retry: '确认重试蛋糕' };
const requestLabels: Record<string, string> = { pending_confirmation: '待确认，尚未办理', executed: '已办理', cancelled: '草稿已取消',
  invalidated: '已失效', expired: '已过期' };
const planLabels: Record<string, string> = { scheduled: '已授权，等待下单', ordering: '正在下单', awaiting_delivery: '已下单，等待送达',
  partial_failure: '部分失败，等待处理', completed: '已完成', completed_with_changes: '变更后已完成', cancelled: '已取消', blocked: '执行受阻' };
const orderLabels: Record<string, string> = { accepted: '供应商已受理，尚未送达', rejected: '供应商拒单', cancelled: '已取消',
  delivered: '已收到送达回执', pending: '等待重试' };
const eventLabels: Record<string, string> = { reserve: '预留专款', spend: '专款支付', release: '释放专款' };

export default function Birthdays({ revision, onChanged, demo = false }: { demo?: boolean; revision: number; onChanged: () => Promise<void> }) {
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
      const next = await api<Data>('/api/birthdays', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) });
      if (!mounted.current || current !== sequence.current) return;
      setData(next); setLoading(false); setSyncError('');
      const signature = JSON.stringify(next.accounts.map(account => [account.account_id, account.balance_minor, account.reserved_minor, account.available_minor]).sort());
      if (financialState.current !== null && financialState.current !== signature) void changed.current().catch(() => {});
      financialState.current = signature;
      return next;
    } catch (e) {
      if (mounted.current && current === sequence.current) {
        setLoading(false); setSyncError(e instanceof Error ? e.message : '生日安排读取失败');
      }
    } finally { if (readController.current === controller) readController.current = null; }
  }
  useEffect(() => {
    mounted.current = true; void load();
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 2000);
    const resume = () => { if (!document.hidden) void load(true); };
    window.addEventListener('focus', resume); window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      mounted.current = false; sequence.current++; readController.current?.abort(); window.clearInterval(timer);
      window.removeEventListener('focus', resume); window.removeEventListener('online', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, []);
  useEffect(() => { void load(true); }, [revision]);

  async function post<T>(path: string, payload?: unknown): Promise<T> {
    if (!mounted.current) throw Error('会话已变化，请重新核对生日安排');
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    if (!mounted.current) throw Error('会话已变化，请重新核对生日安排');
    return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
  }
  async function act(work: () => Promise<string>, recovery?: Recovery) {
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
        if (failed && recovery && latest) {
          if ('requestId' in recovery) {
            const row = latest.requests.find(request => request.id === recovery.requestId);
            if (row?.status === recovery.status) { setError(''); setNotice(row.receipt?.message || '已读取持久回执，办理已完成。'); }
          } else {
            const order = latest.orders.find(item => item.id === recovery.orderId);
            if (order?.status === 'delivered' && order.delivery_receipt) { setError(''); setNotice('已核对持久送达回执。'); }
          }
        }
        if (mounted.current) { await changed.current().catch(() => {}); if (mounted.current) setBusy(false); }
      }
    }
  }
  async function confirm(request: Request): Promise<string> {
    const path = '/api/birthdays/requests/' + encodeURIComponent(request.id);
    const { confirmation_token } = await post<{ confirmation_token: string }>(path + '/confirmation-token');
    if (!mounted.current) throw Error('会话已变化，请重新核对生日安排');
    const completed = await post<Request>(path + '/confirm', { confirmation_token });
    return completed.receipt?.message || '已读取生日安排办理结果，请核对下方计划与资金。';
  }
  const unavailable = busy || loading || !data || !!syncError;
  const today = businessDate(data?.business_now_utc);
  return <section className="section birthday-management" id="birthdays">
    <div className="section-heading"><div><p className="eyebrow">BIRTHDAY PLANS</p><h2>生日安排</h2></div>
      <button type="button" className="secondary" disabled={busy} onClick={() => void load(true)}>刷新生日安排</button></div>
    <p className="operation-note">在对话中提供生日、用款账户、收货信息和预算，核对这里的固定办理单后确认。确认后由后台按约定时间下单，供应商送达回执到齐后按约定释放余款。</p>
    <p className="operation-note">{data?.rules}</p>
    <div className="task-sync"><span role="status">{busy ? '正在办理并核对结果…' : loading ? '正在读取生日安排…' : syncError ? '状态读取失败，确认已暂停' : '生日计划、订单与资金每 2 秒更新'}</span>
      <span>业务时间 {local(data?.business_now_utc)}（北京时间）</span></div>
    {syncError && <div className="alert" role="alert">{syncError} 请恢复最新状态后再确认。</div>}
    {error && <div className="alert" role="alert">{error}</div>}
    {notice && <p className="operation-notice" role="status">{notice}</p>}

    {!demo && <><div className="birthday-accounts"><h3>账户与生日专款</h3><p className="operation-note">账面余额包含已预留专款；普通支出使用一般可用资金。预留不扣减账面，实际付款才形成支出流水。</p>
      <div className="birthday-grid">{data?.accounts.map(account => <article className="birthday-funds" key={account.account_id} data-account-id={account.account_id}>
        <h4>{account.alias || account.account_alias || account.account_id}</h4><dl className="birthday-fund-values">
          <div><dt>账面余额</dt><dd>{money(account.balance_minor)}</dd></div><div><dt>一般可用</dt><dd>{money(account.available_minor)}</dd></div>
          <div><dt>生日专款</dt><dd>{money(account.reserved_minor)}</dd></div></dl>
      </article>)}</div></div>

    <div className="birthday-requests"><h3>固定办理单与回执</h3>
      {!loading && !data?.requests.length && <p>暂无生日办理单，请在对话中提出安排或变更需求。</p>}
      {data?.requests.map(request => <article className="birthday-request" key={request.id} data-request-id={request.id}>
        <div className="birthday-card-heading"><h4>{request.label || confirmationLabels[request.kind] || '生日办理单'}</h4><span className="tag">{requestLabels[request.status] || request.status}</span></div>
        <dl className="birthday-summary-grid"><div><dt>生日</dt><dd>{request.summary.birthday_date}<br /><span className="birthday-detail">来源：{birthdaySource(request.summary.birthday_source)}</span></dd></div>
          <div><dt>预算上限</dt><dd>{money(request.summary.budget_minor)}（含运费）</dd></div>
          <div><dt>下单时点</dt><dd>{local(request.summary.order_at_utc)}（北京时间）</dd></div><div><dt>约定送达日期</dt><dd>{request.summary.delivery_date}</dd></div>
          <div><dt>付款账户</dt><dd>{request.summary.account_alias || request.summary.account_id || request.summary.source_account_id}</dd></div>
          <div><dt>收货信息</dt><dd>{request.summary.recipient}<br />{request.summary.address}</dd></div>
          <div><dt>商品与含运费报价</dt><dd>{request.summary.items?.length ? request.summary.items.map(item => <div key={item.item_key}>{itemLabel(item.item_key)} · {money(item.amount_minor)} · {item.supplier}</div>) : request.summary.item_keys?.map(itemLabel).join('、')}</dd></div>
          <div><dt>余款处理</dt><dd>{request.summary.release_remainder ? '任务完成后释放回一般可用资金' : '按办理单约定处理'}</dd></div>
        </dl>
        {request.kind === 'change' && <p className="birthday-change">原预算 {money(request.summary.previous_budget_minor ?? 0)} → 新预算 {money(request.summary.budget_minor)}；
          专款 {money(request.summary.previous_reserved_minor ?? 0)} → {money(request.summary.new_reserved_minor ?? 0)}。
          {(request.summary.reservation_delta_minor ?? 0) < 0 ? `确认后释放 ${money(-(request.summary.reservation_delta_minor ?? 0))}。` : (request.summary.reservation_delta_minor ?? 0) > 0 ? `确认后追加预留 ${money(request.summary.reservation_delta_minor ?? 0)}。` : '专款金额不变。'}</p>}
        {request.kind === 'cancel' && <p className="birthday-change">本次取消整个生日安排，释放尚未使用的专款 {money(request.summary.release_amount_minor ?? request.summary.previous_reserved_minor ?? 0)}。请核对已执行订单的真实状态。</p>}
        {request.kind === 'cancel_item' && <p className="birthday-change">本次只取消{itemLabel(request.summary.item_key || 'cake')}部分，保留其余商品的订单与进度；剩余专款按任务完成后的约定释放。</p>}
        {request.kind === 'retry' && <p className="birthday-change">本次仅重试失败的{itemLabel(request.summary.item_key || 'cake')}订单，其余已成功订单不重复购买。</p>}
        {request.status === 'pending_confirmation' && <>
          <p>尚未办理。{request.expires_at_utc && <>确认有效至 {local(request.expires_at_utc)}（北京时间）。</>}</p>
          {confirmationLabels[request.kind] && <button type="button" disabled={unavailable} onClick={() => void act(() => confirm(request), { requestId: request.id, status: 'executed' })}>{confirmationLabels[request.kind]}</button>}
          <button type="button" className="secondary" disabled={unavailable} onClick={() => void act(async () => {
            await post('/api/birthdays/requests/' + encodeURIComponent(request.id) + '/cancel');
            return request.kind === 'create' ? '本次生日安排草稿已取消，尚未锁定资金。' : '本次变更草稿已取消，原授权安排已恢复。';
          }, { requestId: request.id, status: 'cancelled' })}>取消本次办理</button>
        </>}
        {request.receipt && <div className="birthday-receipt"><strong>{request.receipt.message || '已保存办理回执。'}</strong>
          {(request.receipt.completed_at_utc || request.completed_at_utc) && <p>办理时间 {local(request.receipt.completed_at_utc || request.completed_at_utc)}（北京时间）</p>}</div>}
        <small>办理编号 {request.id}</small>
      </article>)}
    </div>

    <div className="birthday-plans"><h3>已授权计划与资金记录</h3>
      <p className="operation-note">离开聊天页后仍由后台执行。后台服务需要保持运行；停机期间可能延后，恢复后会核对到期任务。到达生日或完成付款仍需等待供应商送达回执。</p>
      {!loading && !data?.plans.length && <p>暂无已授权的生日计划。</p>}
      {data?.plans.map(plan => <article className="birthday-plan" key={plan.id} data-plan-id={plan.id}>
        <div className="birthday-card-heading"><h4>{plan.recipient} · {plan.birthday_date} 生日安排</h4><span className={['completed', 'completed_with_changes'].includes(plan.status) ? 'tag good' : plan.status === 'partial_failure' || plan.status === 'blocked' ? 'tag warning' : 'tag'}>{planLabels[plan.status] || plan.status}</span></div>
        {plan.paused && <p className="birthday-change" role="status">变更办理单待确认，自动下单已暂停；取消变更草稿可恢复原授权安排。</p>}
        <p>下单 {local(plan.order_at_utc)}（北京时间） · 送达 {plan.delivery_date}</p><p>收货：{plan.recipient} · {plan.address}</p>
        <p>商品：{plan.item_keys.map(itemLabel).join('、')} · 总预算 {money(plan.budget_minor)}</p>
        <dl className="birthday-fund-values"><div><dt>实际支出</dt><dd>{money(plan.spent_minor)}</dd></div><div><dt>当前预算未使用</dt><dd>{money(plan.budget_minor - plan.spent_minor)}</dd></div><div><dt>剩余专款</dt><dd>{money(plan.reserved_minor)}</dd></div><div><dt>累计已释放回一般可用</dt><dd>{money(plan.released_minor)}</dd></div></dl>
        <p className="operation-note">当前预算未使用金额按现预算扣除实际支出计算。累计释放包含此前预算调整释放的资金和任务完成后释放的余款。</p>
        {!!plan.fund_events.length && <div className="analytics-table-wrap birthday-events"><table><caption>专款实际变动记录</caption><thead><tr><th>实际记录时间（北京时间）</th><th>操作与专款变动</th><th>操作后专款</th><th>核验依据</th></tr></thead>
          <tbody>{plan.fund_events.map(event => <tr key={event.id}><td>{local(event.created_at_utc)}</td><td>{eventLabels[event.kind] || event.kind}<br />{event.delta_minor > 0 ? '+' : event.delta_minor < 0 ? '−' : ''}{money(Math.abs(event.delta_minor))}</td><td>{money(event.remaining_minor)}</td><td>{event.message}{event.request_id && <small>关联办理 {event.request_id}</small>}{event.order_id && <small>关联订单 {event.order_id}</small>}{event.transaction_id && <small>关联流水 {event.transaction_id}</small>}</td></tr>)}</tbody></table></div>}
        <small>计划编号 {plan.id} · 版本 {plan.version}</small>
      </article>)}
    </div>

    </>}
    <div className="birthday-orders"><h3>商品订单、支付与送达</h3><p className="operation-note">这里使用本地模拟供应商。模拟送达在演示工具中录入，仅在约定送达日期到达后可用。</p>
      {!loading && !data?.orders.length && <p>尚未产生商品订单。</p>}
      <div className="birthday-grid">{data?.orders.map(order => {
        const plan = data.plans.find(item => item.id === order.plan_id), deliveryDate = order.delivery_date || plan?.delivery_date;
        const canDeliver = !!today && !!deliveryDate && today >= deliveryDate;
        return <article className="birthday-order" key={order.id} data-order-id={order.id}>
          <div className="birthday-card-heading"><h4>{itemLabel(order.item_key)} · {money(order.amount_minor)}</h4><span className={order.status === 'delivered' ? 'tag good' : order.status === 'rejected' ? 'tag warning' : 'tag'}>{orderLabels[order.status] || order.status}</span></div>
          <p>{order.supplier || '本地模拟供应商'} · <strong>{order.payment_status === 'paid' ? '已支付' : '未支付'}</strong></p>
          <p>约定送达 {deliveryDate || '待确定'} · {order.recipient || plan?.recipient} · {order.address || plan?.address}</p>
          {order.message && <p>{order.message}</p>}
          {order.status === 'rejected' && <p className="birthday-change">此订单未完成，请在对话中选择重试或取消蛋糕部分。其余订单按下方真实进度继续。</p>}
          {order.transaction_id && <small>支付流水 {order.transaction_id}</small>}
          {demo && order.status === 'accepted' && order.payment_status === 'paid' && <>
            <button type="button" className="secondary" disabled={unavailable || !canDeliver} onClick={() => void act(async () => {
              await post<Order>('/api/birthdays/orders/' + encodeURIComponent(order.id) + '/simulate-delivery');
              return '已录入' + itemLabel(order.item_key) + '的本地模拟供应商送达回执，请核对计划与余款。';
            }, { orderId: order.id })}>{'模拟' + itemLabel(order.item_key) + '送达'}</button>
            {!canDeliver && <p className="operation-note">约定送达日期尚未到达，等待供应商回执。</p>}
          </>}
          {order.delivery_receipt && <div className="birthday-receipt"><strong>{order.delivery_receipt.simulated ? '本地模拟供应商送达回执' : '供应商送达回执'}</strong>
            <p>{order.delivery_receipt.supplier} · 送达 {local(order.delivery_receipt.delivered_at_utc)}（北京时间）</p><small>回执编号 {order.delivery_receipt.id}</small></div>}
          <small>订单编号 {order.id}{order.attempt_count ? ' · 尝试次数 ' + order.attempt_count : ''}</small>
        </article>;
      })}</div>
    </div>

    <div className="birthday-notifications" role="region" aria-label="生日安排站内通知"><h3>生日安排站内通知</h3>
      {!loading && !data?.notifications.length && <p>暂无生日安排通知。</p>}
      {data?.notifications.map(notification => <article className="birthday-notification" key={notification.id} data-notification-id={notification.id}>
        <h4>{notification.title}</h4><p>{notification.body}</p><small>{local(notification.created_at_utc)}（北京时间）</small>
      </article>)}
    </div>
  </section>;
}
