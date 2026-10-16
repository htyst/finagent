import { useEffect, useRef } from 'react';
import { useWorkspaceDraft } from './WorkspaceDrafts';

export type BirthdayDetails = { source_reference: string; recipient: string; address: string; source_error?: string | null };
export type TransferDetails = { name: string; bank_name: string; account_number: string };
type AccountChoice = { id: string; alias: string; account_mask: string; status: string };

export default function TaskDetailsForm({ taskId, kind, value, accounts, disabled, onSubmit }: {
  taskId: string; kind: 'birthday' | 'transfer'; value: BirthdayDetails | TransferDetails; accounts: AccountChoice[];
  disabled: boolean; onSubmit: (kind: 'birthday' | 'transfer', values: Record<string, string>) => Promise<void>;
}) {
  const birthday = kind === 'birthday';
  const keys = birthday ? ['source_reference', 'recipient', 'address'] : ['name', 'bank_name', 'account_number'];
  const labels = birthday ? ['付款账户', '收货人', '收货地址'] : ['收款户名', '开户银行', '收款账号'];
  const initial = value as unknown as Record<string, string>;
  const [draft, setDraft] = useWorkspaceDraft(`${taskId}-${kind}`, () => Object.fromEntries(keys.map(key => [key, initial[key] ?? ''])));
  const signature = JSON.stringify(keys.map(key => initial[key]));
  const previous = useRef(signature);
  useEffect(() => { if (previous.current !== signature) { previous.current = signature; setDraft(Object.fromEntries(keys.map(key => [key, initial[key] ?? '']))); } },
    [taskId, kind, initial[keys[0]], initial[keys[1]], initial[keys[2]]]);
  return <form className="agent-details-form" aria-label={birthday ? '生日收货资料' : '新收款人资料'} onSubmit={event => {
    event.preventDefault(); if (!disabled) void onSubmit(kind, Object.fromEntries(keys.map(key => [key, draft[key].trim()])));
  }}>
    <h3>{birthday ? '补充付款与收货资料' : '填写新收款人的资料'}</h3>
    <p className="details-note operation-note">{birthday ? '请选择或填写本人已关联的付款账户，再补充收货资料。提交后继续核对安排，不会直接预留资金或扣款。' : '收款人无需预先收录。填写完整资料后准备操作单，页面确认才会付款。'}</p>
    {keys.map((key, index) => <label key={key} htmlFor={`${kind}-${key}`}>{labels[index]}
      <input id={`${kind}-${key}`} aria-label={labels[index]}
        aria-invalid={key === 'source_reference' && !!(value as BirthdayDetails).source_error}
        aria-describedby={key === 'source_reference' && (value as BirthdayDetails).source_error ? 'birthday-source-error' : undefined}
        value={draft[key]} onChange={event => setDraft(old => ({ ...old, [key]: event.target.value }))}
        required disabled={disabled} maxLength={key === 'address' ? 200 : key === 'account_number' ? 50 : key === 'recipient' ? 60 : 80}
        list={key === 'source_reference' ? 'agent-payment-accounts' : undefined}
        autoComplete={key === 'account_number' ? 'off' : undefined}
        placeholder={key === 'source_reference' ? '本人付款账户名称或尾号' : key === 'account_number' ? '完整收款账号，不是尾号' : labels[index]} />
      {key === 'source_reference' && (value as BirthdayDetails).source_error && <span id="birthday-source-error" className="field-error">{(value as BirthdayDetails).source_error}</span>}
    </label>)}
    {birthday && <datalist id="agent-payment-accounts">{accounts.filter(a => a.status === 'active').map(a => <option key={a.id} value={a.alias}>{a.account_mask}</option>)}</datalist>}
    <div className="details-actions"><button type="submit" disabled={disabled || keys.some(key => !draft[key]?.trim())}>提交资料并继续</button></div>
  </form>;
}
