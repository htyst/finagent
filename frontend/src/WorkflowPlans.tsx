import {useCallback, useEffect, useRef, useState} from 'react';
import {api} from './api';
import {executionStates} from './executionGraph';
import './WorkflowPlans.css';

type Goal = {id:string;text:string;status:string;next_step?:string|null};
type Plan = {id:string;kind:string;title:string;status:string;task_id:string|null;route:string;can_review:boolean;goals:Goal[];
  budget_minor?:number;spent_minor?:number;recoveries:Array<{node_id:string;title:string;message:string}>};
type Review = {id:string;task_id:string;title:string;status:string;version:number;next_run_at_utc:string;end_at_utc:string;
  interval_days:number;target_minor:number|null;error_code:string|null;scope:string;result:{as_of_utc?:string;available_minor?:number;gap_minor?:number;note?:string}};
type Data = {plans:Plan[];reviews:Review[];jobs:Array<{id:string;task_id:string;status:string;attempts:number;error_code:string|null}>;
  drafts?:Array<{id:string;task_id:string;title:string;run_at:string;end_at:string;interval_days:number;target_minor:number|null;scope:string;notification:string}>;
  reservations:Array<{plan_id:string;account_id:string;amount_minor:number;purpose:string;route:string}>};
const labels:Record<string,string> = {...executionStates,scheduled:'等待约定时间',completed_with_changes:'变更后完成',ordering:'正在下单',awaiting_delivery:'等待送达回执',partial_failure:'部分失败，待处理',blocked:'执行受阻',gathering_info:'待补充',running_readonly:'处理中',awaiting_confirmation:'待确认'};
const money = (minor:number) => new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(minor/100);
const date = (value:string) => new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
function localInput(days:number){const value=new Date(Date.now()+days*86400000);return new Date(value.getTime()-value.getTimezoneOffset()*60000).toISOString().slice(0,16);}

export default function WorkflowPlans({revision,onOpenTask,onNavigate,onPrompt}:{revision:number;onOpenTask:(id:string,view?:'conversation'|'progress')=>Promise<void>;onNavigate:(page:string)=>void;onPrompt:(text:string)=>void}){
  const [data,setData]=useState<Data|null>(null),[error,setError]=useState(''),[syncError,setSyncError]=useState(''),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
  const [selected,setSelected]=useState<Plan|null>(null),[title,setTitle]=useState(''),[runAt,setRunAt]=useState(()=>localInput(1)),[endAt,setEndAt]=useState(()=>localInput(91));
  const [interval,setIntervalDays]=useState('30'),[target,setTarget]=useState(''),[consent,setConsent]=useState(false);
  const generation=useRef(0),mounted=useRef(false),writing=useRef(false),requests=useRef(new Map<string,string>());
  const activeJobs=data?.jobs.filter(job=>!['completed','cancelled'].includes(data.plans.find(plan=>plan.id===job.task_id)?.status??''))??[];
  const load=useCallback(async()=>{if(writing.current)return;const seq=++generation.current;
    try{const next=await api<Data>('/api/workflows');if(mounted.current&&seq===generation.current){setData(next);setSyncError('');}}
    catch(e){if(mounted.current&&seq===generation.current)setSyncError(e instanceof Error?e.message:'读取计划失败');}},[]);
  useEffect(()=>{mounted.current=true;void load();const timer=window.setInterval(()=>{if(!document.hidden)void load();},5000);
    return()=>{mounted.current=false;generation.current++;window.clearInterval(timer);};},[load]);
  useEffect(()=>{void load();},[load,revision]);
  async function post(path:string,payload:unknown){const {csrf_token}=await api<{csrf_token:string}>('/api/demo/csrf');
    if(!mounted.current)throw Error('会话已变化，请重新核对');
    return api(path,{method:'POST',headers:{'Content-Type':'application/json','X-FinAgent-CSRF':csrf_token},body:JSON.stringify(payload)});}
  async function act(work:()=>Promise<unknown>){if(writing.current)return;writing.current=true;generation.current++;setBusy(true);setError('');setNotice('');
    try{await work();if(mounted.current)setNotice('已保存，请以最新计划和回执为准。');}
    catch(e){if(mounted.current)setError(e instanceof Error?e.message:'响应未收到，请刷新核对原计划。');}
    finally{writing.current=false;if(mounted.current){setBusy(false);await load();}}}
  async function create(){if(!selected)return;
    const amount=target.trim();if(amount&&!/^\d+(\.\d{1,2})?$/.test(amount)){setError('参考金额最多两位小数');return;}
    const payload={task_id:selected.id,title,run_at:new Date(runAt).toISOString(),end_at:new Date(endAt).toISOString(),interval_days:Number(interval),target_minor:amount?Math.round(Number(amount)*100):null,consent};
    const key=JSON.stringify(payload);if(!requests.current.has(key))requests.current.set(key,crypto.randomUUID());
    await act(async()=>{await post('/api/workflows/reviews',{...payload,request_id:requests.current.get(key)});if(mounted.current){setSelected(null);setConsent(false);}});
  }
  return <div className="workflow-plans">
    <section className="section workflow-intro"><h3>围绕目标，串联银行业务</h3><p>查询、分析、办理和后续复查保留在同一目标下。涉及资金或安全的操作仍需核对固定单。</p>
      <div className="workflow-examples">{[['订阅支出优化','帮我核对订阅扣费，分析哪些服务值得保留，先给建议，不要取消或转账。'],['旅行资金规划','结合我的可用资金、消费和近期应付款，分析旅行预算，缺少目标成本时再问我。'],['卡片安全核对','帮我查看最近交易中的异常线索，先核对，不要直接认定盗刷或冻结。']].map(([label,text])=><button className="secondary" key={label} onClick={()=>onPrompt(text)}>{label}</button>)}</div>
    </section>
    {(error||syncError)&&<div className="alert" role="alert">{error||syncError}<button className="secondary" disabled={busy} onClick={()=>void load()}>刷新核对</button></div>}{notice&&<p role="status">{notice}</p>}
    {!!data?.drafts?.length&&<section className="section"><h3>待核对的复查草稿</h3>{data.drafts.map(draft=><article className="workflow-card" key={draft.id}>
      <h4>{draft.title}</h4><p>首次：{date(draft.run_at)} · {draft.interval_days?`每 ${draft.interval_days} 天`:'仅一次'} · 截止：{date(draft.end_at)}</p>
      {draft.target_minor!==null&&<p>参考额：{money(draft.target_minor)}</p>}<p>{draft.scope}</p><p>{draft.notification}</p>
      <button className="secondary" disabled={busy} onClick={()=>{const plan=data.plans.find(p=>p.id===draft.task_id);if(!plan)return;
        const input=(value:string)=>{const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
        setSelected(plan);setTitle(draft.title);setRunAt(input(draft.run_at));setEndAt(input(draft.end_at));setIntervalDays(String(draft.interval_days));setTarget(draft.target_minor===null?'':String(draft.target_minor/100));setConsent(false);
      }}>核对草稿与授权范围</button>
    </article>)}</section>}
    <section className="section"><div className="section-heading"><h3>联动计划与对话历史</h3><button className="text-button" onClick={()=>onNavigate('tasks/occasions')}>查看生活安排详情</button></div>
      {!data?<p>正在读取计划…</p>:!data.plans.length?<p>暂无计划。从上方示例或智能助手提出一个目标。</p>:<div className="workflow-grid">{data.plans.map(plan=><article className="workflow-card" key={plan.kind+plan.id}>
        <div className="workflow-card-heading"><span className="tag">{plan.kind==='occasion'?'生活安排':'用户目标'}</span><span>{labels[plan.status]??plan.status}</span></div><h4>{plan.goals.length>1?plan.goals.map(g=>g.text).join(' · '):plan.title}</h4>
        {!!plan.goals.length&&<ul>{plan.goals.map(g=><li key={g.id}><span>{g.text}</span><small>{labels[g.status]??g.status}</small>{g.next_step&&<p>{g.next_step}</p>}</li>)}</ul>}
        {plan.budget_minor!==undefined&&<p>预算 {money(plan.budget_minor)} · 已支出 {money(plan.spent_minor??0)}</p>}
        {!!plan.recoveries?.length&&<details><summary>未完成事项与处理方式</summary>{plan.recoveries.map(r=><p key={r.node_id}><strong>{r.title}</strong>：{r.message}</p>)}</details>}
        <div className="workflow-actions"><button className="secondary" disabled={busy} onClick={()=>plan.kind==='occasion'?onNavigate(plan.route):void act(()=>onOpenTask(plan.id))}>{plan.kind==='occasion'?'查看采购与回执':'打开对话与固定单'}</button>
          {plan.kind!=='occasion'&&<button className="secondary" disabled={busy} onClick={()=>void act(()=>onOpenTask(plan.id,'progress'))}>查看任务进展</button>}
          {plan.can_review&&<button className="text-button" disabled={busy} onClick={()=>{setSelected(plan);setTitle(plan.title.slice(0,80)+' · 资金复查');setConsent(false);}}>设置后续复查</button>}</div>
      </article>)}</div>}
    </section>
    {selected&&<section className="section workflow-review-form" aria-label="设置只读资金复查"><h3>设置只读资金复查</h3><p>关联目标：{selected.title}</p>
      <form onSubmit={e=>{e.preventDefault();void create();}}><label>复查名称<input required maxLength={100} value={title} onChange={e=>setTitle(e.target.value)}/></label>
        <label>首次时间<input required type="datetime-local" value={runAt} onChange={e=>setRunAt(e.target.value)}/></label><label>授权截止<input required type="datetime-local" value={endAt} onChange={e=>setEndAt(e.target.value)}/></label>
        <label>复查间隔<select value={interval} onChange={e=>setIntervalDays(e.target.value)}><option value="0">仅一次</option><option value="5">每 5 天</option><option value="7">每 7 天</option><option value="30">每 30 天</option>{!['0','5','7','30'].includes(interval)&&<option value={interval}>每 {interval} 天</option>}</select></label>
        <label>可用资金参考额（元，可留空）<input inputMode="decimal" value={target} onChange={e=>setTarget(e.target.value)}/></label>
        <p className="operation-note">每次仅读取本人人民币账户可用资金，形成独立快照。参考额不代表已攒下的钱或已预留资金；不会取消订阅、转账或投资。服务需保持运行，过期任务不补办资金业务。</p>
        <label className="workflow-consent"><input type="checkbox" checked={consent} onChange={e=>setConsent(e.target.checked)}/>我同意以上只读范围、时间和期限</label>
        <div className="workflow-actions"><button disabled={busy||!consent}>确认设置复查</button><button type="button" className="secondary" disabled={busy} onClick={()=>setSelected(null)}>返回</button></div></form>
    </section>}
    <section className="section"><h3>后续复查</h3>{!data?.reviews.length?<p>暂无已授权复查。可从目标卡片设置。</p>:<div className="workflow-grid">{data.reviews.map(r=><article className="workflow-card" key={r.id}><div className="workflow-card-heading"><h4>{r.title}</h4><span>{labels[r.status]??r.status}</span></div>
      <p>{r.status==='scheduled'?'下次':'原约定时间'}：{date(r.next_run_at_utc)}</p><p>截止：{date(r.end_at_utc)} · {r.interval_days?`每 ${r.interval_days} 天`:'仅一次'}</p>
      {r.result.available_minor!==undefined&&<><p>最近快照：{money(r.result.available_minor)}{r.result.as_of_utc&&` · ${date(r.result.as_of_utc)}`}</p>{r.result.gap_minor!==null&&r.result.gap_minor!==undefined&&<p>距参考额差额：{money(r.result.gap_minor)}</p>}<small>{r.result.note}</small></>}
      {r.error_code&&<p>计划已停止或等待处理，请核对目标与授权期限。</p>}
      <div className="workflow-actions">{['scheduled','paused'].includes(r.status)&&<><button className="secondary" disabled={busy} onClick={()=>void act(()=>post(`/api/workflows/reviews/${r.id}/control`,{action:r.status==='paused'?'resume':'pause',version:r.version}))}>{r.status==='paused'?'恢复复查':'暂停复查'}</button><button className="text-button" disabled={busy} onClick={()=>void act(()=>post(`/api/workflows/reviews/${r.id}/control`,{action:'cancel',version:r.version}))}>取消后续复查</button></>}</div>
    </article>)}</div>}</section>
    {!!data?.reservations.length&&<section className="section"><h3>已预留资金明细</h3>{data.reservations.map(r=><p key={r.plan_id}>{r.purpose} · {money(r.amount_minor)} <button className="text-button" onClick={()=>onNavigate(r.route)}>查看安排</button></p>)}</section>}
    {!!activeJobs.length&&<section className="section"><h3>回执后的后续处理</h3>{activeJobs.map(j=><p key={j.id}>{j.status==='blocked'?'后续分析待处理，已发生业务以原回执为准。':'回执已保存，正在继续后续目标。'} <button className="text-button" disabled={busy} onClick={()=>void act(()=>onOpenTask(j.task_id))}>查看原对话</button></p>)}</section>}
  </div>;
}
