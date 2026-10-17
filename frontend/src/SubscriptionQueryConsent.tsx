import './ConversationExtras.css';
import type {AgentWorkspace} from './useAgentWorkspace';
export default function SubscriptionQueryConsent({workspace:w}:{workspace:AgentWorkspace}){
 const action=w.task?.next_actions?.find(a=>a.kind==='confirm_subscription_query');
 if(!action)return null;
 return <section className="subscription-query-consent" aria-label="订阅查询确认"><h3>查看我的订阅与续费</h3><p>{action.scope}</p><p className="operation-note">{action.effect}</p><button disabled={w.unavailable} onClick={()=>void w.confirmSubscriptionQuery(action.pending_id)}>{w.busy?'正在查询…':action.label}</button><p className="caption">确认后直接继续刚才的问题，无需重新输入。</p></section>;
}
