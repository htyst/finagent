import {useEffect,useState} from 'react';
import {api} from './api';
import './FinancialProfile.css';
import FinancialHistory from './FinancialHistory';
type Row=Record<string,string|number>;
type Data={available:boolean;as_of:string;balances:Record<string,number>;cards:Row[];deposits:Row[];investments:Row[];liabilities:Row[];physical_assets:Row[];insurance:Row[];snapshot_note:string;coverage:{status:string}};
const money=(v:unknown)=>v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? '未提供' : new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(Number(v)/100);
const labels:Record<string,string>={active:'正常',frozen:'冻结',expired:'到期',closed:'已结清',disabled:'停用',locked:'定期锁定',matured:'到期已支取',early_withdrawn:'提前支取',overdue:'有逾期',repaid:'已还清'};
export default function FinancialProfile({revision}:{revision:number}){
 const [data,setData]=useState<Data|null>(null);const [error,setError]=useState('');const [attempt,setAttempt]=useState(0);const [historyOpen,setHistoryOpen]=useState(false);
 useEffect(()=>{let stopped=false;api<Data>('/api/financial-profile').then(r=>{if(!stopped){setData(r);setError('');}}).catch(e=>{if(!stopped)setError(e instanceof Error?e.message:'金融档案加载失败');});return()=>{stopped=true;};},[revision,attempt]);
 if(error)return <section className="section"><p role="alert">{error}</p><button onClick={()=>setAttempt(x=>x+1)}>重试金融档案</button></section>;
 if(!data)return <section className="section"><p role="status">正在读取金融档案…</p></section>;
 if(!data.available)return <section className="section"><p>当前用户没有导入金融档案，可在账户页查看银行余额和流水。</p></section>;
 const b=data.balances;
 return <section className="section financial-profile" id="financial-profile"><div className="section-heading"><div><p className="eyebrow">FINANCIAL PROFILE</p><h2>我的完整金融档案</h2></div><span>案例日期 {data.as_of}</span></div>
 <p className="operation-note">{data.snapshot_note}</p>{data.coverage.status==='incomplete'&&<p className="coverage-warning">此用户有历史覆盖缺口，档案金额仅对应可见案例记录。</p>}
 <div className="financial-totals">{[['活期存款',b.demand_minor],['可用活期',b.available_minor],['定期本金',b.term_minor],['基金市值（导入）',b.investment_snapshot_minor],['本系统理财持仓',b.new_app_wealth_minor],['房车估值（导入）',b.physical_minor],['贷款及信用卡欠款（导入）',b.liabilities_snapshot_minor],['净资产',b.net_worth_minor]].map(([label,value])=><article key={String(label)}><span>{label}</span><strong>{money(value)}</strong></article>)}</div>
 <details><summary>银行卡与所属银行 · {data.cards.length}张</summary><div className="analytics-table-wrap"><table><thead><tr><th>银行 / 卡</th><th>类型</th><th>尾号与状态</th><th>关联账户 / 负债</th><th>额度 / 欠款</th></tr></thead><tbody>{data.cards.map(c=><tr key={c.id}><td>{c.bank_name}<br/>{c.alias}</td><td>{c.card_type==='credit'?'信用卡':'借记卡'}</td><td>{c.card_last4} · {labels[String(c.status)]||c.status}</td><td>{c.account_id||c.liability_id}</td><td>{c.card_type==='credit'?`${money(c.credit_limit_minor)} / ${money(c.outstanding_minor)}`:'见银行账户余额'}</td></tr>)}</tbody></table></div><p className="operation-note">信用额度不是存款；信用卡资料与欠款已导入，尚未新增信用卡支付执行接口。</p></details>
 <details><summary>定期、基金与资产明细</summary><div className="analytics-table-wrap"><table><thead><tr><th>资产</th><th>本金 / 市值</th><th>期限与说明</th></tr></thead><tbody>{data.deposits.map(r=><tr key={r.deposit_id}><td>{r.bank_name} · {r.product_name}</td><td>{money(r.current_principal_minor)}</td><td>{r.maturity_date} · {labels[String(r.status)]||r.status}</td></tr>)}{data.investments.map(r=><tr key={r.holding_id}><td>{r.product_name}</td><td>{money(r.market_value_minor)}</td><td>成本 {money(r.current_cost_minor)}；浮动损益 {money(r.unrealized_pnl_minor)}</td></tr>)}{data.physical_assets.map(r=><tr key={r.asset_id}><td>{r.asset_name}</td><td>{money(r.estimated_value_minor)}</td><td>合成自报估值，不能直接动用</td></tr>)}</tbody></table></div></details>
 <details><summary>贷款、还款与保险</summary><div className="analytics-table-wrap"><table><thead><tr><th>项目</th><th>当前金额</th><th>下次日期 / 状态</th></tr></thead><tbody>{data.liabilities.map(r=><tr key={r.liability_id}><td>{r.liability_type==='credit_card'?'信用卡账单':r.liability_type} · {r.lender}</td><td>欠款 {money(r.outstanding_balance_minor)}<br/>下期本金及利息 {r.next_principal_minor === '' || r.next_interest_minor === '' || r.next_principal_minor == null || r.next_interest_minor == null ? '未提供' : money(Number(r.next_principal_minor)+Number(r.next_interest_minor))}</td><td>{r.next_due_date} · {labels[String(r.status)]||r.status}</td></tr>)}{data.insurance.map(r=><tr key={r.policy_id}><td>模拟医疗保障</td><td>年保费 {money(r.annual_premium_minor)}</td><td>保障至 {r.coverage_ends_on}；保额不计资产</td></tr>)}</tbody></table></div></details>
 <details onToggle={e=>setHistoryOpen(e.currentTarget.open)}><summary>查询历史账单与资产变动</summary>{historyOpen&&<FinancialHistory cards={data.cards.map(c=>({id:String(c.id),alias:String(c.alias)}))}/>}</details>
 <p className="operation-note">订阅与自动续费请在“订阅代扣”页签明确确认查询后查看。档案中的定期、贷款和基金用于案例展示；原有金融操作仍走固定单与安全验证。</p>
 </section>;
}
