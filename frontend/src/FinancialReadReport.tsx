import {useEffect,useRef,useState} from 'react';
import {api} from './api';
import './FinancialReadReport.css';

type Row = Record<string, unknown>;
const labels: Record<string,string> = {
  group:'商户 / 日期',purchase_minor:'消费',refund_minor:'退款',net_minor:'净消费',transaction_count:'交易笔数',
  income_minor:'收入',inflow_minor:'现金流入',outflow_minor:'现金流出',net_cashflow_minor:'净现金流',
  eligible_purchase_count:'符合条件的消费笔数',returned_count:'本页笔数',group_count:'总组数',returned_groups:'本页组数',
  has_more_groups:'还有其他分组',id:'记录编号',merchant:'商户',posted_at_utc:'入账时间',amount_minor:'金额',category:'类别',
  card_id:'卡片编号',source:'流水来源',status:'状态',subscription_id:'订阅编号',service_name:'服务名称',billing_cycle:'付费周期',
  current_price_minor:'每期金额',monthly_equivalent_minor:'每月等值费用',paid_total_minor:'查询期间实付',auto_renew_enabled:'自动续费',
  scheduled_date:'计划扣费日期',failure_code:'失败原因',billing_event_id:'扣费记录编号',
  contract_count:'合同数',auto_renew_contract_count:'自动续费合同数',auto_renew_monthly_minor:'自动续费折月金额',
  paid_attempt_count:'已付次数',failed_attempt_count:'失败次数',date:'日期',month:'月份',snapshot_date:'快照日期',
  outstanding_balance_minor:'未偿余额',total_assets_minor:'总资产',net_worth_minor:'净资产',
  as_of:'快照日期',demand_deposit_minor:'活期存款',term_deposit_minor:'定期存款',investment_value_minor:'投资市值',
  physical_assets_minor:'房车估值',total_liabilities_minor:'总负债',coverage_status:'覆盖状态',valuation_scope:'估值口径',
  statement_date:'账单日期',due_date:'还款日期',statement_balance_minor:'账单余额',minimum_due_minor:'最低应还',
  statement_id:'账单编号',cutoff_at_local:'账单截止时点',payment_minor:'已还金额',payment_transaction_id:'还款流水',
  valuation_date:'估值日期',holding_id:'持仓编号',market_value_minor:'市值',current_cost_minor:'当前成本',unrealized_pnl_minor:'浮动损益',
  occurred_at_local:'发生时间',event_type:'事件类型',liability_id:'负债编号',liability_event_id:'信用卡事件编号',cash_transaction_id:'现金流水编号',
  next_billing_date:'下次计划日期',next_billing_amount_minor:'下次计划金额',trial_ends_on:'试用结束日期',
  trial_conversion_price_minor:'试用转付费价格',access_end_date:'权益截止日期',payment_bank_name:'扣款银行',payment_card_last4:'卡尾号',
};
function cell(key:string,value:unknown):string {
  if(value === null || value === undefined || value === '')return '未提供';
  if(key.endsWith('_minor') && Number.isFinite(Number(value)))return new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(Number(value)/100);
  if(typeof value === 'boolean' || value === 'true' || value === 'false')return value===true||value==='true' ? '是' : '否';
  if(key==='status')return ({active:'有效',issued:'已出账',paid:'已付',failed:'失败',blocked:'已阻止',posted:'已入账',trial:'试用',paused:'暂停',canceled:'已取消',closed:'已结清'} as Record<string,string>)[String(value)]??String(value);
  if(key==='coverage_status')return value==='complete'?'完整':value==='incomplete'?'有缺口':String(value);
  if(typeof value === 'object')return JSON.stringify(value);
  return String(value);
}
function Table({rows}:{rows:Row[]}) {
  const keys=[...new Set(rows.flatMap(row=>Object.keys(row)))].filter(k=>!['owner_id','data_origin','user_name'].includes(k));
  if(!rows.length)return <p>本页暂无记录。</p>;
  return <div className="analytics-table-wrap"><table><thead><tr>{keys.map(k=><th key={k}>{labels[k]??k}</th>)}</tr></thead><tbody>{rows.map((row,i)=><tr key={i}>{keys.map(k=><td key={k}>{cell(k,row[k])}</td>)}</tr>)}</tbody></table></div>;
}

// The backend owns all totals. The frontend only formats the persisted facts.
export default function FinancialReadReport(props:{initial:Row;filters?:Row}) {
  return <ReportPage key={JSON.stringify(props)} {...props}/>;
}
function ReportPage({initial,filters={}}:{initial:Row;filters?:Row}) {
  const [data,setData]=useState(initial),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const request=useRef<AbortController|null>(null);
  useEffect(()=>()=>request.current?.abort(),[]);
  const history=typeof data.table==='string',subscription=Array.isArray(data.contracts);
  const title=history?'金融历史档案':subscription?'订阅与自动续费分析':'金融收支分析';
  const period=(initial.period??{}) as Row;
  const original:Row={...(subscription ? {start:period.start_date,end:period.end_date}:{}),...filters,...(initial.query??{}) as Row};
  async function page(offset:number,contracts=false) {
    request.current?.abort();const controller=new AbortController();request.current=controller;
    setBusy(true);setError('');
    const query=new URLSearchParams();
    const allowed=history?['start','end','card_id','liability_id','subscription_id','status']:['start','end','subscription_id','status'];
    for(const k of allowed){const v=original[k as keyof typeof original];if(v!==null&&v!==undefined&&v!=='')query.set(k,String(v));}
    query.set('offset',String(contracts ? data.offset??0 : offset));query.set('limit',String(data.limit??20));
    if(subscription){query.set('contract_offset',String(contracts?offset:data.contract_offset??0));query.set('contract_limit',String(data.contract_limit??50));}
    if(history)query.set('table',String(data.table));
    try {const result=await api<Row>(`/api/${history?'financial-history':'subscription-analysis'}?${query}`,{signal:controller.signal});if(!controller.signal.aborted)setData(result);}
    catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'明细加载失败');}finally{if(!controller.signal.aborted)setBusy(false);}
  }
  const rows=(history||data.analysis_kind?data.items:data.billing_events) as Row[]|undefined;
  const summary=data.summary as Row|undefined;
  const total=Number(history?data.total:data.billing_total),offset=Number(data.offset??0),limit=Number(data.limit??20);
  const contractOffset=Number(data.contract_offset??0),contractLimit=Number(data.contract_limit??50),contractTotal=Number(data.contracts_total??0);
  return <article className="analytics-report financial-read-report" aria-label={title}><h3>{title}</h3>
    <p className="operation-note">{String(data.basis??data.cost_basis??`档案快照：${data.snapshot_as_of??'未提供'}`)}</p>
    {!!period.start_date&&<p>查询日期：{String(period.start_date)} 至 {String(period.end_date??(period.end_exclusive_date ? new Date(Date.parse(String(period.end_exclusive_date)+'T00:00:00Z')-86400000).toISOString().slice(0,10):'未提供'))}</p>}
    {subscription&&<p className="operation-note">合同快照 {String(data.as_of??'未提供')}；金额汇总覆盖查询合同与日期，扣费状态筛选仅作用于下方明细。{!!original.subscription_id&&` 合同：${String(original.subscription_id)}`} {!!original.status&&`扣费状态：${String(original.status)}`}</p>}
    {summary&&<dl>{Object.entries(summary).filter(([,v])=>typeof v!=='object').map(([k,v])=><div key={k}><dt>{labels[k]??k}</dt><dd>{cell(k,v)}</dd></div>)}</dl>}
    {subscription&&<><h4>订阅合同（本页 {contractTotal?contractOffset+1:0} 起，共 {contractTotal} 份）</h4><Table rows={data.contracts as Row[]}/>{contractTotal>contractLimit&&<div className="pager"><button disabled={busy||contractOffset===0} onClick={()=>void page(Math.max(0,contractOffset-contractLimit),true)}>上一页合同</button><span>{Math.floor(contractOffset/contractLimit)+1} / {Math.ceil(contractTotal/contractLimit)}</span><button disabled={busy||contractOffset+contractLimit>=contractTotal} onClick={()=>void page(contractOffset+contractLimit,true)}>下一页合同</button></div>}<h4>扣费依据</h4></>}
    {rows&&<Table rows={rows}/>}
    {Number.isFinite(total)&&<p>当前展示 {rows?.length??0} 条，共 {total} 条。</p>}
    {(history||subscription)&&total>limit&&<div className="pager"><button disabled={busy||offset===0} onClick={()=>void page(Math.max(0,offset-limit))}>{history?'上一页档案':'上一页扣费'}</button><span>{Math.floor(offset/limit)+1} / {Math.ceil(total/limit)}</span><button disabled={busy||offset+limit>=total} onClick={()=>void page(offset+limit)}>{history?'下一页档案':'下一页扣费'}</button></div>}
    {error&&<p role="alert">{error}</p>}
  </article>;
}
