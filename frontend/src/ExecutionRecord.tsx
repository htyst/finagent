import { useRef, useState, useEffect } from 'react';
import { api } from './api';
type Audit = { decision: { plan_version: number; policy_version: string; status: string }; resources: Array<{kind:string;id:string;status:string}>; proposal_events: Array<{id:string;event:string;code:string|null;created_at_utc:string}>; limitations: string[] };
export default function ExecutionRecord({ taskId }: { taskId: string }) {
  const [record,setRecord]=useState<Audit|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  const mounted=useRef(false);
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
  async function load() {
    setBusy(true);setError('');
    try {const next=await api<Audit>(`/api/tasks/${taskId}/audit`);if(mounted.current)setRecord(next);}
    catch(e){if(mounted.current)setError(e instanceof Error?e.message:'执行记录读取失败');}
    finally{if(mounted.current)setBusy(false);}
  }
  return <div className="execution-record"><button className="secondary" disabled={busy} onClick={()=>void load()}>{busy?'正在读取…':'查看服务端审计记录'}</button>{error&&<p role="alert">{error}</p>}{record&&<><p>计划版本 {record.decision.plan_version} · 策略版本 {record.decision.policy_version}</p>{record.resources.map(r=><p key={r.kind}>资源 {r.kind} · {r.id} · {r.status}</p>)}<ul>{record.proposal_events.map(e=><li key={e.id}>{new Date(e.created_at_utc).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})} · {e.event}{e.code&&` · ${e.code}`}</li>)}</ul>{!record.proposal_events.length&&<p>当前记录未返回关联操作事件。</p>}{record.limitations.map(s=><p key={s}>{s}</p>)}</>}</div>;
}
