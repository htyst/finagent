import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api } from './api';
import './DirectoryForms.css';

type AccountChoice = { id: string; alias: string; account_mask: string; status: string };
async function create(path: string, payload: unknown) {
  const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token }, body: JSON.stringify(payload) });
}

export function AddCardForm({ accounts, onSaved }: { accounts: AccountChoice[]; onSaved: () => Promise<void> }) {
  const [alias, setAlias] = useState(''), [bank, setBank] = useState('模拟银行'), [number, setNumber] = useState('');
  const [account, setAccount] = useState(''), [accountAlias, setAccountAlias] = useState(''), [balance, setBalance] = useState('0');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const mounted = useRef(true), writing = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (writing.current) return;
    writing.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const compact = number.replace(/[ -]/g, '');
      if (!/^[0-9]{12,19}$/.test(compact)) throw Error('模拟卡号须为12至19位数字。');
      if (!account && !/^\d+(\.\d{1,2})?$/.test(balance)) throw Error('初始余额须为非负金额，最多两位小数。');
      const minor = Math.round(Number(balance) * 100);
      if (!account && (!Number.isSafeInteger(minor) || minor < 0 || minor > 100000000000)) throw Error('初始余额超出范围。');
      await create('/api/cards', { alias: alias.trim(), bank_name: bank.trim(), card_number: compact,
        ...(account ? { account_id: account } : { account_alias: accountAlias.trim() || alias.trim() + '账户', opening_balance_minor: minor }) });
      if (mounted.current) { setNotice('银行卡已添加，可用于转账、冻结及模拟消费。'); setNumber(''); setAlias(''); setAccountAlias(''); setBalance('0'); }
      await onSaved();
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : '添加失败，请刷新列表核对。'); }
    finally { writing.current = false; if (mounted.current) setBusy(false); }
  }
  return <details className="directory-add"><summary>添加银行卡</summary>
    <p className="operation-note">添加本地模拟银行卡。可以关联已有账户，也可以新建账户并设置初始模拟余额。</p>
    <form aria-label="添加银行卡" className="directory-form" onSubmit={e => void submit(e)}>
      <label>卡片名称<input required maxLength={80} value={alias} onChange={e => setAlias(e.target.value)} disabled={busy} placeholder="例如：旅行卡" /></label>
      <label>银行名称<input required maxLength={80} value={bank} onChange={e => setBank(e.target.value)} disabled={busy} /></label>
      <label>模拟银行卡号<input required maxLength={40} inputMode="numeric" autoComplete="off" value={number} onChange={e => setNumber(e.target.value)} disabled={busy} placeholder="12至19位数字，保存后仅显示尾号" /></label>
      <label>关联账户<select value={account} onChange={e => setAccount(e.target.value)} disabled={busy}><option value="">新建模拟账户</option>{accounts.filter(a => a.status === 'active').map(a => <option key={a.id} value={a.id}>{a.alias} {a.account_mask}</option>)}</select></label>
      {!account && <><label>新账户名称<input maxLength={80} value={accountAlias} onChange={e => setAccountAlias(e.target.value)} disabled={busy} placeholder={alias ? alias + '账户' : '默认使用卡片名称＋账户'} /></label>
        <label>初始模拟余额（元）<input required inputMode="decimal" value={balance} onChange={e => setBalance(e.target.value)} disabled={busy} /></label></>}
      <div className="directory-form-actions"><button disabled={busy}>{busy ? '添加中…' : '添加银行卡'}</button></div>
    </form>{error && <p role="alert" className="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </details>;
}

export function AddRecipientForm({ onSaved }: { onSaved: () => Promise<void> }) {
  const [name, setName] = useState(''), [bank, setBank] = useState('模拟银行'), [number, setNumber] = useState('');
  const [phone, setPhone] = useState(''), [remark, setRemark] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const mounted = useRef(true), writing = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (writing.current) return;
    writing.current = true; setBusy(true); setError(''); setNotice('');
    try {
      await create('/api/recipients', { name: name.trim(), bank_name: bank.trim(), account_number: number.trim(), phone: phone.trim() || null, remark: remark.trim() || null });
      if (mounted.current) { setNotice('收款人已添加，可以按姓名、手机号或备注发起转账。'); setName(''); setNumber(''); setPhone(''); setRemark(''); }
      await onSaved();
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : '添加失败，请刷新列表核对。'); }
    finally { writing.current = false; if (mounted.current) setBusy(false); }
  }
  return <details className="directory-add"><summary>添加收款人</summary>
    <form aria-label="添加收款人" className="directory-form" onSubmit={e => void submit(e)}>
      <label>收款人姓名<input required maxLength={80} value={name} onChange={e => setName(e.target.value)} disabled={busy} /></label>
      <label>开户银行<input required maxLength={80} value={bank} onChange={e => setBank(e.target.value)} disabled={busy} /></label>
      <label>收款账号<input required maxLength={50} autoComplete="off" value={number} onChange={e => setNumber(e.target.value)} disabled={busy} placeholder="完整模拟账号，保存后仅显示尾号" /></label>
      <label>收款人手机号（可选）<input type="tel" maxLength={30} value={phone} onChange={e => setPhone(e.target.value)} disabled={busy} /></label>
      <label>收款人备注（可选）<input maxLength={80} value={remark} onChange={e => setRemark(e.target.value)} disabled={busy} placeholder="例如：房东" /></label>
      <div className="directory-form-actions"><button disabled={busy}>{busy ? '添加中…' : '添加收款人'}</button></div>
    </form>{error && <p role="alert" className="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </details>;
}
