export type Condition = { type: string; start?: string; end?: string; category?: string;
  account_id?: string | null; threshold_minor?: number; transaction_id?: string;
  card_last4?: string; amount_minor?: number; posted_at_utc?: string };
export type Validation = { met: boolean; complete: boolean; condition?: Condition;
  evidence?: { current?: { totals: { net_minor: number } } } };
const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);
export default function ConditionDetails({ condition, validation }: { condition?: Condition; validation?: Validation | null }) {
  if (!condition) return null;
  return <div className="condition-details">
    <strong>执行条件</strong>
    {['spending_gt', 'spending_lt'].includes(condition.type) ? <>
      <p>{condition.start} 至 {condition.end} · {condition.category} · {condition.account_id ? '指定账户' : '本人全部账户'}</p>
      <p>净消费须严格{condition.type === 'spending_lt' ? '小于' : '大于'} {money(condition.threshold_minor ?? 0)}（消费减退款）</p>
      {validation?.evidence?.current && <p>最近核验净消费：{money(validation.evidence.current.totals.net_minor)} · {validation.complete ? validation.met ? '条件成立' : '条件不成立' : '数据不完整'}</p>}
      <small>确认时会按以上固定区间重新核验。条件不成立或数据不完整时不会转账。</small>
    </> : <>
      <p>所选消费 {money(condition.amount_minor ?? 0)} · {condition.posted_at_utc && new Date(condition.posted_at_utc).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间）</p>
      <p>仅冻结这笔交易关联的尾号 {condition.card_last4} 卡片。</p>
      <small>确认时再次核对交易关联卡片及状态。</small>
    </>}
  </div>;
}
