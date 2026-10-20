import { WorkspaceDrafts } from './WorkspaceDrafts';
import CardManagement from './CardManagement';
import Subscriptions from './Subscriptions';
import Birthdays from './Birthdays';
import WorkflowPlans from './WorkflowPlans';
import { useEffect, useRef, useState } from 'react';
import { api, setExpectedUser } from './api';
import SpendingPanel from './SpendingPanel';
import OperationPanel from './OperationPanel';
import AgentPanel, { TaskContextPanel } from './AgentPanel';
import { useAgentWorkspace } from './useAgentWorkspace';
import Icon from './Icon';
import RecipientNotes from './RecipientNotes';
import BankingWorkflows from './BankingWorkflows';
import WealthPanel from './WealthPanel';
import FinancialProfile from './FinancialProfile';
import SecurityGate from './SecurityGate';
import SecurityStatus from './SecurityStatus';
import OperatorPanel from './OperatorPanel';
import './PageInitialize.css';

type DemoInfo = { demo_date: string; dataset_version: string; seed_version: string; mode: string;
  model: { provider: string; model: string | null; configured: boolean; configuration_error: string | null } };
type User = { id: string; display_name: string; profile: string };
type Account = { id: string; alias: string; account_mask: string; balance_minor: number; available_balance_minor?: number; reserved_minor?: number; status: string; debit_blocked?: boolean; restriction_message?: string | null };
type Card = { id: string; alias: string; brand: string; card_mask: string; account_id: string | null; status: string; card_type?: string; read_only?: boolean };
type Transaction = { id: string; account_id: string; card_id: string | null; type: string; direction: string; amount_minor: number; category: string; merchant: string | null; posted_at_utc: string; status: string };
type TransactionPage = { items: Transaction[]; total: number; limit: number; offset: number };

function money(minor: number) {
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY' }).format(minor / 100);
}

function localTime(utc: string) {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(utc));
}

const labels: Record<string, string> = {
  wealth_buy: '模拟理财申购', wealth_redeem: '模拟理财赎回', purchase: '消费', refund: '退款', income: '收入', internal_transfer: '本人转账', external_transfer: '模拟外部转账',
  posted: '已入账', pending: '待入账', void: '已撤销', active: '正常', lost: '已挂失', frozen: '已冻结', disabled: '已停用', expired: '已到期', locked: '定期锁定', closed: '已关闭',
};

export default function App() {
  const [info, setInfo] = useState<DemoInfo | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [userId, setUserId] = useState('');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [cards, setCards] = useState<Card[]>([]);
  const [tx, setTx] = useState<TransactionPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [operationRefresh, setOperationRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [initializing, setInitializing] = useState(false);
  const [initializeNotice, setInitializeNotice] = useState('');
  const [workspaceGeneration, setWorkspaceGeneration] = useState(0);
  const initializingRef = useRef(false);
  const lastSelectedUser = useRef('');
  const requestVersion = useRef(0);
  const portfolioSequence = useRef(0);
  const currentUser = useRef('');
  const [page, setPage] = useState(() => location.hash.slice(1) || 'assistant');
  const [pending, setPending] = useState(false);
  function navigate(target: string) { location.hash = target; setPage(target); }
  useEffect(() => {
    if (!initializeNotice) return;
    const timer = setTimeout(() => setInitializeNotice(''), 5000);
    return () => clearTimeout(timer);
  }, [initializeNotice]);
  useEffect(() => { const read = () => setPage(location.hash.slice(1) || 'assistant'); window.addEventListener('hashchange', read); return () => window.removeEventListener('hashchange', read); }, []);

  async function loadPrivate(version: number, nextOffset = 0) {
    const sequence = ++portfolioSequence.current;
    try {
      const [a, c, t] = await Promise.all([
        api<Account[]>('/api/accounts'), api<Card[]>('/api/cards'),
        api<TransactionPage>(`/api/transactions?limit=10&offset=${nextOffset}`),
      ]);
      if (version !== requestVersion.current || sequence !== portfolioSequence.current) return;
      setAccounts(a); setCards(c); setTx(t); setOffset(nextOffset); setError('');
    } catch (error) {
      if (version === requestVersion.current && sequence === portfolioSequence.current) throw error;
    }
  }

  useEffect(() => {
    const version = ++requestVersion.current;
    Promise.all([api<DemoInfo>('/api/demo/info'), api<User[]>('/api/demo/users')])
      .then(async ([settings, list]) => {
        if (version !== requestVersion.current) return;
        setInfo(settings); setUsers(list);
        const current = await api<{ user: User }>('/api/demo/session');
        if (version !== requestVersion.current) return;
        currentUser.current = current.user.id; setExpectedUser(current.user.id);
        setUserId(current.user.id);
        await loadPrivate(version);
      })
      .catch((err: unknown) => {
        if (version !== requestVersion.current) return;
        if (err instanceof Error && err.message.includes('选择演示用户')) return;
        setError(err instanceof Error ? err.message : '加载失败');
      });
    return () => { requestVersion.current++; };
  }, []);

  async function chooseUser(id: string) {
    if (!id) return false;
    lastSelectedUser.current = id;
    const version = ++requestVersion.current;
    setBusy(true); setError('');
    currentUser.current = ''; setExpectedUser(''); setPending(false);
    setUserId(''); setAccounts([]); setCards([]); setTx(null);
    try {
      await api('/api/demo/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_id: id }) });
      if (version !== requestVersion.current) return;
      currentUser.current = id; setExpectedUser(id);
      setWorkspaceGeneration(value => value + 1);
      setUserId(id);
      await loadPrivate(version, 0);
      return version === requestVersion.current;
    } catch (err) {
      if (version !== requestVersion.current) return;
      setError(err instanceof Error ? err.message : '切换用户失败');
      return false;
    } finally { if (version === requestVersion.current) setBusy(false); }
  }

  async function initializePage() {
    const owner = userId || lastSelectedUser.current;
    if (!owner || initializingRef.current || busy) return;
    initializingRef.current = true; setInitializing(true); setInitializeNotice('');
    navigate('assistant');
    try {
      if (await chooseUser(owner)) {
        setOffset(0);
        setInitializeNotice('页面已初始化，可以开始新的对话。账户、银行卡、收款人与已完成业务已保留。');
      }
    } finally { initializingRef.current = false; setInitializing(false); }
  }

  async function changePage(next: number) {
    const version = ++requestVersion.current;
    setBusy(true); setError('');
    try { await loadPrivate(version, next); }
    catch (err) { if (version === requestVersion.current) setError(err instanceof Error ? err.message : '流水加载失败'); }
    finally { if (version === requestVersion.current) setBusy(false); }
  }

  async function refreshFor(owner: string) {
    if (!owner || currentUser.current !== owner) return;
    const version = requestVersion.current;
    setOperationRefresh((value) => value + 1);
    try { await loadPrivate(version, 0); }
    catch (err) { if (version === requestVersion.current) setError(err instanceof Error ? err.message : '账户刷新失败'); }
  }
  useEffect(() => {
    const lost = (event: Event) => {
      requestVersion.current++;
      currentUser.current = ''; setExpectedUser(''); setPending(false);
      setUserId(''); setAccounts([]); setCards([]); setTx(null); setBusy(false);
      setError((event as CustomEvent<string>).detail || '演示会话已变化，请重新选择用户');
    };
    const resume = () => { if (!document.hidden) void refreshFor(currentUser.current); };
    window.addEventListener('finagent:session-lost', lost);
    window.addEventListener('focus', resume);
    window.addEventListener('online', resume);
    return () => {
      window.removeEventListener('finagent:session-lost', lost);
      window.removeEventListener('focus', resume);
      window.removeEventListener('online', resume);
    };
  }, []);

  const selected = users.find((u) => u.id === userId);

  const section = page.split('/')[0];
  const titles: Record<string, string> = { assistant: '智能助手', assets: '我的资产', tasks: '任务中心', security: '安全与记录', demo: '演示工具' };
  const primary = [['assistant','chat','智能助手'],['assets/accounts','wallet','我的资产'],['tasks/current','tasks','任务中心'],['security','shield','安全与记录']];
  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={e => { e.preventDefault(); document.getElementById('main-content')?.focus(); }}>跳到主要内容</a>
    <aside className="app-sidebar"><a href="#assistant" className="brand" aria-label="FinAgent 首页"><span className="brand-symbol"><i /><i /><i /></span><strong>FinAgent</strong></a>
      <nav className="primary-nav" aria-label="主导航">{primary.map(([target, icon, label]) => <button key={target} aria-current={section === target.split('/')[0] ? 'page' : undefined} onClick={() => navigate(target)}><Icon name={icon} size={22} /><span>{label}</span>{target.startsWith('tasks') && pending && <b className="nav-count">1</b>}</button>)}</nav>
      <div className="sidebar-bottom"><button className={`demo-nav ${section === 'demo' ? 'active' : ''}`} onClick={() => navigate('demo')}><Icon name="lab" size={22}/><span>演示工具</span><Icon name="chevron" size={16}/></button>
        <label className="user-control" htmlFor="user"><span className="round-icon"><Icon name="user" /></span><span className="user-select"><span>演示用户</span><select id="user" aria-label="演示用户" value={userId} disabled={busy} onChange={e => void chooseUser(e.target.value)}><option value="">请选择用户</option>{users.map(u => <option value={u.id} key={u.id}>{u.display_name} · {u.profile}</option>)}</select></span></label>
        <p className="sidebar-disclaimer">本地演示会话 · 非真实银行登录</p>
      </div>
    </aside>
    <div className="app-main"><header className="app-topbar"><h1>{titles[section] ?? '智能助手'}</h1><div className="topbar-actions"><span className="environment-label"><span className="environment-caption">模拟银行 · 合成数据</span><span className="mode-badge">{info ? info.mode.toUpperCase() : '加载中'}</span></span><button className="secondary initialize-page" disabled={busy || initializing || (!userId && !lastSelectedUser.current)} onClick={() => void initializePage()}>{initializing ? '初始化中…' : '初始化页面'}</button><button className="mobile-demo secondary" onClick={() => navigate('demo')}>演示工具</button></div></header>
      {initializeNotice && <div className="page-initialize-notice" role="status">{initializeNotice}<button className="secondary" aria-label="关闭初始化提示" onClick={() => setInitializeNotice('')}>×</button></div>}
      <main id="main-content" tabIndex={-1} className={section === 'assistant' ? 'main-assistant' : 'main-page'}>
        {error && <div className="alert" role="alert">{error}{!users.length && <button className="secondary" onClick={() => window.location.reload()}>重新加载页面</button>}{userId && <button className="secondary" onClick={() => void refreshFor(userId)}>重新加载账户</button>}</div>}
        {busy && <p className="loading-bar" role="status">正在加载账户与流水…</p>}
        {section === 'demo' && <div className="mobile-session"><label>切换演示用户<select aria-label="切换演示用户" value={userId} disabled={busy} onChange={e => void chooseUser(e.target.value)}><option value="">请选择用户</option>{users.map(u => <option key={u.id} value={u.id}>{u.display_name}</option>)}</select></label></div>}
        {!userId ? <div className="welcome-state"><span className="welcome-icon"><Icon name="chat" size={34}/></span><h2>让每一步金融操作清楚可控</h2><p>选择一名演示用户，开始使用智能助手。</p><p className="caption">每个用户的数据相互隔离；选择用户仅建立本地演示会话。</p><div className="welcome-users">{users.map(u => <button className="secondary" key={u.id} disabled={busy} onClick={() => void chooseUser(u.id)}>{u.display_name}<small>{u.profile}</small></button>)}</div></div> : <WorkspaceDrafts key={`${userId}-${workspaceGeneration}`}><SessionWorkspace page={page} navigate={navigate} info={info} accounts={accounts} cards={cards} tx={tx} offset={offset} busy={busy} changePage={changePage} revision={operationRefresh} onChanged={() => refreshFor(userId)} onPending={setPending} userName={selected?.display_name ?? ''}/></WorkspaceDrafts>} 
      </main>
    </div><SecurityGate key={`security-${userId}-${workspaceGeneration}`} />
  </div>;
}

type SessionProps = { page: string; navigate: (page: string) => void; info: DemoInfo | null; accounts: Account[]; cards: Card[]; tx: TransactionPage | null; offset: number; busy: boolean; changePage: (next: number) => Promise<void>; revision: number; onChanged: () => Promise<void>; onPending: (pending: boolean) => void; userName: string };
function SessionWorkspace({page, navigate, info, accounts, cards, tx, offset, busy, changePage, revision, onChanged, onPending, userName}: SessionProps) {
  const workspace = useAgentWorkspace(onChanged, revision);
  const [home, setHome] = useState(false), [modelBusy, setModelBusy] = useState(false), [modelNotice, setModelNotice] = useState('');
  const task = workspace.task;
  useEffect(() => { onPending(task?.status === 'awaiting_confirmation' || task?.status === 'gathering_info'); }, [task?.status, onPending]);
  const [major, minor] = page.split('/');
  const assetTabs = [['accounts','账户'],['profile','金融档案'],['bills','账单'],['cards','卡片'],['wealth','理财'],['subscriptions','订阅代扣'],['recipients','收款人']];
  const taskTabs = [['current','待我处理'],['schedules','预约与 AA'],['plans','联动计划'],['manual','手工办理']];
  const demoTabs = [['','运行状态'],['cards','卡片模拟'],['payments','成员付款'],['delivery','供应商送达'],['operator','模拟坐席']];
  const navTo = (target: string) => { if (target === 'assistant') setHome(false); navigate(target); };
  async function checkModel() {
    setModelBusy(true); setModelNotice('');
    try { const {csrf_token} = await api<{csrf_token: string}>('/api/demo/csrf'); await api('/api/model/probe', {method:'POST',headers:{'X-FinAgent-CSRF':csrf_token}}); setModelNotice('DeepSeek 连接和工具调用校验成功，可以开始对话。'); }
    catch(e) {setModelNotice(e instanceof Error ? e.message : '连接检查失败');} finally {setModelBusy(false);}
  }
  const available = accounts.length && accounts.every(a => a.available_balance_minor !== undefined) ? accounts.reduce((n,a) => n + a.available_balance_minor!,0) : undefined;
  return <><SecurityStatus />
    {major === 'assistant' && <AgentPanel workspace={workspace} accounts={accounts} mode={info?.mode ?? 'mock'} onNavigate={navTo} balance={available} home={home} onHomeChange={setHome} />}
    {major === 'assets' && <><div className="page-heading"><div><h2>我的资产</h2><p>查看账户、账单和已关联服务</p></div><button className="secondary" onClick={() => navTo('assistant')}>对话办理</button></div><div className="page-tabs" role="navigation" aria-label="资产分类">{assetTabs.map(([key,label]) => <button key={key} aria-current={(minor || 'accounts') === key ? 'page' : undefined} onClick={() => navigate('assets/'+key)}>{label}</button>)}</div>
      {(!minor || minor === 'accounts') && <>        <section className="section"><div className="section-heading"><div><p className="eyebrow">ACCOUNTS</p><h2>我的账户</h2></div><span>余额均为模拟金额</span></div><div className="account-grid">{accounts.map((a) => <article className="account-card" key={a.id}><div className="account-top"><span>{a.alias}</span><span className={a.status === 'active' ? 'tag good' : 'tag'}>{labels[a.status] ?? a.status}</span></div><strong>{money(a.balance_minor)}</strong><div className="account-foot"><span>CNY · {a.account_mask}</span><span>账面余额</span></div><dl className="account-funds"><div><dt>一般可用</dt><dd>{a.available_balance_minor === undefined ? '未提供' : money(a.available_balance_minor)}</dd></div><div><dt>已预留资金</dt><dd>{a.reserved_minor === undefined ? '未提供' : money(a.reserved_minor)}</dd></div></dl>{a.restriction_message && <p className="operation-note">{a.restriction_message}</p>}</article>)}</div></section>

        <section className="section"><div className="section-heading"><div><p className="eyebrow">CARDS</p><h2>我的卡片</h2></div><span>仅显示尾号</span></div><div className="card-grid">{cards.map((c) => <article className="bank-card" key={c.id}><div className="bank-card-top"><span>{c.brand}</span><span className={c.status === 'active' ? 'tag good' : 'tag warning'}>{labels[c.status] ?? c.status}</span></div><div className="chip" /><div className="card-bottom"><div><small>{c.alias}</small><strong>•••• {c.card_mask.slice(-4)}</strong></div><small>{c.card_type === 'credit' ? '信用卡 · 导入资料只读' : `关联 ${accounts.find((a) => a.id === c.account_id)?.alias ?? c.account_id ?? '未提供'}`}</small></div></article>)}</div></section>


                <section className="section"><div className="section-heading"><div><p className="eyebrow">ACTIVITY</p><h2>近期流水</h2></div><span>{tx?.total ?? 0} 条记录</span></div><div className="transaction-list">{tx?.items.length ? tx.items.map((t) => <div className="transaction" key={t.id}><div className="tx-icon">{t.direction === 'credit' ? '↙' : '↗'}</div><div className="tx-main"><strong>{t.merchant || labels[t.type] || t.type}</strong><small>{localTime(t.posted_at_utc)} · {t.category} · {accounts.find((a) => a.id === t.account_id)?.alias ?? (t.account_id.startsWith('credit:') ? '信用卡账单' : t.account_id)}{t.card_id && ` · 卡尾号 ${cards.find((c) => c.id === t.card_id)?.card_mask.slice(-4) ?? "未知"}`}</small></div><div className="tx-right"><strong className={t.direction === 'credit' ? 'credit' : ''}>{t.direction === 'credit' ? '+' : '−'}{money(t.amount_minor)}</strong><small>{labels[t.status] ?? t.status}</small></div></div>) : <div className="empty compact">当前范围没有流水。</div>}</div><div className="pager"><button disabled={busy || offset === 0} onClick={() => void changePage(Math.max(0, offset - 10))}>上一页</button><span>{tx ? `${Math.floor(offset / 10) + 1} / ${Math.max(1, Math.ceil(tx.total / 10))}` : '—'}</span><button disabled={busy || !tx || offset + 10 >= tx.total} onClick={() => void changePage(offset + 10)}>下一页</button></div></section>
</>}
      {minor === 'profile' && <FinancialProfile revision={revision} />}
      {minor === 'bills' && <SpendingPanel cards={cards} revision={revision} demoDate={info?.demo_date ?? ''} accounts={accounts} />}
      {minor === 'cards' && <CardManagement accounts={accounts} revision={revision} onChanged={onChanged} />}
      {minor === 'wealth' && <WealthPanel revision={revision} onChanged={onChanged} />}
      {minor === 'subscriptions' && <Subscriptions revision={revision} onChanged={onChanged} />}
      {minor === 'recipients' && <RecipientNotes />}
    </>}
    {major === 'tasks' && <><div className="page-heading"><div><h2>任务中心</h2><p>核对待处理事项，继续已保存的安排</p></div><button className="secondary" onClick={() => navTo('assistant')}>回到智能助手</button></div><div className="page-tabs" aria-label="任务分类">{taskTabs.map(([key,label]) => <button key={key} aria-current={(minor || 'current') === key ? 'page' : undefined} onClick={() => navigate('tasks/'+key)}>{label}</button>)}</div>
      {(!minor || minor === 'current') && <><TaskContextPanel workspace={workspace} onNavigate={navTo} standalone /><p className="caption">在联动计划中查看目标历史、后续复查和生活安排；已发生业务以固定单及回执为准。</p></>}
      {minor === 'schedules' && <BankingWorkflows revision={revision} onChanged={onChanged} />}
      {minor === 'plans' && <WorkflowPlans revision={revision} onNavigate={navTo} onOpenTask={async (id,view) => { await workspace.openTask(id); navTo(view==='progress'?'tasks/current':'assistant'); }} onPrompt={text => { workspace.startNewConversation(); workspace.setInput(text); navTo('assistant'); }} />}
      {['occasions','birthdays'].includes(minor) && <><button className="text-button" onClick={() => navigate('tasks/plans')}>返回联动计划</button><Birthdays revision={revision} onChanged={onChanged} /></>}
      {minor === 'manual' && <OperationPanel accounts={accounts} cards={cards.filter((c): c is Card & {account_id: string} => !c.read_only && c.status !== 'expired' && c.account_id !== null)} agentProposalId={task?.proposal?.id ?? null} revision={revision} onChanged={onChanged} />}
    </>}
    {major === 'security' && <><div className="page-heading"><div><h2>安全与记录</h2><p>每一笔操作都由服务端核验权限和固定条款</p></div></div><div className="security-principles"><article><span className="tag good">绿色 · 自动查询</span><h3>有依据地回答</h3><p>余额、流水和账单来自业务接口。模型不可编造账户或交易结果。</p></article><article><span className="tag warning">黄色 · 明确确认</span><h3>先核对，再执行</h3><p>小额转账、订阅查询与取消等按当前服务端规则办理。</p></article><article><span className="tag danger">红色 · 独立强验证</span><h3>固定单绑定验证</h3><p>大额转账、挂失和申购等需要独立模拟短信验证；不在聊天中接收验证码。</p></article></div><section className="section"><h3>当前操作记录</h3><TaskContextPanel workspace={workspace} onNavigate={navTo} standalone /></section><section className="section"><h3>恢复与求助</h3><p>未收到响应时先查询原单状态；已经发生的交易不会因暂停或取消而撤回。模拟坐席不能替你授权。后台安全锁由服务端执行。</p><button className="secondary" disabled={!task || workspace.unavailable} onClick={() => void workspace.taskAction('handoff')}>请求人工接管</button><p className="caption">本系统使用合成数据、模拟银行与本机模拟认证，不代表真实银行认证。</p></section></>}
    {major === 'demo' && <><div className="page-heading"><div><h2>演示工具</h2><p>本机模拟角色和联调工具，与普通办理分开</p></div></div><div className="page-tabs" aria-label="演示分类">{demoTabs.map(([key,label]) => <button key={key} aria-current={(minor ?? '') === key ? 'page' : undefined} onClick={() => navigate('demo'+(key ? '/'+key : ''))}>{label}</button>)}</div>
      {!minor && <section className="section"><h3>运行状态</h3><dl className="diagnostics"><div><dt>当前用户</dt><dd>{userName} · 本地演示会话</dd></div><div><dt>模式</dt><dd>{info?.mode.toUpperCase()}</dd></div><div><dt>演示日期</dt><dd>{info?.demo_date}</dd></div><div><dt>数据版本</dt><dd>{info?.dataset_version} / {info?.seed_version}</dd></div><div><dt>模型</dt><dd>{info?.model?.provider} · {info?.model?.model ?? '未配置'} · {info?.model?.configured ? '本地配置有效' : info?.model?.configuration_error}</dd></div></dl>{info?.mode === 'live' && <><p>连接检查会发送一次真实模型请求并产生用量。</p><button disabled={modelBusy} onClick={() => void checkModel()}>{modelBusy ? '正在检查…' : '检查 DeepSeek 连接'}</button></>}{modelNotice && <p role="status">{modelNotice}</p>}<p className="operation-note">模型不可用时不自动切换 Mock；必要状态查询、任务控制及原回执恢复仍可使用。模式由启动器决定。</p><p className="caption">业务测试时间沿用现有命令行工具；本轮未新增时钟写接口。</p></section>}
      {minor === 'cards' && <CardManagement demo accounts={accounts} revision={revision} onChanged={onChanged} />}
      {minor === 'payments' && <BankingWorkflows demo revision={revision} onChanged={onChanged} />}
      {minor === 'delivery' && <Birthdays demo revision={revision} onChanged={onChanged} />}
      {minor === 'operator' && <OperatorPanel />}
    </>}
  </>;
}
