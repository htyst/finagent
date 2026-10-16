type Category = { category: string; net_minor: number; purchase_minor: number; refund_minor: number };
const money = (n: number) => new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(n / 100);
export default function SpendingChart({ categories, complete }: { categories: Category[]; complete: boolean }) {
  const max = Math.max(1, ...categories.map((item) => Math.abs(item.net_minor)));
  return <figure className="spending-chart" aria-label="分类净消费图">
    <figcaption><strong>分类净消费</strong><span>{complete ? '消费减退款 · 元' : '仅展示已知小计，数据不完整'}</span></figcaption>
    {categories.length ? categories.map((item) => <div className="chart-row" key={item.category}>
      <div className="chart-label"><span>{item.category}</span><strong>{money(item.net_minor)}</strong></div>
      <div className="chart-track" aria-hidden="true"><span className={item.net_minor < 0 ? 'chart-bar negative' : 'chart-bar'} style={{ width: `${Math.abs(item.net_minor) / max * 50}%`, left: item.net_minor < 0 ? `${50 - Math.abs(item.net_minor) / max * 50}%` : '50%' }} /></div>
      <small>消费 {money(item.purchase_minor)} · 退款 {money(item.refund_minor)}</small>
    </div>) : <p>所选范围暂无已入账消费或退款。</p>}
    <p className="chart-note">中线为零；右侧为净支出，左侧为退款超过消费。数值来自当前筛选的完整聚合结果。</p>
  </figure>;
}
