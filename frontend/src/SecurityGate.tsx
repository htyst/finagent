import { useEffect, useRef, useState } from 'react';
import { api, setSecurityGate } from './api';
import type { SecurityOperation } from './api';

const labels: Record<string, string> = {
  transfer: '转账', schedule: '预约转账', buy: '理财申购', redeem: '理财赎回', loss: '卡片挂失',
  freeze: '临时冻结', unfreeze: '卡片解冻', apply: '虚拟卡申请', limit_increase: '提高交易限额',
  limit_decrease: '降低交易限额', online_block: '禁止线上交易', online_unblock: '恢复线上交易',
  reminder: '续费提醒', revoke: '撤销代扣', create: '创建生日安排', change: '修改生日安排',
  cancel: '取消安排', cancel_item: '取消商品', retry: '重试未付商品', collection: 'AA收款',
  source_account_id: '付款账户编号', destination_account_id: '收款账户编号', account_id: '账户编号',
  account_alias: '账户', account_mask: '账户尾号', source: '付款账户', target: '收款对象',
  account_last4: '账户尾号', card_id: '卡片编号', card_alias: '卡片', card_mask: '卡片尾号',
  amount_minor: '金额', total_minor: '总金额', budget_minor: '预算', limit_minor: '单笔交易限额',
  fee_minor: '费用', currency: '币种', action: '操作', kind: '办理类型', type: '对象类型',
  own_account: '本人账户', saved_recipient: '已保存收款人', external_account: '外部账户',
  recipient_id: '收款人编号', name: '姓名', alias: '名称', note: '备注', title: '标题',
  bank_name: '银行', account_number: '账户号码', product: '产品', product_id: '产品编号',
  product_name: '产品名称', units_hundredths: '份额（百分之一份）', merchant: '商户',
  period: '周期', monthly: '每月', days_before: '提前天数', hour: '时', minute: '分',
  timezone: '时区', execute_at_utc: '执行时间', birthday: '生日', birthday_date: '生日日期',
  address: '地址', delivery_address: '配送地址', recipient_name: '收件人', recipient_phone: '联系电话',
  applicant: '申请人', controls: '当前卡片限制', online_enabled: '允许线上交易', lost: '已挂失',
  condition: '执行条件', items: '商品', supplier: '供应商', price_minor: '价格', quantity: '数量',
  target_binding: '预约收款对象', members: '分摊成员', share_minor: '分摊金额', mask: '尾号',
  impact: '执行影响', phone_mask: '电话尾号', card: '卡片', target_type: '收款类型',
};
function Terms({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span>无</span>;
  if (Array.isArray(value)) return <ol>{value.map((item, index) => <li key={index}><Terms value={item} /></li>)}</ol>;
  if (typeof value === 'object') return <dl>{Object.entries(value).filter(([key]) => !/digest|version|hash/.test(key)).map(([key, item]) => <div key={key}>
    <dt>{labels[key] || key}</dt><dd>{key.endsWith('_minor') && typeof item === 'number' ? `¥${(item / 100).toFixed(2)}` : <Terms value={item} />}</dd>
  </div>)}</dl>;
  return <span>{typeof value === 'boolean' ? (value ? '是' : '否') : labels[String(value)] || String(value)}</span>;
}

function keyTerms(operation: SecurityOperation): Record<string, unknown> {
  const summary = operation.terms['核对摘要'];
  if (summary && typeof summary === 'object' && !Array.isArray(summary)) {
    const result: Record<string, unknown> = {};
    for (const key of ['target', 'source', 'card']) {
      const value = (summary as Record<string, unknown>)[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        const parts = value as Record<string, unknown>;
        result[key] = ['alias','bank_name','mask','phone_mask'].map(field => parts[field]).filter(Boolean).join(' · ');
      }
    }
    return result;
  }
  return Object.fromEntries(Object.entries(operation.terms).filter(([key]) => ['target_binding','card_mask','account_alias','account_mask','product_name','execute_at_utc','recipient','recipient_name','merchant'].includes(key)));
}

export default function SecurityGate() {
  const [operation, setOperation] = useState<SecurityOperation | null>(null);
  const [challenge, setChallenge] = useState(''), [code, setCode] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef<{ resolve: () => void; reject: (error: Error) => void } | null>(null);
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!operation) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden'; dialog.current?.focus();
    return () => { document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, [operation]);
  function close(accepted: boolean) {
    const current = pending.current; pending.current = null;
    setOperation(null); setCode(''); setChallenge(''); setError('');
    if (accepted) current?.resolve(); else current?.reject(Error('已取消安全确认，未执行操作'));
  }
  useEffect(() => {
    setSecurityGate(details => new Promise<void>((resolve, reject) => {
      if (pending.current) { reject(Error('已有操作正在安全确认')); return; }
      pending.current = { resolve, reject }; setOperation(details); setCode(''); setChallenge(''); setError('');
    }));
    const cancelled = () => close(false);
    window.addEventListener('finagent:session-lost', cancelled);
    return () => { setSecurityGate(null); window.removeEventListener('finagent:session-lost', cancelled); pending.current?.reject(Error('页面已关闭')); };
  }, []);
  async function post<T>(suffix: string, payload?: unknown) {
    const { csrf_token } = await api<{ csrf_token: string }>('/api/demo/csrf');
    return api<T>(`/api/security/operations/${operation!.resource_kind}/${operation!.resource_id}/${suffix}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-FinAgent-CSRF': csrf_token },
      body: payload ? JSON.stringify(payload) : undefined,
    });
  }
  async function requestCode() {
    const waiting = pending.current;
    setBusy(true); setError('');
    try { const next = await post<{ challenge_id: string }>('challenge'); if (pending.current === waiting) setChallenge(next.challenge_id); }
    catch (e) { setError(e instanceof Error ? e.message : '签发失败'); }
    finally { setBusy(false); }
  }
  async function accept() {
    const waiting = pending.current;
    setBusy(true); setError('');
    try {
      if (operation?.level === 'red') await post('verify', { challenge_id: challenge, code });
      if (pending.current === waiting) close(true);
    } catch (e) { setError(e instanceof Error ? e.message : '验证失败'); setCode(''); }
    finally { setBusy(false); }
  }
  if (!operation) return null;
  return <div className="security-overlay"><section ref={dialog} tabIndex={-1} className="security-dialog" role="dialog" aria-modal="true" aria-labelledby="security-heading" onKeyDown={event => {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); close(false); }
    if (event.key !== 'Tab') return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), summary, [href]') ?? []).filter(el => el.getClientRects().length > 0);
    const first = controls[0], last = controls.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first.focus(); }
  }}>
    <h2 id="security-heading">{operation.level === 'red' ? '红色 · 独立强验证' : '黄色 · 明确确认'}</h2>
    <p>{operation.required}。确认后将办理下列固定操作，资金、银行及认证均为本地模拟。</p>
    <p className="security-key-amount">操作：{labels[operation.action] || operation.action}；金额/预算：¥{(operation.amount_minor / 100).toFixed(2)}</p>
    {operation.including_this_minor !== null && <p>用户跨账户今日转账累计（含本笔）：¥{(operation.including_this_minor / 100).toFixed(2)}</p>}
    <div className="security-key-terms"><Terms value={keyTerms(operation)} /></div>
    <details><summary>核对对象、账户及完整固定条款</summary><Terms value={operation.terms} /></details>
    {operation.level === 'red' && <>
      <p>本地模拟短信收件箱由本机命令读取：在项目目录运行 scripts/auth-code.ps1。验证码不会进入聊天或模型。</p>
      <button disabled={busy} onClick={() => void requestCode()}>{challenge ? '重新签发验证码' : '获取模拟短信验证码'}</button>
      {challenge && <p>挑战编号：{challenge}（3分钟有效，仅绑定本单）</p>}
      <label>六位验证码<input autoComplete="one-time-code" inputMode="numeric" type="password" value={code} maxLength={6} onChange={e => setCode(e.target.value.replace(/\D/g, ''))} /></label>
    </>}
    {error && <p role="alert">{error}</p>}
    <div className="proposal-actions"><button disabled={busy || (operation.level === 'red' && (!challenge || code.length !== 6))} onClick={() => void accept()}>确认条款并{operation.level === 'red' ? '验证执行' : '执行'}</button><button className="secondary" disabled={busy} onClick={() => close(false)}>取消</button></div>
  </section></div>;
}
