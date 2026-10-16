import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import SpendingChart from './SpendingChart';

type Account = { id: string; alias: string; account_mask: string };
type Totals = { purchase_minor: number; refund_minor: number; net_minor: number; purchase_count: number; refund_count: number; is_complete: boolean };
type Coverage = { status: 'complete' | 'incomplete'; missing_account_ids: string[] };
type Period = { start_date: string; end_exclusive_date: string };
type Evidence = { transaction_id: string; type: string; amount_minor: number; signed_minor: number; category: string; merchant: string | null; posted_at_utc: string; original_transaction_id: string | null };
type PeriodSummary = { period: Period; coverage: Coverage; totals: Totals; categories: Array<Totals & { category: string }>; evidence: { total: number; limit: number; offset: number; items: Evidence[] } };
type Comparison = { mode: string; period: Period; coverage: Coverage; totals: Totals; delta_minor: number | null; percent_change: string | null; percent_change_reason: string | null };
type Spending = { account_ids: string[]; category: string | null; current: PeriodSummary; comparison: Comparison | null; rules_version: string; dataset_version: string };
type SpendingQuery = { start: string; end: string; accountId: string; category: string; mode: string; compareStart: string; compareEnd: string };

const money = (minor: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(minor / 100);
const displayEnd = (exclusive: string) => {
  const date = new Date(`${exclusive}T00:00:00+08:00`);
  date.setUTCDate(date.getUTCDate() - 1);
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
};

export default function SpendingPanel({ demoDate, accounts, revision }: { demoDate: string; accounts: Account[]; revision: number }) {
  const defaultStart = demoDate.slice(0, 7) + '-01';
  const [start, setStart] = useState(defaultStart);
  const [end, setEnd] = useState(demoDate);
  const [accountId, setAccountId] = useState('');
  const [category, setCategory] = useState('餐饮');
  const [mode, setMode] = useState('previous_corresponding');
  const [compareStart, setCompareStart] = useState('');
  const [compareEnd, setCompareEnd] = useState('');
  const [result, setResult] = useState<Spending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [appliedQuery, setAppliedQuery] = useState<SpendingQuery>();
  const requestVersion = useRef(0);
  const attemptedQuery = useRef<SpendingQuery | undefined>(undefined);

  async function load(offset = 0, options?: SpendingQuery) {
    const version = ++requestVersion.current;
    const q = options ?? { start, end, accountId, category, mode, compareStart, compareEnd };
    attemptedQuery.current = q;
    setBusy(true); setError('');
    try {
      const params = new URLSearchParams({ category: q.category, evidence_limit: '5', evidence_offset: String(offset) });
      if (q.accountId) params.set('account_id', q.accountId);
      const defaultRange = q.start === defaultStart && q.end === demoDate;
      if (!defaultRange || q.mode === 'none' || q.mode === 'custom') {
        params.set('start', q.start); params.set('end', q.end);
      }
      params.set('comparison_mode', q.mode);
      if (q.mode === 'custom') { params.set('compare_start', q.compareStart); params.set('compare_end', q.compareEnd); }
      const data = await api<Spending>(`/api/spending?${params}`);
      if (version !== requestVersion.current) return;
      setResult(data);
      setAppliedQuery(q);
    } catch (err) {
      if (version !== requestVersion.current) return;
      setResult(null);
      setError(err instanceof Error ? err.message : '统计查询失败');
    } finally { if (version === requestVersion.current) setBusy(false); }
  }

  useEffect(() => {
    if (demoDate && accounts.length) void load(0, { start: defaultStart, end: demoDate, accountId: '', category: '餐饮', mode: 'previous_corresponding', compareStart: '', compareEnd: '' });
    return () => { requestVersion.current++; };
  }, [demoDate, accounts.length]);
  useEffect(() => {
    if (revision && appliedQuery) void load(0, appliedQuery);
  }, [revision]);
  const filtersChanged = result && JSON.stringify(appliedQuery) !== JSON.stringify({ start, end, accountId, category, mode, compareStart, compareEnd });

  return <section className="section spending-section">
    <div className="section-heading"><div><p className="eyebrow">SPENDING INSIGHT</p><h2>消费统计</h2></div><span>后端按已入账流水计算</span></div>
    <form className="spending-filters" onSubmit={(e) => { e.preventDefault(); void load(); }}>
      <label>开始日期<input type="date" value={start} onChange={(e) => { setStart(e.target.value); setMode('none'); }} required /></label>
      <label>结束日期<input type="date" value={end} onChange={(e) => { setEnd(e.target.value); setMode('none'); }} required /></label>
      <label>账户<select aria-label="账户" value={accountId} onChange={(e) => setAccountId(e.target.value)}><option value="">全部本人账户</option>{accounts.map((a) => <option key={a.id} value={a.id}>{a.alias} {a.account_mask}</option>)}</select></label>
      <label>类别<select aria-label="类别" value={category} onChange={(e) => setCategory(e.target.value)}><option value="全部">全部类别</option><option value="餐饮">餐饮</option><option value="购物">购物</option><option value="交通">交通</option><option value="未分类">未分类</option></select></label>
      <label>比较<select aria-label="比较" value={mode} onChange={(e) => setMode(e.target.value)}><option value="previous_corresponding" disabled={start !== defaultStart || end !== demoDate}>上月同期</option><option value="previous_full_month" disabled={start !== defaultStart || end !== demoDate}>上月整月</option><option value="none">不比较</option><option value="custom">自定义区间</option></select></label>
      {mode === 'custom' && <><label>比较开始<input type="date" value={compareStart} onChange={(e) => setCompareStart(e.target.value)} required /></label><label>比较结束<input type="date" value={compareEnd} onChange={(e) => setCompareEnd(e.target.value)} required /></label></>}
      <button type="submit" disabled={busy}>查询</button>
    </form>
    {filtersChanged && <p className="coverage-warning">筛选条件已修改，点击查询后生效；翻页仍使用当前结果的查询条件。</p>}
    {error && <div className="alert" role="alert">{error}<button className="secondary" disabled={busy} onClick={() => void load(0, attemptedQuery.current)}>重试统计查询</button></div>}
    {busy && result && <p role="status">正在更新统计，请稍候…</p>}
    {busy && !result && <div className="empty compact">正在计算…</div>}
    {result && <>
      <div className="spending-summary">
        <div><p className="eyebrow">{result.current.coverage.status === 'complete' ? '本期净消费' : '已知小计 · 数据不完整'}</p><strong>{money(result.current.totals.net_minor)}</strong><small>{result.current.period.start_date} 至 {displayEnd(result.current.period.end_exclusive_date)} · {result.account_ids.length} 个账户 · {result.category ?? '全部类别'}</small></div>
        <div className="spending-breakdown"><span>消费 <b>{money(result.current.totals.purchase_minor)}</b> · {result.current.totals.purchase_count} 笔</span><span>退款 <b>−{money(result.current.totals.refund_minor)}</b> · {result.current.totals.refund_count} 笔</span></div>
      </div>
      {result.current.coverage.status === 'incomplete' && <p className="coverage-warning">所选区间存在数据覆盖缺口，仅显示已知记录的小计，不代表完整支出。</p>}
      {result.comparison && <div className="comparison-row"><span>对比区间 {result.comparison.period.start_date} 至 {displayEnd(result.comparison.period.end_exclusive_date)}：{result.comparison.coverage.status === 'complete' ? money(result.comparison.totals.net_minor) : `已知小计 ${money(result.comparison.totals.net_minor)}`}</span><strong>{result.comparison.delta_minor === null ? '差额不可计算' : `差额 ${money(result.comparison.delta_minor)}`}</strong><span>{result.comparison.percent_change === null ? result.comparison.percent_change_reason === 'DATA_INCOMPLETE' ? '覆盖不完整，不计算增长率' : '基准净额不大于零，不计算增长率' : `变化 ${result.comparison.percent_change}%`}</span></div>}
      <SpendingChart categories={result.current.categories} complete={result.current.coverage.status === 'complete'} />
      <div className="evidence-head"><div><strong>计算依据</strong><small>{result.current.evidence.total} 条消费/退款记录 · 口径 {result.rules_version}</small></div><span>消费计正，退款计负</span></div>
      <div className="evidence-list">{result.current.evidence.items.length ? result.current.evidence.items.map((item) => <div className="evidence-item" key={item.transaction_id}><div><strong>{item.merchant || item.category}</strong><small>{item.transaction_id} · {item.type === 'refund' ? '退款' : '消费'}{item.original_transaction_id ? ` · 原消费 ${item.original_transaction_id}` : ''}</small></div><b className={item.signed_minor < 0 ? 'refund' : ''}>{item.signed_minor < 0 ? '−' : '+'}{money(Math.abs(item.signed_minor))}</b></div>) : <div className="empty compact">所选范围没有已入账消费或退款。</div>}</div>
      <div className="pager"><button disabled={busy || result.current.evidence.offset === 0} onClick={() => void load(Math.max(0, result.current.evidence.offset - 5), appliedQuery)}>上一页</button><span>{Math.floor(result.current.evidence.offset / 5) + 1} / {Math.max(1, Math.ceil(result.current.evidence.total / 5))}</span><button disabled={busy || result.current.evidence.offset + 5 >= result.current.evidence.total} onClick={() => void load(result.current.evidence.offset + 5, appliedQuery)}>下一页</button></div>
    </>}
  </section>;
}
