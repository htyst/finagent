import type { AgentWorkspace,AuthorizationOperation,Prerequisite } from './useAgentWorkspace';
import { Terms,keyTerms } from './SecurityGate';
import { labels } from './taskPresentation';
import ConditionDetails from './ConditionDetails';
import { useWorkspaceDraft } from './WorkspaceDrafts';
import Icon from './Icon';
import './ConversationExtras.css';

const money=(n:number)=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(n/100);
const local=(date:string)=>new Date(date).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});

function Operation({workspace:w,operation:o}:{workspace:AgentWorkspace;operation:AuthorizationOperation}){
  const pending=o.status==='pending_confirmation';
  const proposal=o.resource_kind==='proposal'?w.task?.proposal:null;
  return <article className="conversation-authorization proposal-card" aria-label={`${o.label}办理单`} data-testid={o.resource_kind==='proposal'?'operation-card':'authorization-card'} data-operation-kind={o.resource_kind}>
    <div className="authorization-heading"><strong><Icon name="shield"/>{o.label}</strong><span className={`tag ${pending?'warning':''}`}>{labels[o.status]??o.status}</span></div>
    <div className="authorization-body">
      <p className="authorization-level">{pending?(o.level==='red'?'需要独立强验证和本人确认':'需要本人明确确认'):'固定办理记录'}</p>
      {(o.amount_minor>0||o.action==='transfer')&&<p>金额／预算 <strong className="proposal-amount">{money(o.amount_minor)}</strong></p>}
      <div className="authorization-key-terms"><Terms value={keyTerms(o)}/></div>
      {proposal&&<ConditionDetails condition={proposal.summary.condition} validation={proposal.validation}/>}
      <p className="caption">计划版本 {o.plan_version??w.task?.plan_version} · 固定单 {o.resource_id.slice(-8)}</p>
      <details><summary>核对完整固定条款</summary><Terms value={o.terms}/></details>
      {pending&&<><p className="caption">本单有效至 {local(o.expires_at)}。确认只适用于这份固定单，修改信息后需要重新核对。</p>
        {o.blocked_reason&&<p className="coverage-warning" role="status">{o.blocked_reason}</p>}
        <div className="proposal-actions"><button type="button" disabled={w.unavailable||!o.can_confirm} onClick={()=>void w.confirmAuthorization(o)}>核对并继续</button><button type="button" className="secondary" disabled={w.unavailable} onClick={()=>void w.cancelAuthorization(o)}>取消本单</button></div>
        <p className="caption">下一步在当前页面确认具体条款；需要强验证时会显示独立安全窗口。聊天中的“确认”不能代替授权。</p>
      </>}
      {o.status==='submitted'&&<p role="status">{o.action==='credit_limit'?'调额申请已提交，等待独立模拟审批；授信额度尚未改变。':'申请已提交，等待银行审批，尚未发卡。'}</p>}
      {o.status==='scheduled'&&<p role="status">已授权预约，将在约定时间办理；当前不表示资金已转出。</p>}
      {o.status==='collecting'&&<p role="status">正在等待成员付款，实际到账以回执为准。</p>}
      {o.receipt&&<div className="receipt" role="status"><strong>{typeof o.receipt.message==='string'?o.receipt.message:'办理结果已保存'}</strong><details><summary>查看持久回执</summary><Terms value={o.receipt}/></details></div>}
      {!pending&&!o.receipt&&['cancelled','expired','invalidated','failed'].includes(o.status)&&<p>这份单不能继续授权，请按需要重新准备并核对。</p>}
    </div>
  </article>;
}

function Assessment({workspace:w,action}:{workspace:AgentWorkspace;action:Prerequisite}){
  const [answers,setAnswers]=useWorkspaceDraft<string[]>(`assessment:${w.task!.id}:${action.pending_id}`,()=>action.questions.map(()=>''));
  return <form className="conversation-assessment" aria-label="对话风险评估" onSubmit={event=>{event.preventDefault();if(answers.every(v=>v!==''))void w.submitAssessment(action.pending_id,answers.map(Number));}}>
    <h3>先完成风险评估，再继续原需求</h3><p>{action.effect}</p>
    <div className="assessment-fields">{action.questions.map((q,i)=><label key={q.label}>{q.label}<select value={answers[i]} required disabled={w.unavailable} onChange={e=>setAnswers(old=>old.map((v,n)=>n===i?e.target.value:v))}><option value="">请选择实际情况</option>{q.options.map((option,n)=><option key={option} value={n}>{option}</option>)}</select></label>)}</div>
    <button disabled={w.unavailable||answers.some(v=>v==='')}>{action.label}</button><p className="caption">评估结果按现有规则计算。风险不匹配、余额不足或产品锁定等业务限制不能用确认跳过。</p>
  </form>;
}

export default function ConversationAuthorizations({workspace:w}:{workspace:AgentWorkspace}){
  if(!w.task)return null;
  return <div className="conversation-authorizations">{w.task.prerequisites?.map(a=><Assessment key={a.pending_id} workspace={w} action={a}/>)}{w.task.authorization_operations?.map(o=><Operation key={`${o.resource_kind}:${o.resource_id}`} workspace={w} operation={o}/>)}</div>;
}
