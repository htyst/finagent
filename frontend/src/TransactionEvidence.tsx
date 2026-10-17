import { useEffect, useRef, useState } from 'react';
import { api } from './api';

export type TransactionPage = {
  items: Array<{ id: string; account_id: string; type: string; direction: string; amount_minor: number;
    category: string; merchant: string | null; posted_at_utc: string; status: string;
    original_transaction_id?: string | null }>;
  total: number; limit: number; offset: number;
};
export type TransactionFilters = Record<string, string | number | null>;
const labels: Record<string, string> = { wealth_buy: '模拟理财申购', wealth_redeem: '模拟理财赎回', purchase: '消费', refund: '退款', income: '收入',
  internal_transfer: '本人转账', external_transfer: '模拟外部转账', posted: '已入账', pending: '待入账', void: '已撤销' };
const money = (value: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(value / 100);

export default function TransactionEvidence(props: {initial: TransactionPage; filters: TransactionFilters}) {
  return <TransactionEvidencePage key={JSON.stringify(props)} {...props}/>;
}
function TransactionEvidencePage({ initial, filters }: { initial: TransactionPage; filters: TransactionFilters }) {
  const [page, setPage] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function load(offset: number) {
    setBusy(true); setError('');
    const params = new URLSearchParams({ offset: String(offset), limit: String(page.limit) });
    for (const name of ['start', 'end', 'category', 'account_id', 'card_id', 'merchant', 'type', 'direction', 'status', 'sort']) {
      if (filters[name] != null) params.set(name, String(filters[name]));
    }
    try {
      const result = await api<TransactionPage>(`/api/transactions?${params}`);
      if (mounted.current) setPage(result);
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : '流水加载失败，请重试');
    } finally { if (mounted.current) setBusy(false); }
  }
  return <div className="agent-transaction-evidence">
    <h3>本次查询的流水明细</h3>
    <p className="operation-note">{filters.start ?? '全部历史'} 至 {filters.end ?? '演示日期'} · {filters.category ?? '全部类别'} · {filters.account_id ? '指定账户' : '本人全部账户'} · 共 {page.total} 条</p>
    <div className="agent-transaction-list">{page.items.map(item => <div className="transaction" key={item.id}>
      <div className="tx-main"><strong>{item.merchant || '商户未提供'}</strong>
        <small>{new Date(item.posted_at_utc).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}（北京时间） · {item.category} · {labels[item.type] ?? item.type}</small>
        {item.original_transaction_id && <small>关联原消费：{item.original_transaction_id}</small>}
      </div>
      <div className="tx-right"><strong className={item.direction === 'credit' ? 'credit' : ''}>{item.direction === 'credit' ? '+' : '−'}{money(item.amount_minor)}</strong><small>{labels[item.status] ?? item.status}</small></div>
    </div>)}</div>
    {!page.items.length && <p className="empty compact">本次筛选没有流水。</p>}
    {error && <p role="alert" className="alert">{error}</p>}
    <div className="pager">
      <button type="button" disabled={busy || page.offset === 0} onClick={() => void load(Math.max(0, page.offset - page.limit))}>上一页流水</button>
      <span>{Math.floor(page.offset / page.limit) + 1} / {Math.max(1, Math.ceil(page.total / page.limit))}</span>
      <button type="button" disabled={busy || page.offset + page.limit >= page.total} onClick={() => void load(page.offset + page.limit)}>下一页流水</button>
    </div>
  </div>;
}
