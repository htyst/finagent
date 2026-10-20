import {labels} from './taskPresentation';
import type {AgentTask} from './useAgentWorkspace';
import {executionStates,isAnswerNode,answerAttemptState} from './executionGraph';
const states:Record<string,string>={...executionStates,succeeded:'已完成',skipped:'前置步骤未完成',awaiting_confirmation:'待手动确认',superseded:'已由新问题替代'};

export default function AnalysisWorkflowSummary({workflow:w,graph:g}:{workflow:AgentTask['analysis_workflow'];graph?:AgentTask['execution_graph']}){
 if(g){
  const execution=g.nodes.filter(node=>!isAnswerNode(node));
  const answers=g.nodes.filter(isAnswerNode);
  const rejected=answers.filter(node=>node.status==='failed');
  return <section className="execution-progress" aria-label="任务目标进度">
   <div className="execution-heading"><strong>当前目标</strong><span role="status">{states[g.status]??g.status}</span></div>
   <ul className="execution-goals">{g.goals.map(goal=><li key={goal.id}>
    <div><strong>{goal.text}</strong><span className={`execution-state is-${goal.status}`}>{goal.status==='waiting_authorization'&&g.nodes.some(n=>n.goal_ids?.includes(goal.id)&&n.status==='waiting_authorization'&&(n as {evaluated_risk?:string}).evaluated_risk==='red')?'待独立验证及确认':states[goal.status]??goal.status}</span></div>
    {goal.next_step&&<p>{goal.next_step}</p>}{!!goal.receipt_refs.length&&<p className="caption">已取得业务回执；付款、受理或预约状态以固定单为准。</p>}
   </li>)}</ul>
   {!!execution.length&&<details className="goal-details" data-testid="execution-tools">
    <summary>实际工具记录 · {execution.length} 次调用</summary>
    <p className="caption">计划版本 {g.plan_version} · 串行执行；记录调用与证据，不展示模型内部推理。</p>
    <ol>{execution.map(n=><li key={n.id}><strong>{n.title}</strong> · {n.invalidated?'已失效（保留记录）':n.recovered_by?'参数已修正':states[n.status]??n.status}
     {n.domain_status&&<span> · 业务状态：{labels[n.domain_status]??n.domain_status}</span>}
     {!!n.depends_on.length&&<p className="caption">前置调用：{n.depends_on.map(id=>g.nodes.find(x=>x.id===id)?.title??id).join('、')}</p>}
     {n.error_code&&<p className="caption">错误：{n.error_code}</p>}
    </li>)}</ol>
   </details>}
   {!!rejected.length&&<details className="goal-details" data-testid="answer-adjustments">
    <summary>回答调整记录 · {rejected.length} 次校验未通过</summary>
    <p className="caption">这些是答复生成和校验记录。是否办理成功以上方目标状态及实际业务回执为准。</p>
    <ol>{answers.map(n=>{
     const state=answerAttemptState(g.nodes,n);
     const label=state==='historical'?'历史回答（已失效）':n.status==='failed'?(state==='adjusted'?'回答已重新生成':'回答仍待调整'):states[n.status]==='已完成'?'回答通过校验':states[n.status]??n.status;
     return <li key={n.id}><strong>{label}</strong>{n.error_code&&<p className="caption">校验代码：{n.error_code}</p>}</li>;
    })}</ol>
   </details>}
  </section>;
 }
 if(!w)return null;
 return <details className="goal-details" aria-label="分析工作流"><summary>工具执行过程 · {states[w.status]??w.status}</summary><p>{w.objective}</p><p className="caption">{w.read_only?'查询与分析记录':'查询、分析与办理记录'}；涉及授权时等待你手动确认。</p><ol>{w.nodes.map(n=><li key={n.id}><strong>{n.title}</strong> · {states[n.status]??n.status}{!!n.depends_on.length&&<p className="caption">依赖：{n.depends_on.map(id=>w.nodes.find(x=>x.id===id)?.title??id).join('、')}</p>}</li>)}</ol></details>;
}
