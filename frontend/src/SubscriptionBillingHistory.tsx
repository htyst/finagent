import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import DetailDialog from './DetailDialog';
import type {ImportedData} from './ImportedSubscriptions';
type Row=Record<string,string|number|null>;
type Page={billing_events:Row[];billing_total:number;limit:number;offset:number;has_more:boolean;period:{start_date:string;end_date:string}};
type Query={month:string;subscription:string;status:string};
const empty:Query={month:'',subscription:'',status:''};
const money=(v:unknown)=>v===null||v===undefined||v===''||!Number.isFinite(Number(v))?'未提供':new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(Number(v)/100);
const labels:Record<string,string>={paid:'已付',failed:'失败',blocked:'已阻止',CARD_FROZEN:'卡片冻结',CARD_EXPIRED:'卡片到期',INSUFFICIENT_FUNDS:'余额不足'};
export default function SubscriptionBillingHistory({data,refreshKey=''}:{data:ImportedData;refreshKey?:string}){
 const [query,setQuery]=useState(empty),[applied,setApplied]=useState(empty),[page,setPage]=useState<Page|null>(null);
 const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const [detail,setDetail]=useState<Row|null>(null),[detailError,setDetailError]=useState(''),[detailBusy,setDetailBusy]=useState(false);
 const controller=useRef<AbortController|null>(null),detailController=useRef<AbortController|null>(null);
 const selected=useRef(applied);selected.current=applied;
 useEffect(()=>()=>{controller.current?.abort();detailController.current?.abort();},[]);
 async function load(q:Query,offset=0){
  controller.current?.abort();const current=new AbortController();controller.current=current;setBusy(true);setError('');if(JSON.stringify(q)!==JSON.stringify(selected.current))setPage(null);
  const params=new URLSearchParams({limit:'10',offset:String(offset)});
  if(q.month){const [year,month]=q.month.split('-').map(Number);params.set('start',q.month+'-01');params.set('end',`${q.month}-${new Date(Date.UTC(year,month,0)).getUTCDate()}`);}
  if(q.subscription)params.set('subscription_id',q.subscription);if(q.status)params.set('status',q.status);
  try{const value=await api<Page>(`/api/subscription-analysis?${params}`,{signal:current.signal});if(!current.signal.aborted){setPage(value);setApplied(q);}}
  catch(e){if(!current.signal.aborted){setPage(null);setError(e instanceof Error?e.message:'扣费历史加载失败');}}
  finally{if(!current.signal.aborted)setBusy(false);}
 }
 useEffect(()=>{if(open)void load(selected.current);},[refreshKey]);
 async function transaction(row:Row){
  detailController.current?.abort();const current=new AbortController();detailController.current=current;setDetail({});setDetailError('');setDetailBusy(true);
  const id=row.cash_transaction_id||row.liability_event_id||'';
  try{const value=await api<Row>(`/api/transactions/${encodeURIComponent(String(id))}`,{signal:current.signal});if(!current.signal.aborted)setDetail(value);}
  catch(e){if(!current.signal.aborted)setDetailError(e instanceof Error?e.message:'流水加载失败');}
  finally{if(!current.signal.aborted)setDetailBusy(false);}
 }
 return <><details onToggle={e=>{const next=e.currentTarget.open;setOpen(next);if(next&&!page&&!busy)void load(applied);}}><summary>查看历史扣费尝试 · {page?.billing_total??data.billing_events.length}次</summary>
 <form className="spending-filters" onSubmit={e=>{e.preventDefault();void load(query);}}><label>扣费月份<input type="month" value={query.month} onChange={e=>setQuery({...query,month:e.target.value})}/></label><label>订阅服务<select aria-label="订阅服务" value={query.subscription} onChange={e=>setQuery({...query,subscription:e.target.value})}><option value="">全部服务</option>{data.contracts.map(s=><option key={s.subscription_id} value={String(s.subscription_id)}>{s.service_name}</option>)}</select></label><label>扣费结果<select aria-label="扣费结果" value={query.status} onChange={e=>setQuery({...query,status:e.target.value})}><option value="">全部结果</option><option value="paid">已付</option><option value="failed">失败</option><option value="blocked">已阻止</option></select></label><button disabled={busy}>查询扣费历史</button></form>
 {JSON.stringify(query)!==JSON.stringify(applied)&&page&&<p className="coverage-warning">筛选尚未应用，翻页仍使用当前结果的条件。</p>}
 {busy&&<p role="status">正在读取扣费历史…</p>}{error&&<p role="alert">{error}<button className="secondary" disabled={busy} onClick={()=>void load(query)}>重试扣费历史</button></p>}
 {page&&<><p>范围 {page.period.start_date} 至 {page.period.end_date}；按扣费日期从新到旧，共 {page.billing_total} 次。</p><div className="analytics-table-wrap"><table><thead><tr><th>服务 / 日期</th><th>金额</th><th>结果</th><th>对应流水</th></tr></thead><tbody>{page.billing_events.map(b=><tr key={b.billing_event_id}><td>{b.service_name||data.contracts.find(s=>s.subscription_id===b.subscription_id)?.service_name||b.subscription_id}<br/>{b.scheduled_date}<small>{b.subscription_id}</small></td><td>{money(b.amount_minor)}</td><td>{labels[String(b.status)]||b.status}{b.failure_code&&<><br/>{labels[String(b.failure_code)]||b.failure_code}</>}</td><td>{b.cash_transaction_id||b.liability_event_id?<button className="text-button" disabled={busy||detailBusy} onClick={()=>void transaction(b)}>查看关联流水</button>:'未产生扣款'}</td></tr>)}</tbody></table></div>{!page.billing_events.length&&<p>当前条件没有扣费尝试。</p>}<div className="pager"><button disabled={busy||page.offset===0} onClick={()=>void load(applied,Math.max(0,page.offset-page.limit))}>上一页扣费记录</button><span>{Math.floor(page.offset/page.limit)+1} / {Math.max(1,Math.ceil(page.billing_total/page.limit))}</span><button disabled={busy||!page.has_more} onClick={()=>void load(applied,page.offset+page.limit)}>下一页扣费记录</button></div></>}
 </details>{detail&&<DetailDialog title="关联交易明细" onClose={()=>{detailController.current?.abort();setDetail(null);setDetailBusy(false);}}>{detailBusy?<p>正在读取交易…</p>:detailError?<p role="alert">{detailError}</p>:<dl><dt>交易编号</dt><dd>{detail.id}</dd><dt>商户</dt><dd>{detail.merchant||'未提供'}</dd><dt>金额</dt><dd>{money(detail.amount_minor)}</dd><dt>北京时间</dt><dd>{new Date(String(detail.posted_at_utc)).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}</dd><dt>来源</dt><dd>{detail.source==='credit_card_ledger'?'信用卡账单':'现金账户流水'}</dd></dl>}</DetailDialog>}</>;
}
