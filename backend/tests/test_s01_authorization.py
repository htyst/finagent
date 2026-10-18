"""S01 independent security contracts, real writer races and injected failures."""
import json,secrets,threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from sqlalchemy import select,func
import pytest
from app.agent import authorization,dynamic_runtime as runtime,dynamic_protocol as protocol,privacy
from app.agent.service import create,send,view,cancel
from app.agent.control import change
from app.data.models import Account,AgentTask,AgentMessage,AuditEvent,DemoSession,DemoSetting,Proposal,TransferOrder,Transaction,Card
from app.data.security_models import SecurityChallenge,SecurityState,SecurityAuthorization
from app.data.scheduled_models import ScheduledTransfer
from app.domain import operations,security,subscriptions,card_management,wealth,scheduled_transfers
from app.domain.errors import DomainError
from test_dynamic_tools import dynamic,sequence,calls,reply,session
from test_security import transfer,mfa
from test_chat_authorization import prepare as chat_prepare
from test_background import schedule


def number(db):return db.scalar(select(func.count()).select_from(TransferOrder))
def token(db,pid,sid='s1',now=None):return operations.issue_confirmation_token(db,session_id=sid,proposal_id=pid,now=now)['confirmation_token']
def commit_one(db,pid,tok,sid='s1',now=None,**kw):return operations.confirm_proposal(db,session_id=sid,proposal_id=pid,confirmation_token=tok,now=now,**kw)


@pytest.mark.parametrize('words',['确认','同意','我同意，你直接执行'])
def test_chat_agreement_is_not_fixed_form_authorization(dynamic,monkeypatch,words):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='600'))),reply('请在固定操作单中核对并确认，聊天同意没有付款。'))
    first=create(db,session(db),'live','从活期账户转600元到储蓄账户')
    result=send(db,session(db),first['id'],words)
    assert number(db)==0 and result['proposal']['status']=='pending_confirmation'
    assert db.get(Proposal,first['proposal']['id']).token_hash is None


@pytest.mark.parametrize('style',['copula','colon_copula','spaced','otp'])
def test_authentication_text_never_reaches_model_or_regular_store(dynamic,monkeypatch,style):
    db,_=dynamic;seen=sequence(monkeypatch,reply('你好。'),reply('请使用独立安全通道。'))
    first=create(db,session(db),'live','你好')
    secret=''.join(str(secrets.randbelow(10)) for _ in range(6))
    text={'copula':'验证码是'+secret,'colon_copula':'短信验证码为：'+secret,'spaced':'验证码 '+secret[:3]+' '+secret[3:],'otp':'OTP is '+secret}[style]
    send(db,session(db),first['id'],text)
    messages=[r.content for r in db.scalars(select(AgentMessage))]
    ordinary=json.dumps(messages+[r.context_json for r in db.scalars(select(AgentTask))]+[r.detail_code for r in db.scalars(select(AuditEvent))],ensure_ascii=False)
    leak=len(seen)!=1 or secret in ordinary or (style=='spaced' and text in ordinary)
    if leak:pytest.fail('Authentication input was admitted to the model or ordinary persistence',pytrace=False)
    scrubbed=json.dumps(privacy.model_payload(dict(messages=[text],tool_result=dict(code=secret))),ensure_ascii=False)
    if secret in scrubbed or (style=='spaced' and text in scrubbed):pytest.fail('Historical/tool payload auth redaction is incomplete',pytrace=False)


def test_subscription_query_grant_does_not_authorize_cancellation(bank):
    db,_=bank;subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
    with pytest.raises(DomainError) as e:subscriptions.identify(db,'s1')
    assert e.value.code=='SUBSCRIPTION_CONFIRMATION_REQUIRED'
    security.confirm_subscription_access(db,'s1');subscriptions.identify(db,'s1')
    auth=db.scalar(select(__import__('app.data.subscription_models',fromlist=['SubscriptionAuthorization']).SubscriptionAuthorization))
    request=subscriptions.prepare(db,session_id='s1',kind='revoke',authorization_id=auth.id)
    with pytest.raises(DomainError):subscriptions.confirm(db,session_id='s1',request_id=request['id'],confirmation_token='not-a-fixed-form-token')
    db.expire_all();assert auth.status=='active'
    assert all(g.resource.startswith('subscription-query:') for g in db.scalars(select(SecurityAuthorization)))
    with pytest.raises(DomainError):security.assert_subscription_access(db,'s2')


@pytest.mark.parametrize('field',['amount','account','target','terms'])
def test_modification_revokes_old_form_token_and_proof(bank,field):
    db,_=bank;old=transfer(db,200000);old_row=db.get(Proposal,old);old_task=old_row.task_id
    old_token=token(db,old);mfa(db,old)
    args=dict(source_account_id='a1c',target=dict(type='own_account',account_id='a1s'),amount_minor=200000)
    if field=='amount':args['amount_minor']=190000
    elif field=='account':args.update(source_account_id='a1s',target=dict(type='own_account',account_id='a1c'))
    elif field=='target':args['target']=dict(type='saved_recipient',recipient_id='r1')
    else:args['note']='新的固定备注'
    new=operations.prepare_transfer(db,session_id='s1',task_id=old_task,**args)['proposal']['id']
    assert old!=new and db.get(Proposal,old).status=='invalidated'
    with pytest.raises(DomainError):commit_one(db,old,old_token)
    new_token=token(db,new)
    with pytest.raises(DomainError) as denied:commit_one(db,new,new_token)
    assert denied.value.code=='MFA_REQUIRED' and number(db)==0


def drafts(db):
    first=transfer(db,60000);second=transfer(db,50000,sid='s2',source='a1s')
    tokens=[token(db,first),token(db,second,'s2')]
    assert security.describe(db,'s1','proposal',first)['level']=='yellow'
    assert security.describe(db,'s2','proposal',second)['level']=='yellow'
    return [(first,tokens[0],'s1'),(second,tokens[1],'s2')]


def test_preissued_yellow_tokens_recheck_after_first_commit(bank):
    db,_=bank;first,second=drafts(db)
    commit_one(db,first[0],first[1],first[2])
    with pytest.raises(DomainError) as e:commit_one(db,second[0],second[1],second[2])
    assert e.value.code=='MFA_REQUIRED' and number(db)==1 and security.daily_total(db,'u1')==60000


@pytest.mark.parametrize('round_id',range(5))
def test_preissued_600_500_tokens_race_under_writer_lock(bank,round_id):
    db,factory=bank;items=drafts(db);db.commit();barrier=threading.Barrier(2)
    def run(item):
        with factory() as connection:
            barrier.wait(timeout=10)
            try:return commit_one(connection,item[0],item[1],item[2])['status']
            except DomainError as e:return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:states=list(pool.map(run,items))
    assert sorted(states)==['MFA_REQUIRED','executed']
    db.expire_all();assert number(db)==1 and security.daily_total(db,'u1') in {50000,60000}


@pytest.mark.parametrize('round_id',range(3))
def test_concurrent_same_fixed_form_returns_identical_receipt(bank,round_id):
    db,factory=bank;pid=transfer(db,60000);tok=token(db,pid);db.commit();barrier=threading.Barrier(2)
    def run(_):
        with factory() as c:
            barrier.wait(timeout=10);return commit_one(c,pid,tok)
    with ThreadPoolExecutor(max_workers=2) as pool:receipts=list(pool.map(run,range(2)))
    assert receipts[0]==receipts[1]
    db.expire_all();assert number(db)==1


def test_beijing_midnight_and_inclusive_threshold(bank,monkeypatch):
    db,_=bank;instant=[datetime(2026,10,17,15,59,59,tzinfo=timezone.utc)]
    monkeypatch.setattr(security,'now',lambda:instant[0])
    pid=operations.prepare_transfer(db,session_id='s1',source_account_id='a1c',target=dict(type='own_account',account_id='a1s'),amount_minor=60000,now=instant[0])['proposal']['id'];commit_one(db,pid,token(db,pid,now=instant[0]),now=instant[0])
    assert security.policy(db,'u1','transfer',40000)['level']=='yellow'
    assert security.policy(db,'u1','transfer',40001)['level']=='red'
    instant[0]+=timedelta(seconds=1)
    assert security.daily_total(db,'u1')==0
    assert security.policy(db,'u1','transfer',100000)['level']=='yellow'


@pytest.mark.parametrize('kind',['loss','unloss','buy'])
def test_fixed_red_actions_cannot_use_yellow_confirmation(bank,kind):
    db,_=bank
    if kind=='buy':
        wealth.submit_assessment(db,session_id='s1',answers=[2,2,2,2])
        row=wealth.prepare_order(db,session_id='s1',kind='buy',product_id='P1',account_id='a1c',amount_minor=10000)
        resource='wealth';issue=wealth.issue_token;execute=wealth.confirm_order;field='order_id'
    else:
        if kind=='unloss':
            from test_card_unloss import lost_card
            card,_=lost_card(db)
        else:card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
        row=card_management.prepare(db,session_id='s1',kind=kind,card_id=card.id)
        resource='card';issue=card_management.issue_token;execute=card_management.confirm;field='request_id'
    assert security.describe(db,'s1',resource,row['id'])['level']=='red'
    t=issue(db,session_id='s1',**{field:row['id']})['confirmation_token']
    with pytest.raises(DomainError) as e:execute(db,session_id='s1',confirmation_token=t,**{field:row['id']})
    assert e.value.code=='MFA_REQUIRED'


@pytest.mark.parametrize('action',['pause','cancel'])
def test_actual_late_model_response_after_control_cannot_prepare(dynamic,monkeypatch,action):
    db,factory=dynamic;entered=threading.Event();release=threading.Event()
    def late(*_):
        entered.set()
        if not release.wait(10):raise AssertionError('Test model wait exceeded')
        return calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='600')))
    monkeypatch.setattr(protocol,'step',late);db.commit()
    def run():
        with factory() as c:return create(c,c.get(DemoSession,'s1'),'live','从活期账户转600元到储蓄账户')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run)
        try:
            assert entered.wait(10)
            with factory() as c:
                task=c.scalar(select(AgentTask).order_by(AgentTask.created_at_utc.desc()))
                if action=='pause':change(c,task,'pause')
                else:cancel(c,c.get(DemoSession,'s1'),task.id)
        finally:release.set()
        result=future.result(timeout=15)
    db.expire_all()
    assert number(db)==0 and db.scalar(select(func.count()).select_from(Proposal))==0
    assert result['status'] in {'paused','cancelled'}


@pytest.mark.parametrize('change_kind',['owner_session','same_owner_session','session_expiry','dataset','account_version','token_expiry'])
def test_bound_authorization_cannot_cross_changed_security_context(bank,change_kind):
    db,_=bank;pid=transfer(db,60000);t=token(db,pid);sid='s1'
    if change_kind=='owner_session':sid='other'
    elif change_kind=='same_owner_session':sid='s2'
    elif change_kind=='session_expiry':db.get(DemoSession,'s1').expires_at_utc=(security.now()-timedelta(seconds=1)).isoformat()
    elif change_kind=='dataset':db.get(DemoSetting,1).dataset_version='S01-TEST-NEW-VERSION'
    elif change_kind=='account_version':db.get(Account,'a1c').version+=1
    else:db.get(Proposal,pid).expires_at_utc=(security.now()-timedelta(seconds=1)).isoformat()
    db.commit()
    with pytest.raises(DomainError):commit_one(db,pid,t,sid)
    assert number(db)==0


@pytest.mark.parametrize('cause',['balance','grant_expiry','risk','target_version','dataset','locked'])
def test_background_revalidates_instead_of_reusing_frontend_authority(bank,cause):
    db,_=bank;sid,due=schedule(db,60000)
    if cause=='balance':
        from app.domain.ledger import post_entry
        available=db.get(Account,'a1c').balance_minor
        post_entry(db,owner_id='u1',account_id='a1c',kind='purchase',direction='debit',amount_minor=available-10000,business_id='S01-test-spend',business_at=security.now())
    elif cause=='grant_expiry':db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource=='schedule:'+sid)).expires_at=(security.now()-timedelta(seconds=1)).isoformat()
    elif cause=='risk':
        pid=transfer(db,50000,sid='s2',source='a1s');commit_one(db,pid,token(db,pid,'s2'),'s2')
    elif cause=='target_version':
        db.get(Account,'a1s').version+=1;db.get(Account,'a1s').alias='S01-changed-target-terms'
    elif cause=='dataset':db.get(DemoSetting,1).dataset_version='S01-TEST-CHANGED'
    else:
        for _ in range(5):security.failure(db,'u1','MFA_INVALID')
    db.commit();before=number(db)
    result=scheduled_transfers._execute_due(db,sid,due)
    expected={'balance':'INSUFFICIENT_FUNDS','grant_expiry':'REAUTHORIZATION_REQUIRED','risk':'REAUTHORIZATION_REQUIRED','target_version':'STALE_RESOURCE','dataset':'SCHEDULE_DATASET_CHANGED','locked':'SECURITY_LOCKED'}[cause]
    assert result['status']=='failed' and result['error_code']==expected
    assert number(db)==before


@pytest.mark.parametrize('round_id',range(3))
def test_background_workers_race_only_one_payment(bank,round_id):
    db,factory=bank;sid,due=schedule(db,60000);db.commit();barrier=threading.Barrier(2)
    def run(_):
        with factory() as c:
            barrier.wait(timeout=10);return scheduled_transfers._execute_due(c,sid,due)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
    db.expire_all();assert number(db)==1 and results[0]['receipt']==results[1]['receipt']


def test_response_loss_after_commit_and_fresh_session_returns_original_receipt(bank):
    db,factory=bank;pid=transfer(db,60000);t=token(db,pid)
    original=commit_one(db,pid,t);db.close()
    with factory() as restarted:
        replay=commit_one(restarted,pid,t)
        assert original==replay and number(restarted)==1


def test_business_commit_then_ui_ack_failure_recovers_without_second_write(bank,monkeypatch):
    db,factory=bank;row,action=chat_prepare(db);tid=row.id;pid=action['resource_id'];aid=action['action_id']
    tok=authorization.issue_token(db,session(db),tid,'proposal',pid,aid)['confirmation_token']
    original_say=__import__('app.agent.service',fromlist=['_say'])._say
    def fail_ack(*_):raise RuntimeError('S01 simulated response persistence failure')
    with monkeypatch.context() as patch:
        patch.setattr('app.agent.service._say',fail_ack)
        with pytest.raises(RuntimeError):authorization.confirm(db,session(db),tid,'proposal',pid,aid,tok)
    db.expire_all();assert number(db)==1 and db.get(Proposal,pid).status=='executed'
    with factory() as restarted:
        recovered=authorization.confirm(restarted,restarted.get(DemoSession,'s1'),tid,'proposal',pid,aid,tok)
        assert recovered['proposal']['receipt'] and number(restarted)==1


def test_persistent_lock_does_not_replay_on_cooldown(bank,monkeypatch):
    db,factory=bank;pid=transfer(db,200000);t=token(db,pid)
    challenge=security.start_challenge(db,'s1','proposal',pid)
    for _ in range(5):
        with pytest.raises(DomainError):security.verify_challenge(db,'s1','proposal',pid,challenge['challenge_id'],'invalid')
    db.expire_all();until=datetime.fromisoformat(db.get(SecurityState,'u1').locked_until)
    with factory() as restarted:
        with pytest.raises(DomainError) as locked:commit_one(restarted,pid,t)
        assert locked.value.code=='SECURITY_LOCKED' and number(restarted)==0
        monkeypatch.setattr(security,'now',lambda:until+timedelta(seconds=1))
        security.assert_unlocked(restarted,'u1')
        assert number(restarted)==0 and restarted.get(Proposal,pid).status=='pending_confirmation'
        with pytest.raises(DomainError) as fresh_required:commit_one(restarted,pid,t)
        assert fresh_required.value.code in {'MFA_REQUIRED','INVALID_ORDER_STATE','PROPOSAL_EXPIRED'}
        assert number(restarted)==0

def test_plan_version_change_revokes_action_and_independent_proof(bank):
    db,_=bank;row,a=chat_prepare(db,amount=200000);t=authorization.issue_token(db,session(db),row.id,'proposal',a['resource_id'],a['action_id'])['confirmation_token']
    mfa(db,a['resource_id']);row.plan_version+=1;db.commit()
    with pytest.raises(DomainError) as e:authorization.confirm(db,session(db),row.id,'proposal',a['resource_id'],a['action_id'],t)
    assert e.value.code=='STALE_RESOURCE'
    with pytest.raises(DomainError) as e:commit_one(db,a['resource_id'],t)
    assert e.value.code=='MFA_REQUIRED' and number(db)==0


def test_valid_schedule_rechecks_balance_without_revoking_on_routine_balance_version(bank):
    db,_=bank;sid,due=schedule(db,60000)
    from app.domain.ledger import post_entry
    post_entry(db,owner_id='u1',account_id='a1c',kind='purchase',direction='debit',amount_minor=1000,business_id='S01-routine-spend',business_at=security.now());db.commit()
    result=scheduled_transfers._execute_due(db,sid,due)
    assert result['status']=='executed' and number(db)==1


def test_late_duplicate_authorization_queue_cannot_restore_completed_wait(dynamic,monkeypatch):
    from contextlib import contextmanager
    db,factory=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))),reply('业务回执已保留。'))
    result=create(db,session(db),'live','从活期账户转100元到储蓄账户');tid=result['id'];a=result['authorization_operations'][0]
    t=authorization.issue_token(db,session(db),tid,'proposal',a['resource_id'],a['action_id'])['confirmation_token']
    ack=authorization.confirm(db,session(db),tid,'proposal',a['resource_id'],a['action_id'],t);db.commit()
    entered=threading.Event();release=threading.Event();worker={};jobs=[];atomic=runtime.atomic_write
    class Background:
        def add_task(self,*args,**kwargs):jobs.append(True)
    @contextmanager
    def delayed(connection):
        if threading.get_ident()==worker.get('id'):
            entered.set()
            if not release.wait(10):raise AssertionError('queue wait exceeded')
        with atomic(connection):yield
    monkeypatch.setattr(runtime,'atomic_write',delayed)
    def enqueue():
        worker['id']=threading.get_ident()
        with factory() as c:return runtime.queue_authorization(Background(),c,c.get(DemoSession,'s1'),tid,'proposal',a['resource_id'],a['action_id'],ack)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(enqueue)
        try:
            assert entered.wait(10)
            with factory() as c:
                done=runtime.after_authorization(c,c.get(DemoSession,'s1'),tid,'proposal',a['resource_id'],a['action_id'],ack)
                assert done['status']=='completed'
        finally:release.set()
        future.result(timeout=15)
    db.expire_all();persisted=runtime.get(db.get(AgentTask,tid))
    assert persisted['wait'] is None and len(persisted['completed_operations'])==1
    assert persisted['status']=='completed' and number(db)==1 and not jobs


@pytest.mark.parametrize('secret_key',['code','confirmation_token','csrf_token','operator_token','approver_token','MODEL_API_KEY'])
def test_nested_model_payload_cannot_include_authentication_fields(secret_key):
    secret='S01-NOT-A-REAL-SECRET'
    result=privacy.model_payload(dict(tool=dict(result=[{secret_key:secret}]),ordinary='应保留的业务说明'))
    assert result['ordinary']=='应保留的业务说明'
    if secret in json.dumps(result):pytest.fail('Nested auth field not redacted',pytrace=False)


def test_ordinary_six_digit_business_amount_is_not_auth_input(dynamic,monkeypatch):
    db,_=dynamic;seen=sequence(monkeypatch,reply('请核实相关费用和资金来源。'))
    create(db,session(db),'live','预算100000元，只分析，不办理')
    assert len(seen)==1


def test_authorization_queue_cannot_undo_a_pause(dynamic,monkeypatch):
    db,_=dynamic
    sequence(monkeypatch,calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))))
    first=create(db,session(db),'live','从活期账户转100元到储蓄账户');tid=first['id'];a=first['authorization_operations'][0]
    t=authorization.issue_token(db,session(db),tid,'proposal',a['resource_id'],a['action_id'])['confirmation_token']
    ack=authorization.confirm(db,session(db),tid,'proposal',a['resource_id'],a['action_id'],t)
    change(db,db.get(AgentTask,tid),'pause');jobs=[]
    class Background:
        def add_task(self,*a,**kw):jobs.append(True)
    result=runtime.queue_authorization(Background(),db,session(db),tid,'proposal',a['resource_id'],a['action_id'],ack)
    assert not jobs and result['status']=='paused' and number(db)==1


def test_real_process_exit_after_commit_then_repeated_request_writes_once(bank,tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    db,_=bank;pid=transfer(db,60000);t=token(db,pid);initial=db.get(Account,'a1c').balance_minor;db.commit()
    script=tmp_path/'s01-process-recovery.py'
    script.write_text('''import json,os,sys
from app.data.db import SessionLocal
from app.domain.operations import confirm_proposal
payload=json.load(sys.stdin)
with SessionLocal() as db:
 result=confirm_proposal(db,session_id='s1',proposal_id=payload['proposal_id'],confirmation_token=payload['confirmation_token'])
 if payload.get('lose_response'):os._exit(73)
 print(json.dumps(dict(status=result['status'],order_id=result['order_id'])))
''',encoding='utf-8')
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]),FINAGENT_DB_PATH=db.get_bind().url.database,
        MODEL_API_KEY='',DEEPSEEK_API_KEY='',FINAGENT_MODE='mock',PYTHONDONTWRITEBYTECODE='1',FINAGENT_SCHEDULER_ENABLED='0',FINAGENT_SUBSCRIPTIONS_ENABLED='0',FINAGENT_BIRTHDAYS_ENABLED='0')
    def process(lose=False):
        return subprocess.run([sys.executable,'-B',str(script)],input=json.dumps(dict(proposal_id=pid,confirmation_token=t,lose_response=lose)),text=True,capture_output=True,timeout=25,env=env)
    lost=process(True)
    if lost.returncode!=73:pytest.fail('Isolated process did not reach committed response-loss boundary',pytrace=False)
    results=[process(),process()]
    if any(r.returncode for r in results):pytest.fail('Isolated recovery process failed',pytrace=False)
    receipts=[json.loads(r.stdout) for r in results]
    assert receipts[0]==receipts[1] and receipts[0]['status']=='executed'
    db.expire_all();assert number(db)==1 and db.get(Account,'a1c').balance_minor==initial-60000


def test_precommit_fault_rolls_back_money_receipt_and_proof_together(bank):
    db,_=bank;pid=transfer(db,200000);t=token(db,pid);issued,_=mfa(db,pid);before=db.get(Account,'a1c').balance_minor
    def crash():raise RuntimeError('S01 injected precommit fault')
    with pytest.raises(RuntimeError):commit_one(db,pid,t,before_commit=crash)
    db.expire_all()
    assert number(db)==0 and db.get(Account,'a1c').balance_minor==before
    assert db.get(SecurityChallenge,issued['challenge_id']).status=='verified'
    first=commit_one(db,pid,t);second=commit_one(db,pid,t)
    assert first==second and number(db)==1


def test_background_failure_after_posting_rolls_back_before_retry(bank,monkeypatch):
    db,_=bank;sid,due=schedule(db,60000);audit=scheduled_transfers._audit
    def fail_final(db,owner,pid,event,instant,*args,**kwargs):
        if event=='schedule_executed':raise RuntimeError('S01 injected worker precommit fault')
        return audit(db,owner,pid,event,instant,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(scheduled_transfers,'_audit',fail_final)
        with pytest.raises(RuntimeError):scheduled_transfers._execute_due(db,sid,due)
    db.expire_all()
    assert number(db)==0 and db.get(ScheduledTransfer,sid).status=='scheduled'
    grant=db.scalar(select(SecurityAuthorization).where(SecurityAuthorization.resource=='schedule:'+sid))
    assert grant.status=='active' and grant.remaining==1
    first=scheduled_transfers._execute_due(db,sid,due);second=scheduled_transfers._execute_due(db,sid,due)
    assert first['receipt']==second['receipt'] and number(db)==1