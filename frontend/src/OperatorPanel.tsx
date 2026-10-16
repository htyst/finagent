import { useState } from 'react';
import { api } from './api';

type Ticket = { id: string; owner_id: string; plan_version: number; control: { reason?: string; suggestion?: string } };
export default function OperatorPanel() {
  const [key, setKey] = useState(''), [note, setNote] = useState('');
  const [tickets, setTickets] = useState<Ticket[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  async function load() {
    setBusy(true); setError('');
    try { setTickets(await api<Ticket[]>('/api/operator/queue', { headers: { 'X-FinAgent-Operator': key } })); }
    catch (e) { setError(e instanceof Error ? e.message : '队列读取失败'); }
    finally { setBusy(false); }
  }
  async function act(id: string, action: string) {
    setBusy(true); setError('');
    try {
      const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
      await api(`/api/operator/tasks/${id}`, { method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token, 'X-FinAgent-Operator': key,
      }, body: JSON.stringify({ action, reason: note }) });
      setNote(''); await load();
    } catch (e) { setError(e instanceof Error ? e.message : '接管处理失败'); }
    finally { setBusy(false); }
  }
  return <section className="section"><details><summary>本机模拟坐席队列</summary>
    <p>只提供澄清建议、终止或恢复检查点。坐席不能替用户确认转账、申购或强验证。凭据由本机 scripts/operator-key.ps1 创建/读取。</p>
    <label>模拟坐席凭据<input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} /></label>
    <button disabled={busy || !key} onClick={() => void load()}>读取接管队列</button>
    <label>澄清问题或修改建议<textarea value={note} maxLength={500} onChange={e => setNote(e.target.value)} /></label>
    {error && <p role="alert">{error}</p>}
    {tickets.map(ticket => <article key={ticket.id}><p>任务 {ticket.id} · 用户 {ticket.owner_id} · 计划版本 {ticket.plan_version}</p><p>原因：{ticket.control.reason}</p><p>建议：{ticket.control.suggestion || '暂无'}</p>
      <button disabled={busy || !note} onClick={() => void act(ticket.id, 'suggest')}>记录澄清/建议</button>
      <button disabled={busy} onClick={() => void act(ticket.id, 'resume')}>恢复检查点，交还用户</button>
      <button disabled={busy} className="secondary" onClick={() => void act(ticket.id, 'terminate')}>终止后续步骤</button>
    </article>)}
  </details></section>;
}
