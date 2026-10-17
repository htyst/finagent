import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import FinancialReadReport from './FinancialReadReport';
const tables=[['monthly_financial_snapshots','月末资产快照'],['credit_card_statements','信用卡账单'],['liability_events','负债与信用卡事件'],['investment_valuations','基金估值'],['monthly_summary','月度收支汇总'],['deposits','定期存款合同'],['liabilities','负债合同'],['insurance_policies','保险合同']];
export default function FinancialHistory({cards}:{cards:Array<{id:string;alias:string}>}){
 const [table,setTable]=useState('monthly_financial_snapshots'),[card,setCard]=useState(''),[start,setStart]=useState(''),[end,setEnd]=useState('');
 const [data,setData]=useState<Record<string,unknown>|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const controller=useRef<AbortController|null>(null);
 async function load(selected=table){
  controller.current?.abort();const current=new AbortController();controller.current=current;setBusy(true);setError('');setData(null);
  const query=new URLSearchParams({table:selected,limit:'10'});
  if(start)query.set('start',start);if(end)query.set('end',end);
  if(card&&['credit_card_statements','liability_events'].includes(selected))query.set('card_id',card);
  try{const value=await api<Record<string,unknown>>(`/api/financial-history?${query}`,{signal:current.signal});if(!current.signal.aborted)setData(value);}
  catch(e){if(!current.signal.aborted)setError(e instanceof Error?e.message:'历史档案读取失败');}
  finally{if(!current.signal.aborted)setBusy(false);}
 }
 useEffect(()=>{void load();return()=>controller.current?.abort();},[]);
 return <div className="financial-history"><form className="spending-filters" onSubmit={e=>{e.preventDefault();void load();}}><label>历史资料<select aria-label="历史资料" value={table} onChange={e=>{setTable(e.target.value);setCard('');}}>{tables.map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label><label>历史开始日期<input type="date" value={start} onChange={e=>setStart(e.target.value)}/></label><label>历史结束日期<input type="date" value={end} onChange={e=>setEnd(e.target.value)}/></label><label>历史银行卡<select aria-label="历史银行卡" disabled={!['credit_card_statements','liability_events'].includes(table)} value={card} onChange={e=>setCard(e.target.value)}><option value="">全部银行卡</option>{cards.map(c=><option key={c.id} value={c.id}>{c.alias}</option>)}</select></label><button disabled={busy}>查询金融历史</button></form><p className="operation-note">修改条件后点击查询；翻页使用下方结果原有的范围。</p>{busy&&<p role="status">正在读取历史档案…</p>}{error&&<p role="alert">{error}<button className="secondary" onClick={()=>void load()}>重试金融历史</button></p>}{data&&<FinancialReadReport initial={data}/>}</div>;
}
