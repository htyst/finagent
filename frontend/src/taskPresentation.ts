import type { AgentTask } from './useAgentWorkspace';
export const labels: Record<string, string> = { submitted:'已提交，等待独立审批', rejected:'未获批准', scheduled: '已预约', collecting: '收款中', created: '待提出需求', gathering_info: '待补充信息', running_readonly: '查询中', awaiting_confirmation: '待确认', completed: '已完成', partially_completed: '部分完成', cancelled: '已取消', expired: '已过期', failed: '未完成', executed: '已执行', invalidated: '已失效', pending_confirmation: '待确认', pending: '等待前序步骤', running: '处理中', succeeded: '已完成', skipped: '已跳过' };
const toolLabels: Record<string, string> = { prepare_card_replacement:'准备借记卡补发',prepare_credit_limit_change:'准备授信调整申请',prepare_merchant_contract_cancel:'准备取消会员续订',read_service_requests:'查询服务申请与审批回执', identify_subscriptions: '从流水识别疑似订阅', prepare_subscription: '准备订阅办理单', read_subscriptions: '查询代扣与提醒状态', validate_requirement: '核对语义和业务规则', prepare_schedule: '准备预约转账', prepare_collection: '准备 AA 收款', list_transactions: '核对交易', prepare_card_action: '准备卡片操作单', list_accounts: '查询账户余额', summarize_spending: '计算消费与退款', generate_statement: '生成账单报告', inspect_anomalies: '筛查待核实交易', prepare_transfer: '准备转账操作单' };
Object.assign(labels,{accepted:'已受理',settled:'已结算',submitted:'待银行审批',issued:'已发卡',confirmed:'已确认',revoked:'已解除'});
Object.assign(toolLabels,{prepare_wealth:'准备理财固定单',prepare_card:'准备卡片办理单',prepare_birthday:'准备生日安排单',compare_wealth:'比较模拟理财产品'});
Object.assign(toolLabels,{analyze_finances:'汇总金融收支',read_financial_history:'读取金融历史档案',read_financial_profile:'读取金融档案',read_wealth:'查询理财持仓',list_cards:'查询全部银行卡'});
export function stepTitle(step: AgentTask['steps'][number]) {
  if (step.result.already_in_state) return '核对卡片状态 · 已处于目标状态，无需执行';
  const executed = step.status === 'succeeded' && !!step.result.receipt;
  const title = executed && step.tool_name.startsWith('prepare_')
    ? ({ prepare_transfer: '模拟转账', prepare_card_action: '卡片操作', prepare_schedule: '预约转账', prepare_collection: 'AA 收款' } as Record<string, string>)[step.tool_name] ?? '操作'
    : toolLabels[step.tool_name] ?? '查询';
  return `${title} · ${labels[step.status] ?? step.status}`;
}
