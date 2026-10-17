import type {AgentTask} from './useAgentWorkspace';
const states:Record<string,string>={succeeded:'已完成',completed:'已完成',partially_completed:'部分完成',failed:'未完成',skipped:'前置步骤未完成',pending:'待处理',running:'正在分析',awaiting_confirmation:'待手动确认',superseded:'已由新问题替代'};
export default function AnalysisWorkflowSummary({workflow:w}:{workflow:AgentTask['analysis_workflow']}){
 if(!w)return null;
 return <details className="goal-details" aria-label="分析工作流"><summary>工具执行过程 · {states[w.status]??w.status}</summary><p>{w.objective}</p><p className="caption">{w.read_only?'查询与分析记录':'查询、分析与办理记录'}；涉及授权时等待你手动确认。</p><ol>{w.nodes.map(n=><li key={n.id}><strong>{n.title}</strong> · {states[n.status]??n.status}{!!n.depends_on.length&&<p className="caption">依赖：{n.depends_on.map(id=>w.nodes.find(x=>x.id===id)?.title??id).join('、')}</p>}</li>)}</ol></details>;
}
