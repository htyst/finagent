import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import ApprovalPanel from './ApprovalPanel';
import ServiceRequests from './ServiceRequests';
import { AddCardForm } from './DirectoryForms';
type Card = { id: string; alias: string; mask: string; account_id: string | null; status: string; limit_minor: number; online_enabled: boolean; lost: boolean; card_type?:string;read_only?:boolean };
type DirectoryCard = {id:string;alias:string;card_mask:string;account_id:string|null;status:string;card_type:string;read_only:boolean;controls:{limit_minor:number;online_enabled:boolean;lost:boolean}};
type Request = { id: string; label: string; kind: string; status: string; expires_at_utc: string; summary: { applicant: string | null; product: string | null; account_alias: string; account_mask: string; card_mask: string | null; limit_minor: number | null }; receipt: { message: string; card_mask?: string; completed_at_utc?: string } | null };
type Payment = { id: string; card_id: string; amount_minor: number; channel: string; status: string; message: string; transaction_id: string | null; balance_minor: number; created_at_utc: string };
type Data = { rules: string; cards: Card[]; requests: Request[]; payments: Payment[] };
const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);
const local = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
const labels: Record<string, string> = { active: '正常', replaced:'补卡后原卡已永久停用', frozen: '已冻结', disabled: '已停用', closed: '已关闭', lost: '已挂失', pending_confirmation: '待确认，未办理', submitted: '审核中，尚未发卡', issued: '已发卡', executed: '已办理', cancelled: '已取消', invalidated: '已失效', expired: '已过期' };
export default function CardManagement({ accounts, revision, onChanged, demo = false }: { accounts: Array<{id: string; alias: string; account_mask: string; status: string}>; demo?: boolean; revision: number; onChanged: () => Promise<void> }) {
 const [data, setData] = useState<Data | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState(''), [syncError, setSyncError] = useState(''), [notice, setNotice] = useState('');
 const [card, setCard] = useState(''), [amount, setAmount] = useState('10'), [channel, setChannel] = useState('online');
 const mounted = useRef(true), writing = useRef(false), seq = useRef(0), retry = useRef<{ key: string; digest: string } | null>(null);
 async function load() {
  if (writing.current) return;
  const version = ++seq.current;
  try { const [next,directory] = await Promise.all([api<Data>('/api/card-management'),api<DirectoryCard[]>('/api/cards')]);
   next.cards=directory.map(c=>({...next.cards.find(n=>n.id===c.id),id:c.id,alias:c.alias,mask:c.card_mask,account_id:c.account_id,status:c.status,card_type:c.card_type,read_only:c.read_only,...c.controls}));
   if (mounted.current && version === seq.current) { setData(next); setSyncError(''); setCard(c => next.cards.some(r => r.id === c&&!r.read_only) ? c : next.cards.find(c=>!c.read_only)?.id ?? ''); } }
  catch (e) { if (mounted.current && version === seq.current) setSyncError(e instanceof Error ? e.message : '读取卡片状态失败'); }
 }
 useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; seq.current++; }; }, []);
 useEffect(() => { void load(); }, [revision]);
 async function post<T>(path: string, payload?: unknown) { const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf'); return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) }); }
 async function act(work: () => Promise<void>) {
  if (writing.current) return;
  writing.current = true; seq.current++; setBusy(true); setError(''); setNotice('');
  try { await work(); } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : '办理失败，请刷新核对持久状态'); }
  finally { writing.current = false; if (mounted.current) { setBusy(false); await load(); await onChanged(); } }
 }
 const unavailable = busy || !data || !!syncError;
 return <section className="section card-management" id="card-management">
  <div className="section-heading"><div><p className="eyebrow">SIMULATED CARD MANAGEMENT</p><h2>模拟卡片管理</h2></div><button className="secondary" disabled={busy} onClick={() => void load()}>刷新卡片记录</button></div>
  {!demo && <AddCardForm accounts={accounts} onSaved={async () => { await load(); await onChanged(); }} />}
  <p className="operation-note">在对话中申请卡、调整单笔限额、设置线上限制或挂失，核对固定办理单后确认。{data?.rules}</p>
  {syncError && <div className="alert" role="alert">{syncError} 确认已暂停，请刷新后继续。</div>}{error && <div className="alert" role="alert">{error}</div>}{notice && <p role="status">{notice}</p>}
  <div className="analytics-table-wrap"><table><thead><tr><th>卡片</th><th>卡状态</th><th>单笔交易限额</th><th>线上消费</th></tr></thead><tbody>{data?.cards.map(c => <tr key={c.id}><th>{c.alias} {c.mask}<small>{c.card_type==='credit'?'信用卡 · 导入只读':'借记卡'}</small></th><td>{c.lost ? '已挂失，原卡不可消费' : labels[c.status] ?? c.status}</td><td>{c.read_only?'未接入单笔限额':money(c.limit_minor)}</td><td>{c.read_only?'本系统未接入支付':c.online_enabled ? '允许' : '禁止'}{c.lost || c.status === 'frozen' || c.status === 'expired' ? '（仍受整卡状态限制）' : ''}</td></tr>)}</tbody></table></div>
  {!demo && <><h3>申请及办理记录</h3>{!data?.requests.length && <p>暂无申请或办理记录</p>}{data?.requests.map(r => <article className="card-request" key={r.id}>
   <h4>{r.label} · {labels[r.status] ?? r.status}</h4><p>{r.summary.card_mask ?? r.summary.product} · 关联{r.summary.account_alias} {r.summary.account_mask}{r.summary.applicant && ` · 申请人${r.summary.applicant}`}{r.summary.limit_minor !== null && ` · 新限额${money(r.summary.limit_minor)}`}</p><small>办理编号 {r.id}</small>
   {r.status === 'pending_confirmation' && <><p>未办理，确认有效至 {local(r.expires_at_utc)}。</p><button disabled={unavailable} onClick={() => void act(async () => { const { confirmation_token } = await post<{ confirmation_token: string }>(`/api/card-management/requests/${r.id}/confirmation-token`); const done = await post<Request>(`/api/card-management/requests/${r.id}/confirm`, { confirmation_token }); if (mounted.current) setNotice(done.receipt?.message ?? '请核对办理记录'); })}>确认卡片办理</button><button className="secondary" disabled={unavailable} onClick={() => void act(async () => { await post(`/api/card-management/requests/${r.id}/cancel`); })}>取消卡片办理</button></>}
   {r.status === 'submitted' && <p>申请已受理，尚未发卡。请到演示工具，由持独立凭据的本机模拟审批员处理；用户确认不等于审批。</p>}
   {r.receipt && <div className="card-receipt"><strong>{r.receipt.message}</strong>{r.receipt.card_mask && <p>卡片 {r.receipt.card_mask}</p>}{r.receipt.completed_at_utc && <small>{local(r.receipt.completed_at_utc)}</small>}</div>}
  </article>)}
  </>}
  {demo && <><ApprovalPanel requests={data?.requests ?? []} onChanged={async () => { await load(); await onChanged(); }} />
  <form className="card-payment-form" onSubmit={e => { e.preventDefault(); void act(async () => {
   const number = Number(amount); if (!Number.isFinite(number) || number <= 0 || !/^\d+(\.\d{1,2})?$/.test(amount)) throw Error('金额须为正数，最多两位小数');
   const payload = { card_id: card, amount_minor: Math.round(number * 100), channel }; const digest = JSON.stringify(payload);
   if (!retry.current || retry.current.digest !== digest) retry.current = { key: crypto.randomUUID(), digest };
   const done = await post<Payment>('/api/card-management/payments', { ...payload, idempotency_key: retry.current.key }); retry.current = null; if (mounted.current) setNotice(done.message + '；请核对下方记录和账户余额');
  }); }}><h3>发起独立模拟消费</h3><p>用于验证卡片规则是否实际生效，成功后写入模拟流水，拒绝时不扣款。</p><div className="risk-fields"><label>卡片<select name="card" disabled={unavailable} value={card} onChange={e => setCard(e.target.value)}>{data?.cards.filter(c=>!c.read_only).map(c => <option key={c.id} value={c.id}>{c.alias} {c.mask}</option>)}</select></label><label>金额（元）<input name="amount" required disabled={unavailable} value={amount} onChange={e => setAmount(e.target.value)} inputMode="decimal" /></label><label>交易渠道<select name="channel" disabled={unavailable} value={channel} onChange={e => setChannel(e.target.value)}><option value="online">线上</option><option value="offline">线下</option></select></label></div><button disabled={unavailable || !card}>发起模拟消费</button></form>
  <h3>消费结果与回执</h3>{!data?.payments.length && <p>暂无模拟消费</p>}{data?.payments.map(p => <article className="card-payment" key={p.id}><strong>{data.cards.find(c => c.id === p.card_id)?.mask ?? p.card_id} · {money(p.amount_minor)} · {p.channel === 'online' ? '线上' : '线下'} · {p.message}</strong><p>该次处理后账户余额 {money(p.balance_minor)} · {local(p.created_at_utc)}</p><small>消费编号 {p.id}{p.transaction_id && ` · 流水${p.transaction_id}`}</small></article>)}
 </>}
 <ServiceRequests revision={revision} onChanged={onChanged}/>
 </section>;
}
