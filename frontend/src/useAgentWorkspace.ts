import type {ExecutionGraph} from './executionGraph';
import { useEffect,useRef,useState } from 'react';
import { api,ApiError } from './api';
import type { SecurityOperation } from './api';
import { useTaskSync } from './useTaskSync';
import type { Condition, Validation } from './ConditionDetails';
import type { BirthdayDetails, TransferDetails } from './TaskDetailsForm';
import type { TransactionPage, TransactionFilters } from './TransactionEvidence';
export type Proposal = { id: string; task_id: string; kind: string; status: string; expires_at_utc: string;
  summary: { action: string; source?: { alias: string; mask: string }; target?: { alias: string; mask: string; phone_mask?: string | null; bank_name?: string; account_number?: string };
    card?: { alias: string; mask: string }; amount_minor?: number; impact: string; condition?: Condition }; validation?: Validation | null;
  receipt?: { order_id?: string; committed_at_utc?: string; message: string; amount_minor?: number; source_balance_minor?: number; recipient_credit_minor?: number; recipient_credit_id?: string; card_status?: string } | null };
export type AuthorizationOperation=SecurityOperation & {plan_version?:number;terms_sha256?:string;action_id:string;authorization_path:string;label:string;status:string;expires_at:string;can_confirm:boolean;blocked_reason:string|null;receipt:Record<string,unknown>|null};
export type Prerequisite={kind:'risk_assessment';pending_id:string;label:string;effect:string;questions:Array<{label:string;options:string[]}>};
type OutgoingMessage={id:number;text:string;newTask:boolean;taskId?:string;afterCount:number;status:'sending'|'uncertain'|'failed'};
function delivered(task:AgentTask|null|undefined,pending:OutgoingMessage){
  if(!task||(pending.newTask?task.id===pending.taskId:task.id!==pending.taskId))return false;
  return task.messages.slice(pending.newTask?0:pending.afterCount).some(m=>m.role==='user'&&m.content===pending.text);
}
export type AgentTask = { service_request?: {id:string;kind:string} | null; birthday_details?: BirthdayDetails | null; transfer_details?: TransferDetails | null; wealth_order?: { id: string } | null; card_request?: { id: string } | null; subscription_request?: { id: string } | null; birthday_request?: { id: string } | null; id: string; mode: string; intent: string | null; status: string; proposal: Proposal | null;
  messages: Array<{ role: string; content: string }>;
  requested_outcomes?: Array<string | {text: string; kind?: string}>;
  execution_graph?:ExecutionGraph | null;
  analysis_workflow?: {id:string;objective:string;status:string;read_only:boolean;nodes:Array<{id:string;title:string;name:string;status:string;depends_on:string[]}>} | null;
  next_actions?: Array<{kind:string;pending_id:string;label:string;scope:string;effect:string}>;
  authorization_operations?:AuthorizationOperation[];prerequisites?:Prerequisite[];
  control?: { state?: string; reason?: string; suggestion?: string };
  plan_version: number; query_range?: string[];
  candidates: Array<{ id: string; merchant: string | null; amount_minor: number; posted_at_utc: string; card_last4: string | null }>;
  steps: Array<{ ordinal: number; tool_name: string; status: string; arguments?: TransactionFilters; result: Partial<TransactionPage> & {
    reason?: string; message?: string; available_balance_minor?: number; alias?: string; mask?: string;
    receipt?: { status?: string }; already_in_state?: boolean; status?: string; validation?: Validation; condition?: Condition; evidence?: Validation['evidence']; met?: boolean; complete?: boolean } }> };

export function useAgentWorkspace(onChanged: () => Promise<void>, revision: number) {
  const { task, setTask, busy, setBusy, loading, syncError, updatedAt, refresh, retry } = useTaskSync<AgentTask>('/api/agent/tasks/current', onChanged, revision);
  const [forceNew,setForceNew]=useState(false);
  function startNewConversation(){if(busy)return;setForceNew(true);setInput('');setError('');setNotice('已开始新对话，原任务及回执保留在服务端，可按任务ID查询。');}
  const [input, setInput] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [outgoing,setOutgoing]=useState<OutgoingMessage|null>(null);
  const outgoingId=useRef(0);
  useEffect(()=>{setOutgoing(current=>current&&delivered(task,current)?null:current);},[task]);
  const unavailable = busy || loading || !!syncError;
  async function write<T>(path: string, payload?: unknown): Promise<T> {
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json',
      'X-FinAgent-CSRF': csrf_token }, ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
  }
  async function sendMessage(message: string) {
    if (!message || unavailable) return;
    if(/^\d{6}$/.test(message)||/(?:验证码|动态码|短信码|OTP|密码|confirmation_token|operator_token)\s*[:：=]?\s*[A-Za-z0-9_./+\-=]{4,}/i.test(message)){setError('认证信息请只输入独立安全窗口，此消息未发送。');return;}
    if (!setBusy(true)) return; setError(''); setNotice('');
    // A completed answer/payment still belongs to this conversation. Keep
    // receipt and reference context until explicit New conversation; terminated
    // or expired tasks require a fresh task and cannot be implicitly replayed.
    const active = !forceNew && task && !['cancelled','expired'].includes(task.status);
    const pending:OutgoingMessage={id:++outgoingId.current,text:message,newTask:!active,taskId:task?.id,afterCount:task?.messages.length??0,status:'sending'};
    setOutgoing(pending);setInput('');
    let acknowledged=false;
    try {
      const result = active
        ? await write<AgentTask>(`/api/tasks/${task.id}/messages`, { message })
        : await write<AgentTask>('/api/tasks', { message });
      acknowledged=true;setForceNew(false);setTask(result);setOutgoing(null);
      await onChanged();
    } catch (err) {
      if(!acknowledged){
        let saved=false;
        try { saved=delivered(await refresh(),pending); } catch { /* Keep the local question and offer read-only recovery. */ }
        if(saved){setOutgoing(null);setNotice('问题已在服务端保存，请查看最新回答或核对任务状态；没有重复发送。');}
        else{setOutgoing({...pending,status:err instanceof ApiError&&err.status>=400&&err.status<500?'failed':'uncertain'});setInput(current=>current||message);}
      }
      setError(err instanceof Error ? err.message : '对话请求失败');
    }
    finally { setBusy(false); }
  }
  async function submitDetails(kind: 'birthday' | 'transfer', values: Record<string, string>) {
    if (!task || unavailable) return;
    if (!setBusy(true)) return; setError(''); setNotice('');
    try {
      setTask(await write<AgentTask>(`/api/tasks/${task.id}/${kind}-details`, values));
      await onChanged();
    } catch (err) {
      try { await refresh(task.id); } catch { /* Keep submitted inputs visible until recovery. */ }
      setError(err instanceof Error ? err.message : '资料提交失败');
    } finally { setBusy(false); }
  }
  async function confirm(proposal: Proposal) {
    const action=task?.authorization_operations?.find(a=>a.resource_kind==='proposal'&&a.resource_id===proposal.id);
    if(action)return confirmAuthorization(action);
    if (!setBusy(true)) return; setError(''); setNotice('');
    try {
      const { confirmation_token } = await write<{ confirmation_token: string }>(`/api/proposals/${proposal.id}/confirmation-token`);
      await write(`/api/proposals/${proposal.id}/confirm`, { confirmation_token });
      if (task) await refresh(task.id);
      await onChanged();
    } catch (err) {
      // A lost response after commit is resolved from the durable task state.
      try {
        if (task && (await refresh(task.id))?.proposal?.status === 'executed') { setNotice('已核对服务端，操作已成功，回执已恢复。'); await onChanged(); return; }
      } catch { /* Preserve the original request error. */ }
      setError(err instanceof Error ? err.message : '确认失败');
    } finally { setBusy(false); }
  }
  async function confirmAuthorization(action:AuthorizationOperation){
    if(!task||unavailable||!action.can_confirm||!setBusy(true))return;
    const taskId=task.id;
    setError('');setNotice('');
    try{
      for(let attempt=0;attempt<2;attempt++){
        try{
          const {confirmation_token}=await write<{confirmation_token:string}>(action.authorization_path+'/confirmation-token',{action_id:action.action_id});
          setTask(await write<AgentTask>(action.authorization_path+'/confirm',{action_id:action.action_id,confirmation_token}));
          await onChanged();return;
        }catch(e){
          // Policy may upgrade between viewing terms and committing. A fresh
          // visible verification is required; no automatic payment retry.
          if(attempt===0&&e instanceof ApiError&&['MFA_REQUIRED','REAUTHORIZATION_REQUIRED'].includes(e.code)){
            setNotice('安全要求已更新，请重新核对本单并完成验证。');continue;
          }
          throw e;
        }
      }
    }catch(e){
      if(e instanceof ApiError&&e.code==='AUTHORIZATION_CANCELLED')setNotice('已暂缓授权，本单未因取消验证而执行。可稍后重新核对，或取消本单。');
      else{
        try{
          const recovered=await refresh(taskId);
          const result=recovered?.authorization_operations?.find(a=>a.resource_kind===action.resource_kind&&a.resource_id===action.resource_id);
          if(result&&['executed','settled','submitted','issued','scheduled','collecting','completed'].includes(result.status)){
            setNotice('已重新核对服务端，办理结果已恢复，请查看当前状态和回执。');await onChanged();return;
          }
        }catch{/* Offer read-only recovery; never replay an uncertain write. */}
        setError(e instanceof Error?e.message:'本单未完成，请查看最新状态后继续');
      }
    }finally{setBusy(false);}
  }
  async function cancelAuthorization(action:AuthorizationOperation){
    if(!task||unavailable||!setBusy(true))return;
    setError('');setNotice('');
    try{setTask(await write<AgentTask>(action.authorization_path+'/cancel',{action_id:action.action_id}));await onChanged();}
    catch(e){try{await refresh(task.id);}catch{/* Preserve recovery controls. */}setError(e instanceof Error?e.message:'取消未完成，请核对状态');}
    finally{setBusy(false);}
  }
  async function submitAssessment(pendingId:string,answers:number[]){
    if(!task||unavailable||!setBusy(true))return;
    setError('');setNotice('');
    try{setTask(await write<AgentTask>(`/api/tasks/${task.id}/risk-assessment`,{pending_id:pendingId,answers}));await onChanged();}
    catch(e){try{await refresh(task.id);}catch{/* Keep submitted questionnaire visible for recovery. */}setError(e instanceof Error?e.message:'风险评估提交未完成');}
    finally{setBusy(false);}
  }
  async function taskAction(action: 'cancel' | 'reject-operation' | 'resume' | 'pause' | 'handoff' | 'continue') {
    if (!task) return;
    if (!setBusy(true)) return; setError(''); setNotice('');
    try { setTask(await write<AgentTask>(`/api/tasks/${task.id}/${['pause', 'handoff', 'continue'].includes(action) ? 'control' : action}`,
      ['pause', 'handoff', 'continue'].includes(action) ? { action: action === 'continue' ? 'resume' : action, reason: '用户页面请求' } : undefined)); await onChanged(); }
    catch (err) { try { await refresh(task.id); } catch { /* Read-only retry is offered below. */ } setError(err instanceof Error ? err.message : '操作失败'); }
    finally { setBusy(false); }
  }
  async function confirmSubscriptionQuery(pendingId:string){
    if(!task||unavailable||!setBusy(true))return;
    setError('');setNotice('');
    try{setTask(await write<AgentTask>(`/api/tasks/${task.id}/subscription-query-confirmation`,{pending_id:pendingId}));await onChanged();}
    catch(e){try{await refresh(task.id);}catch{/* Read-only state recovery. */}setError(e instanceof Error?e.message:'订阅查询未完成，请重试');}
    finally{setBusy(false);}
  }
  async function openTask(id:string){
    if(unavailable||!setBusy(true))throw Error('当前任务正在处理或同步，请稍后再打开历史任务。');
    try{await refresh(id);setForceNew(false);setOutgoing(null);setInput('');setError('');}
    finally{setBusy(false);}
  }
  return { task:forceNew?null:task, startNewConversation, openTask, outgoing, input, setInput, error, notice, busy, loading, syncError, updatedAt, unavailable, retry, sendMessage, submitDetails, confirm, confirmAuthorization, cancelAuthorization, submitAssessment, taskAction, confirmSubscriptionQuery };
}
export type AgentWorkspace = ReturnType<typeof useAgentWorkspace>;
