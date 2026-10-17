import { useEffect, useState } from 'react';
import { api } from './api';
import { AddRecipientForm } from './DirectoryForms';

type Recipient = { id: string; name: string; bank_name?: string | null; phone_mask?: string | null; account_mask: string; remark: string | null; status: string };
export default function RecipientNotes() {
  const [recipients, setRecipients] = useState<Recipient[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    let active = true;
    api<Recipient[]>('/api/recipients').then(rows => {
      if (active) { setRecipients(rows); setDrafts(Object.fromEntries(rows.map(row => [row.id, row.remark ?? '']))); }
    }).catch(err => { if (active) setError(err instanceof Error ? err.message : '加载失败'); });
    return () => { active = false; };
  }, []);
  async function save(recipient: Recipient) {
    setBusy(recipient.id); setError(''); setNotice('');
    try {
      const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
      const updated = await api<Recipient>(`/api/recipients/${recipient.id}/remark`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token },
        body: JSON.stringify({ remark: drafts[recipient.id]?.trim() || null }),
      });
      setRecipients(rows => rows.map(row => row.id === recipient.id ? { ...row, ...updated } : row));
      setNotice(`${recipient.name}的备注已保存。`);
    } catch (err) { setError(err instanceof Error ? err.message : '保存失败'); }
    finally { setBusy(''); }
  }
  return <section className="section recipient-notes">
    <div className="section-heading"><div><p className="eyebrow">RECIPIENT DIRECTORY</p><h2>收款人管理</h2></div><span>本人保存的模拟收款人</span></div>
    <AddRecipientForm onSaved={async () => {
      const rows = await api<Recipient[]>('/api/recipients'); setRecipients(rows);
      setDrafts(Object.fromEntries(rows.map(row => [row.id, row.remark ?? ''])));
    }} />
    <details open><summary>查看和编辑备注</summary><p className="operation-note">保存后可说“给备注为房东的人转账”。多个相同备注会要求选择尾号；备注修改后，原待确认转账需要重新核对。</p>
      {recipients.map(recipient => <div className="remark-row" key={recipient.id}>
        <label htmlFor={`remark-${recipient.id}`}>{recipient.name} · {recipient.account_mask}<small>{recipient.bank_name ? ` · ${recipient.bank_name}` : ''}{recipient.phone_mask ? ` · ${recipient.phone_mask}` : ''}</small></label>
        <input id={`remark-${recipient.id}`} value={drafts[recipient.id] ?? ''} maxLength={80} disabled={!!busy}
          onChange={event => setDrafts(value => ({ ...value, [recipient.id]: event.target.value }))} placeholder="例如：房东" />
        <button type="button" disabled={!!busy || (drafts[recipient.id]?.trim() || null) === recipient.remark} onClick={() => void save(recipient)}>{busy === recipient.id ? '保存中…' : '保存备注'}</button>
      </div>)}
    </details>
    {error && <p role="alert" className="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
