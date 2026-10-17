import { useEffect, useRef, useState } from 'react';
import { api } from './api';
type Product = { id: string; name: string; risk_level: number; min_minor: number; lock_days: number; nav_minor: number; fee_minor: number; yield_text: string };
type Assessment = { id: string; max_risk: number; score: number; created_at_utc: string; expires_at_utc: string; scope: string; source: string };
type Order = { id: string; kind: string; status: string; product_id: string; account_id: string; amount_minor: number; units_hundredths: number; expires_at_utc: string;
 summary: { product: Product; account: { alias: string; mask: string }; fee_minor: number; settlement: string };
 receipt: { message: string; transaction_id: string; balance_minor: number; remaining_units_hundredths: number; accepted_at_utc: string; settled_at_utc: string } | null };
type Portfolio = { imported_positions?: Array<Record<string,string>>; assessment: Assessment | null; assessment_rule: string; questions: Array<{ label: string; options: string[] }>; products: Product[];
 positions: Array<{ id: string; product_id: string; units_hundredths: number }>; orders: Order[] };
const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);
const local = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
const labels: Record<string, string> = { pending_confirmation: '待确认，尚未受理', accepted: '已受理，待结算', settled: '已结算', cancelled: '已取消', invalidated: '已失效', expired: '已过期' };

export default function WealthPanel({ revision, onChanged }: { revision: number; onChanged: () => Promise<void> }) {
 const [data, setData] = useState<Portfolio | null>(null);
 const [answers, setAnswers] = useState(['', '', '', '']);
 const [retake, setRetake] = useState(false);
 const [busy, setBusy] = useState(false);
 const [error, setError] = useState('');
 const [syncError, setSyncError] = useState('');
 const [notice, setNotice] = useState('');
 const sequence = useRef(0), mounted = useRef(true), writing = useRef(false);
 async function load() {
   if (writing.current) return;
   const seq = ++sequence.current;
   try { const next = await api<Portfolio>('/api/wealth'); if (mounted.current && seq === sequence.current) { setData(next); setSyncError(''); } }
   catch (e) { if (mounted.current && seq === sequence.current) setSyncError(e instanceof Error ? e.message : '理财状态读取失败'); }
 }
 useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; sequence.current++; }; }, []);
 useEffect(() => { void load(); }, [revision]);
 async function post<T>(path: string, payload?: unknown): Promise<T> {
   const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
   return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
 }
 async function act(work: () => Promise<void>) {
   if (writing.current) return;
   writing.current = true; sequence.current++; setBusy(true); setError(''); setNotice('');
   try { await work(); }
   catch (e) { if (mounted.current) { setError(e instanceof Error ? e.message : '办理请求失败'); setNotice('请读取下方持久状态核对结果，再决定下一步。'); } }
   finally { writing.current = false; if (mounted.current) { setBusy(false); await load(); await onChanged(); } }
 }
 const unavailable = busy || !data || !!syncError;
 return <section className="section wealth-section" id="wealth">
   <div className="section-heading"><div><p className="eyebrow">SIMULATED WEALTH</p><h2>模拟理财</h2></div><button type="button" className="secondary" disabled={busy} onClick={() => void load()}>刷新理财记录</button></div>
   <p className="operation-note">在对话中提出申购或赎回，核对订单后通过这里的按钮授权。产品均为虚构测试产品，展示收益与历史区间不保证收益或保本；测试期间无收益。</p>
   {syncError && <div className="alert" role="alert">{syncError} 确认已暂停，读取最新状态后继续。</div>}
   {error && <div className="alert" role="alert">{error}</div>}
   {notice && <p role="status">{notice}</p>}
   {data?.assessment && <div className="risk-result">
     <h3>有效风险评估：R{data.assessment.max_risk}</h3>
     <p>形成时间 {local(data.assessment.created_at_utc)} · 有效至 {local(data.assessment.expires_at_utc)}</p>
     <p>{data.assessment.scope}</p>
     <button className="secondary" disabled={unavailable} onClick={() => { setRetake(true); setAnswers(['', '', '', '']); }}>重新风险评估</button>
   </div>}
   {data && (!data.assessment || retake) && <form className="risk-questionnaire" onSubmit={e => { e.preventDefault(); void act(async () => {
     await post('/api/wealth/assessment', { answers: answers.map(Number) });
     if (mounted.current) { setRetake(false); setNotice('风险评估已保存，后续推荐和交易将读取这一结果。'); }
   }); }}>
     <h3>完成风险评估</h3><p>{data.assessment_rule}</p>
     <div className="risk-fields">{data.questions.map((q, i) => <label key={q.label}>{q.label}<select required disabled={unavailable} value={answers[i]} onChange={e => setAnswers(values => values.map((v, n) => n === i ? e.target.value : v))}>
       <option value="">请选择实际情况</option>{q.options.map((option, index) => <option key={option} value={index}>{option}</option>)}
     </select></label>)}</div>
     <button disabled={unavailable || answers.some(v => v === '')}>提交风险评估</button>
   </form>}
   <div className="wealth-products"><h3>模拟产品事实</h3><div className="analytics-table-wrap"><table><thead><tr><th>产品</th><th>风险与起购</th><th>期限与赎回</th><th>收益口径</th></tr></thead><tbody>{data?.products.map(p => <tr key={p.id}><th>{p.id} {p.name}</th><td>R{p.risk_level} · {money(p.min_minor)}</td><td>{p.lock_days ? `锁定${p.lock_days}天，期内不可赎回` : '无固定锁定期，授权时同步结算'}</td><td>{p.yield_text}</td></tr>)}</tbody></table></div></div>
   <div className="wealth-positions"><h3>持久持仓</h3>{data?.positions.length ? data.positions.map(p => <p key={p.id}><strong>{p.product_id}：{(p.units_hundredths / 100).toFixed(2)}份</strong></p>) : <p>暂无本系统新交易持仓</p>}</div>
   {!!data?.imported_positions?.length && <div className="wealth-positions"><h3>导入基金持仓（快照，只读）</h3><p>导入持仓不支持在本系统赎回。</p>{data.imported_positions.map(p => <p key={p.holding_id}><strong>{p.product_name}</strong> · 市值 {money(Number(p.market_value_minor))} · 成本 {money(Number(p.current_cost_minor))}</p>)}</div>}
   <div className="wealth-orders"><h3>理财订单与回执</h3>{!data?.orders.length && <p>暂无订单</p>}{data?.orders.map(order => <article className="wealth-order" key={order.id}>
     <h4>{order.product_id} {order.kind === 'buy' ? '申购' : '赎回'} · {labels[order.status] ?? order.status}</h4>
     <p>{order.summary.account.alias} {order.summary.account.mask} · 金额 {money(order.amount_minor)} · 份额 {(order.units_hundredths / 100).toFixed(2)}份 · 手续费 {money(order.summary.fee_minor)} · 净值 {money(order.summary.product.nav_minor)}</p>
     <p>{order.summary.settlement}</p><small>订单 {order.id}</small>
     {order.status === 'pending_confirmation' && <><p>尚未扣款、未改变持仓，确认有效至 {local(order.expires_at_utc)}。</p><button disabled={unavailable} onClick={() => void act(async () => {
       const { confirmation_token } = await post<{ confirmation_token: string }>(`/api/wealth/orders/${order.id}/confirmation-token`);
       const done = await post<Order>(`/api/wealth/orders/${order.id}/confirm`, { confirmation_token });
       if (mounted.current) setNotice(done.receipt?.message ?? '请核对订单状态');
     })}>确认理财交易</button><button className="secondary" disabled={unavailable} onClick={() => void act(async () => { await post(`/api/wealth/orders/${order.id}/cancel`); })}>取消理财订单</button></>}
     {order.receipt && <div className="wealth-receipt"><strong>{order.receipt.message}</strong><p>受理时间 {local(order.receipt.accepted_at_utc)} · 结算时间 {local(order.receipt.settled_at_utc)}</p><p>账户余额 {money(order.receipt.balance_minor)} · 该笔结算后持仓 {(order.receipt.remaining_units_hundredths / 100).toFixed(2)}份</p><small>关联流水 {order.receipt.transaction_id}</small></div>}
   </article>)}</div>
 </section>;
}
