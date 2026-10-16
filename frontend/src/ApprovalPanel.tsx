import { useState } from 'react';
import { api } from './api';

export default function ApprovalPanel({ requests, onChanged }: { requests: Array<{ id: string; status: string }>; onChanged: () => Promise<void> }) {
  const [key, setKey] = useState(''), [busy, setBusy] = useState(false), [message, setMessage] = useState('');
  async function approve(id: string) {
    setBusy(true); setMessage('');
    try {
      const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
      await api(`/api/card-management/requests/${id}/approve`, { method: 'POST', headers: {
        'X-FinAgent-CSRF': csrf_token, 'X-FinAgent-Approver': key,
      } });
      setMessage('已记录独立模拟审批，请核对发卡回执。'); await onChanged();
    } catch (e) { setMessage(e instanceof Error ? e.message : '审批失败'); }
    finally { setBusy(false); }
  }
  return <details className="operation-note"><summary>本机独立模拟审批</summary>
    <p>审批员凭据由 scripts/approver-key.ps1 创建，与用户确认、短信验证和模拟坐席凭据分别使用。仅能处理已受理申请；这是本机模拟角色隔离。</p>
    <label>模拟审批员凭据<input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} /></label>
    {requests.filter(r => r.status === 'submitted').map(r => <p key={r.id}>申请 {r.id} <button type="button" disabled={busy || !key} onClick={() => void approve(r.id)}>模拟审批通过并发卡</button></p>)}
    {message && <p role="status">{message}</p>}
  </details>;
}
