"""Current-version six-scene transcripts: substituted model, actual domain and auth."""
import json
from pathlib import Path
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.agent import authorization,dynamic_runtime as rt
from app.agent.service import create,view
from app.agent.conversation_reads import confirm_subscription_query
from app.agent.privacy import model_payload
from app.data.models import Card,AgentTask
from app.domain import wealth,subscriptions,birthdays
from test_dynamic_tools import dynamic,sequence,calls,reply,session,transfers
from test_security import mfa

@pytest.mark.parametrize('scene',['transfer','bills','wealth','cards','subscriptions','birthday'])
def test_six_actual_domain_scene_traces(dynamic,monkeypatch,scene):
    db,_=dynamic;user='';stages=[]
    if scene=='transfer':
        user='从活期账户转100元到储蓄账户';steps=[calls(('prepare_transfer',dict(source='活期账户',target='储蓄账户',amount_yuan='100'))),reply('已取得本单回执，请以固定单状态为准。')]
    elif scene=='bills':
        user='查看本月消费与退款，只查询不办理';steps=[calls(('summarize_spending',{})),reply('已核对本人本月记录，消费与退款金额见报告；缺口以覆盖信息为准。')]
    elif scene=='wealth':
        wealth.submit_assessment(db,session_id='s1',answers=[2,2,2,2])
        user='从活期账户申购P1模拟理财100元';steps=[calls(('prepare_wealth_buy',dict(product='P1',source='活期账户',amount_yuan='100'))),reply('已取得申购业务回执，状态以固定单为准。')]
    elif scene=='cards':
        card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'));user='挂失尾号'+card.card_last4+'的银行卡';steps=[calls(('prepare_card_loss',dict(card=card.card_last4))),reply('已取得挂失回执，其他限制以卡片记录为准。')]
    elif scene=='subscriptions':
        subscriptions.seed_test_authorizations(db,owner_id='u1',account_id='a1c');db.commit()
        user='查询云音乐订阅并解除银行代扣';steps=[calls(('read_subscriptions',{})),calls(('prepare_cancel_bank_mandate',dict(merchant='云音乐'))),reply('已取得银行代扣解除回执，商户会员合同未因此取消。')]
    else:
        date=(birthdays.birthday_now(db)+timedelta(days=10)).date().isoformat();user=f'从活期账户为{date}生日安排鲜花和蛋糕，预算1000元，收件人合成收件人，地址合成测试地址，生日前两天北京时间09:00下单，生日当天送达，完成后释放余款，鲜花供应商：模拟花店报价300元，蛋糕供应商：模拟蛋糕店报价200元，免运费'
        steps=[calls(('prepare_birthday_create',dict(source='活期账户',birthday_date=date,birthday_recipient='合成收件人',birthday_address='合成测试地址',budget_yuan='1000',birthday_items=['flowers','cake']))),reply('已取得安排回执，当前是保存计划与预算预留，未表示商品已送达。')]
    sequence(monkeypatch,*steps);result=create(db,session(db),'live',user);stages.append(dict(stage='after_understanding',graph=result['execution_graph'],authorization=[{k:o[k] for k in ('resource_kind','level','status','plan_version')} for o in result.get('authorization_operations',[])]))
    if scene=='subscriptions':
        assert not result['authorization_operations'] and transfers(db)==0
        pending=result['next_actions'][0]['pending_id'];result=confirm_subscription_query(db,session(db),result['id'],pending)
        stages.append(dict(stage='yellow_query_confirmed_not_write',graph=result['execution_graph']))
    if scene!='bills':
        op=next(o for o in result['authorization_operations'] if o['status']=='pending_confirmation')
        assert result['execution_graph']['goals'][0]['status']=='waiting_authorization'
        if scene in {'wealth','cards'}:assert op['level']=='red'
        if op['level']=='red':mfa(db,op['resource_id'],kind=op['resource_kind'])
        token=authorization.issue_token(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'])['confirmation_token']
        paid=authorization.confirm(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'],token)
        result=rt.after_authorization(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'],paid)
        assert result['execution_graph']['goals'][0]['receipt_refs']
        count=transfers(db);again=authorization.confirm(db,session(db),result['id'],op['resource_kind'],op['resource_id'],op['action_id'],token)
        assert transfers(db)==count and again['execution_graph']['goals'][0]['receipt_refs']==result['execution_graph']['goals'][0]['receipt_refs']
    else:assert not result['authorization_operations'] and transfers(db)==0
    assert result['execution_graph']['goals'][0]['status']=='completed'
    stages.append(dict(stage='final',graph=result['execution_graph'],reply=result['messages'][-1]['content']))
    from app.agent.audit import task_audit
    out=Path('var/reviews/remaining-20261018/contest');out.mkdir(parents=True,exist_ok=True)
    (out/(scene+'.json')).write_text(json.dumps(model_payload(dict(scene=scene,user=user,mode='model_substitute+real_domain+local_simulated_MFA',stages=stages,audit=task_audit(db,session(db),result['id']))),ensure_ascii=False,indent=2),encoding='utf-8')
