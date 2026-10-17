import { useEffect, useState } from 'react';
import { api } from './api';
import { Terms } from './SecurityGate';

type Entry = { id: string; kind: string; label: string; status: string; summary: Record<string, unknown>; receipt: Record<string, unknown> | null };
const states: Record<string,string> = { pending_confirmation:'待本人确认', submitted:'待独立审批', executed:'已办理', rejected:'未获批准', cancelled:'已取消', invalidated:'已失效', expired:'已过期' };
export default function ServiceRequests({ revision, onChanged }: { revision: number; onChanged: () => Promise<void> }) {
  const [items,setItems]=useState<Entry[]>([]),[key,setKey]=useState(''),[reason,setReason]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  async function load(){const data=await api<{items:Entry[]}>('/api/service-requests');setItems(data.items);}
  useEffect(()=>{let active=true;api<{items:Entry[]}>('/api/service-requests').then(d=>{if(active)setItems(d.items);}).catch(e=>{if(active)setError(String(e));});return()=>{active=false;};},[revision]);
  async function decide(id:string,decision:'approve'|'reject'){
    setBusy(true);setError('');
    try { const {csrf_token}=await api<{csrf_token:string}>('/api/demo/csrf');
      await api(`/api/service-requests/${id}/decision`,{method:'POST',headers:{'Content-Type':'application/json','X-FinAgent-CSRF':csrf_token,'X-FinAgent-Approver':key},body:JSON.stringify({decision,reason})});
      setKey('');await onChanged();await load();
    } catch(e){setError(e instanceof Error?e.message:String(e));}finally{setBusy(false);}
  }
  return <section className="operation-note"><h3>补卡与授信申请记录</h3><p>补卡和调额从对话发起并由本人确认。调额提交后，还需持独立本机凭据的模拟审批员处理。</p>
    {error&&<p role="alert">{error}</p>}{!items.length&&<p>暂无服务申请。</p>}
    {items.map(r=><article key={r.id}><strong>{r.label} · {states[r.status]??r.status}</strong><details><summary>固定条款与回执</summary><Terms value={r.summary}/>{r.receipt&&<Terms value={r.receipt}/>}</details>
      {r.kind==='credit_limit'&&r.status==='submitted'&&<details><summary>独立模拟审批</summary><p>本人验证码及确认不能代替审批员凭据。</p>
        <label>审批员凭据<input type="password" autoComplete="off" value={key} disabled={busy} onChange={e=>setKey(e.target.value)}/></label>
        <label>审批理由<input value={reason} maxLength={200} disabled={busy} onChange={e=>setReason(e.target.value)}/></label>
        <button type="button" disabled={busy||!key||!reason.trim()} onClick={()=>void decide(r.id,'approve')}>模拟审批通过</button>
        <button type="button" className="secondary" disabled={busy||!key||!reason.trim()} onClick={()=>void decide(r.id,'reject')}>模拟审批拒绝</button>
      </details>}</article>)}
  </section>;
}
