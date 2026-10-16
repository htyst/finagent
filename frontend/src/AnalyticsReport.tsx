import { useState } from 'react';
import { api } from './api';

type Category = { category: string; purchase_minor: number; refund_minor: number; net_minor: number };
type Totals = { purchase_minor: number; refund_minor: number; net_minor: number; is_complete: boolean };
type Period = { start_date: string; end_exclusive_date: string };
type EvidenceRow = { transaction_id: string; merchant: string | null; amount_minor: number; signed_minor: number; category: string; type: string; posted_at_utc: string; original_transaction_id: string | null };
type Evidence = { items: EvidenceRow[]; total: number; limit: number; offset: number };
type Spending = { report_kind?: string; title?: string; current: { period: Period; totals: Totals; categories: Category[]; evidence: Evidence }; account_ids: string[]; category: string | null;
  comparison: { period: Period; totals: Totals; delta_minor: number | null; percent_change: string | null } | null;
  months?: Array<{ month: string; totals: Totals | null; categories: Category[]; observed_totals: Totals }>;
  complete_months?: string[]; missing_months?: string[]; ranking_scope?: string; highest_months?: Array<{ month: string; net_minor: number }> };
type Anomalies = { report_kind: 'anomalies'; period: Period; scanned_count: number; candidate_count: number; unflagged_count: number; coverage: { status: string };
  rules: { large_amount: { median_minor: number; threshold_minor: number; enabled: boolean }; near_duplicate: { window_seconds: number } };
  candidates: Array<{ transaction_id: string; merchant: string | null; amount_minor: number; posted_at_utc: string; reasons: Array<{ rule: string; reason: string; related_transaction_ids: string[] }> }>; conclusion: string };
const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);
const end = (p: Period) => new Date(Date.parse(p.end_exclusive_date + 'T00:00:00Z') - 86400000).toISOString().slice(0, 10);
const range = (p: Period) => `${p.start_date} 至 ${end(p)}`;
const days = (p: Period) => Math.round((Date.parse(p.end_exclusive_date) - Date.parse(p.start_date)) / 86400000);
const stamp = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });

function SpendingReport({ result }: { result: Spending }) {
  const [evidence, setEvidence] = useState(result.current.evidence);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const current = result.current;
  async function load(offset: number) {
    setBusy(true); setError('');
    const params = new URLSearchParams({ start: current.period.start_date, end: end(current.period), comparison_mode: 'none', evidence_limit: String(evidence.limit), evidence_offset: String(offset) });
    if (result.category) params.set('category', result.category);
    if (result.account_ids.length === 1) params.set('account_id', result.account_ids[0]);
    try { const value = await api<Spending>(`/api/spending?${params}`); setEvidence(value.current.evidence); }
    catch (err) { setError(err instanceof Error ? err.message : '明细读取失败'); }
    finally { setBusy(false); }
  }
  return <article className="analytics-report" aria-label={result.title ?? '消费统计报告'}>
    <div className="analytics-overview">
      <h3>{result.title ?? (result.comparison ? '净消费区间比较' : '消费统计与明细')}</h3>
      <p>{range(current.period)}（北京时间） · {result.category ?? '全部类别'} · 本人所选账户 {result.account_ids.join('、')}</p>
      <p className="operation-note">仅统计已入账消费与退款；净消费＝消费－退款；收入和本人转账排除。类别、月份与总额使用相同口径。</p>
      <p><strong>{current.totals.is_complete ? '净消费' : '已知净消费'} {money(current.totals.net_minor)}</strong> · 消费 {money(current.totals.purchase_minor)} · 退款 {money(current.totals.refund_minor)}</p>
      {!current.totals.is_complete && <p className="coverage-warning">数据不完整，已知金额不能当作完整期间总额；缺失数据不是零消费。</p>}
      <div className="analytics-table-wrap"><table><thead><tr><th>类别</th><th>消费</th><th>退款</th><th>净消费</th></tr></thead><tbody>{current.categories.map(c => <tr key={c.category}><th>{c.category}</th><td>{money(c.purchase_minor)}</td><td>{money(c.refund_minor)}</td><td>{money(c.net_minor)}</td></tr>)}</tbody></table></div>
      {current.categories.length > 0 && <p>主要支出：{[...current.categories].sort((a, b) => b.net_minor - a.net_minor)[0].category}，净消费 {money(Math.max(...current.categories.map(c => c.net_minor)))}</p>}
      {result.months && <><p>已完整覆盖月份：{result.complete_months?.join('、') || '暂无'}</p>
        {!!result.missing_months?.length && <p className="coverage-warning">缺失或不完整月份：{result.missing_months.join('、')}。无法确定全年净消费；下方仅按已完整覆盖月份排名。</p>}
        <p>{result.ranking_scope === 'full_year' ? '全年' : '已完整覆盖月份中'}净消费最高（所有并列）：{result.highest_months?.map(m => `${m.month} ${money(m.net_minor)}`).join('；') || '暂无可排名月份'}</p></>}
      {result.comparison && <div className="analytics-comparison"><h4>与基准区间比较</h4><p>当前：{range(current.period)}，{days(current.period)} 天，{money(current.totals.net_minor)}；基准：{range(result.comparison.period)}，{days(result.comparison.period)} 天，{money(result.comparison.totals.net_minor)}。</p>
        {days(current.period) !== days(result.comparison.period) && <p>两个区间不等长；这里比较累计净消费，不作为上期同期或消费速度结论。</p>}
        <p>{result.comparison.delta_minor === null ? '数据不完整，无法给出完整变化金额或比例。' : `净消费变化 ${money(result.comparison.delta_minor)}${result.comparison.percent_change === null ? '（基准非正数，比例不适用）' : `（${result.comparison.percent_change}%）`}`}</p></div>}
    </div>
    {result.months && <div className="analytics-months"><h4>逐月消费、退款与类别净消费</h4><div className="analytics-table-wrap"><table><thead><tr><th>月份</th><th>消费</th><th>退款</th><th>净消费</th>{current.categories.map(c => <th key={c.category}>{c.category}净消费</th>)}<th>覆盖</th></tr></thead><tbody>{result.months.map(m => <tr key={m.month}><th>{m.month}</th><td>{m.totals ? money(m.totals.purchase_minor) : '未知'}</td><td>{m.totals ? money(m.totals.refund_minor) : '未知'}</td><td>{m.totals ? money(m.totals.net_minor) : `未知（已知 ${money(m.observed_totals.net_minor)}）`}</td>{current.categories.map(c => <td key={c.category}>{m.totals ? money(m.categories.find(row => row.category === c.category)?.net_minor ?? 0) : '未知'}</td>)}<td>{m.totals ? '完整' : '缺数据'}</td></tr>)}</tbody></table></div></div>}
    <details className="analytics-evidence" open={result.report_kind !== 'annual'}><summary>本次统计的逐笔依据（共 {evidence.total} 条）</summary>
      <div className="analytics-table-wrap"><table><thead><tr><th>日期（北京时间）与交易编号</th><th>商户 / 类别</th><th>类型 / 关联原消费</th><th>净消费贡献</th></tr></thead><tbody>{evidence.items.map(row => <tr key={row.transaction_id}><td>{stamp(row.posted_at_utc)}<small>{row.transaction_id}</small></td><td>{row.merchant ?? '商户未提供'}<small>{row.category}</small></td><td>{row.type === 'refund' ? '退款' : '消费'}{row.original_transaction_id && <small>{row.original_transaction_id}</small>}</td><td>{money(row.signed_minor)}</td></tr>)}</tbody></table></div>
      {!evidence.items.length && <p>本范围暂无已知消费或退款明细。</p>}{error && <p role="alert">{error}</p>}
      {evidence.total > evidence.limit && <div className="pager"><button disabled={busy || evidence.offset === 0} onClick={() => void load(Math.max(0, evidence.offset - evidence.limit))}>上一页依据</button><span>{Math.floor(evidence.offset / evidence.limit) + 1} / {Math.ceil(evidence.total / evidence.limit)}</span><button disabled={busy || evidence.offset + evidence.limit >= evidence.total} onClick={() => void load(evidence.offset + evidence.limit)}>下一页依据</button></div>}
    </details>
  </article>;
}

export default function AnalyticsReport({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object') return null;
  if ('current' in value) return <SpendingReport result={value as Spending} />;
  if (!('report_kind' in value) || value.report_kind !== 'anomalies') return null;
  const result = value as Anomalies;
  return <article className="analytics-report analytics-anomalies" aria-label="异常交易核实报告">
    <h3>异常交易核实报告</h3><p>{range(result.period)}（北京时间） · {result.scanned_count} 笔已入账消费 · {result.candidate_count} 笔待核实 · {result.unflagged_count} 笔未标记</p>
    <p>规则一：至少5笔样本，消费金额达到1000元且至少本范围中位数的10倍；中位数 {money(result.rules.large_amount.median_minor)}，阈值 {money(result.rules.large_amount.threshold_minor)}。{!result.rules.large_amount.enabled && '样本不足，本次大额规则未启用。'}</p>
    <p>规则二：同账户、同商户、同金额，间隔不超过 {result.rules.near_duplicate.window_seconds} 秒。深夜消费本身不构成盗刷证据。</p>
    {result.coverage.status !== 'complete' && <p className="coverage-warning">本范围数据不完整，筛查仅针对已知流水。</p>}
    {result.candidates.map(row => <div className="analytics-anomaly" key={row.transaction_id}><h4>{row.merchant ?? '商户未提供'} · {money(row.amount_minor)}</h4><p>{stamp(row.posted_at_utc)}（北京时间）</p><small>交易编号：{row.transaction_id}</small>{row.reasons.map(item => <p key={item.rule}>{item.reason}{!!item.related_transaction_ids.length && <small>关联交易：{item.related_transaction_ids.join('、')}</small>}</p>)}</div>)}
    {!result.candidates.length && <p>当前规则未发现待核实线索。</p>}<p className="coverage-warning">{result.conclusion}</p>
  </article>;
}
