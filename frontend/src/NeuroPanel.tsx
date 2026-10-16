import { useEffect, useState } from 'react';
import { api } from './api';
type Confirmation = { id: string; status: string; summary: string };
type Notification = { id: string; period: string; status: string; message: string };
type Order = { id: string; status: string; confirmation: Confirmation | null };
export default function NeuroPanel({ traceId, onChanged }: { traceId?: string | null; onChanged: () => Promise<void> }) {
  const [clock, setClock] = useState('');
  const [orders, setOrders] = useState<Order[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [trace, setTrace] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  async function write<T>(path: string, data?: unknown): Promise<T> {
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token }, body: JSON.stringify(data ?? {}) });
  }
  async function load() {
    setOrders(await api<Order[]>('/api/standing-orders'));
    setNotifications(await api<Notification[]>('/api/standing-orders/notifications'));
    try { setClock((await api<{ now: string }>('/api/debug/clock')).now); } catch { setClock(''); }
  }
  useEffect(() => { void load().catch(() => {}); }, [traceId]);
  async function action(fn: () => Promise<unknown>) {
    setBusy(true); setNotice('');
    try { await fn(); await load(); await onChanged(); } catch (e) { setNotice(e instanceof Error ? e.message : '操作未完成'); } finally { setBusy(false); }
  }
  async function confirm(c: Confirmation) {
    const { confirmation_token } = await write<{ confirmation_token: string }>(`/api/standing-confirmations/${c.id}/confirmation-token`);
    await write(`/api/standing-confirmations/${c.id}/confirm`, { confirmation_token });
  }
  return <details className="proposal-card"><summary>调试日历 定期转账与验证轨迹</summary>
    {clock && <p>模拟时间 {new Date(clock).toLocaleString('zh-CN')} <button disabled={busy} onClick={() => void action(async () => { const r = await write<{ notifications: Array<{ message: string }> }>('/api/debug/clock/advance', { days: 1 }); setNotice(r.notifications.map(n => n.message).join('；')); })}>推进一天</button></p>}
    <button disabled={busy} onClick={() => void action(async () => { const r = await write<Array<{ message: string }>>('/api/standing-orders/tick'); setNotice(r.map(n => n.message).join('；') || '当前没有新的到期指令'); })}>检查到期指令</button>
    {orders.map(order => <article key={order.id}><p>定期转账状态 {order.status}</p>
      {order.confirmation && <><p>{order.confirmation.summary}</p>{order.confirmation.status === 'pending' && <button disabled={busy} onClick={() => void action(() => confirm(order.confirmation!))}>确认长期指令</button>}</>}
      {order.status === 'active' && <button disabled={busy} onClick={() => void action(() => write(`/api/standing-orders/${order.id}/pause`))}>暂停</button>}
      {order.status === 'paused' && <button disabled={busy} onClick={() => void action(() => write(`/api/standing-orders/${order.id}/resume`))}>恢复</button>}
      {['active', 'paused'].includes(order.status) && <button disabled={busy} onClick={() => void action(() => write(`/api/standing-orders/${order.id}/cancel`))}>准备取消</button>}
    </article>)}
    {notifications.map(n => <article key={n.id}><p>{n.period} · {n.status} · {n.message}</p>{['pending_confirmation', 'awaiting_confirmation', 'expired'].includes(n.status) && <button disabled={busy} onClick={() => void action(() => write(`/api/standing-occurrences/${n.id}/claim`))}>查看并核对本期操作单</button>}</article>)}
    {traceId && <button disabled={busy} onClick={() => void action(async () => setTrace(await api(`/api/debug/trace/${traceId}`)))}>查看本次验证轨迹</button>}
    {trace !== null && <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(trace, null, 2)}</pre>}
    {notice && <p role="status">{notice}</p>}
  </details>;
}
