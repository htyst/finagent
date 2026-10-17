import pytest
from sqlalchemy import select,func
from app.data.models import Card,AuditEvent
from app.data.card_models import CardControl
from app.domain import card_management as cards,security
from app.domain.errors import DomainError
from app.agent import dynamic_protocol as protocol,tool_catalog
from app.agent.service import create
from test_security import mfa
from test_dynamic_tools import dynamic,sequence,calls


def lost_card(db):
    card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    c=db.get(CardControl,card.id)
    if c is None:
        c=CardControl(card_id=card.id,limit_minor=50000,online_enabled=0,lost=1,version=1);db.add(c)
    else:c.lost=1;c.online_enabled=0;c.limit_minor=50000
    card.status='frozen';db.commit()
    return card,c


def prepare(db,card):return cards.prepare(db,session_id='s1',kind='unloss',card_id=card.id)['id']


def test_unloss_strong_verification_preserves_other_controls_and_replay(bank):
    db,_=bank;card,c=lost_card(db);before=c.version;rid=prepare(db,card)
    assert c.lost==1 and security.describe(db,'s1','card',rid)['level']=='red'
    token=cards.issue_token(db,session_id='s1',request_id=rid)['confirmation_token']
    with pytest.raises(DomainError) as rejected:cards.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)
    assert rejected.value.code=='MFA_REQUIRED' and c.lost==1
    mfa(db,rid,kind='card')
    result=cards.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)
    assert result['status']=='executed' and c.lost==0 and c.version==before+1
    assert card.status=='frozen' and c.online_enabled==0 and c.limit_minor==50000
    with pytest.raises(DomainError) as frozen:cards.check_purchase(db,card,100,'offline')
    assert frozen.value.code=='INVALID_CARD_STATE'
    again=cards.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)
    assert again['receipt']==result['receipt'] and c.version==before+1
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event=='card_unloss_confirmed'))==1


def test_unloss_rejects_normal_or_other_owner_card(bank):
    db,_=bank
    card=db.scalar(select(Card).where(Card.owner_id=='u1',Card.status=='active'))
    with pytest.raises(DomainError) as normal:prepare(db,card)
    assert normal.value.code=='CARD_NOT_LOST'
    with pytest.raises(DomainError) as other:cards.prepare(db,session_id='other',kind='unloss',card_id=card.id)
    assert other.value.code=='RESOURCE_NOT_FOUND'


def test_unloss_detects_changed_controls_and_honors_cancellation(bank):
    db,_=bank;card,c=lost_card(db);rid=prepare(db,card);mfa(db,rid,kind='card')
    token=cards.issue_token(db,session_id='s1',request_id=rid)['confirmation_token']
    c.online_enabled=1;c.version+=1;db.commit()
    with pytest.raises(DomainError) as stale:cards.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)
    assert stale.value.code=='STALE_RESOURCE' and c.lost==1
    cards.cancel(db,session_id='s1',request_id=rid)
    with pytest.raises(DomainError):cards.confirm(db,session_id='s1',request_id=rid,confirmation_token=token)
    assert c.lost==1


def test_unloss_tool_creates_red_confirmation_without_mutation(dynamic,monkeypatch):
    db,_=dynamic;card,c=lost_card(db)
    sequence(monkeypatch,calls(('prepare_card_unloss',{'card':card.card_last4})))
    result=create(db,db.get(__import__('app.data.models',fromlist=['DemoSession']).DemoSession,'s1'),'live','找到卡了，请解除挂失，尾号'+card.card_last4)
    assert result['card_request']['kind']=='unloss' and result['authorization_operations'][0]['level']=='red'
    assert c.lost==1 and 'prepare_card_unloss' in tool_catalog.catalog()['groups']['卡片管理']
    with pytest.raises(ValueError):protocol.models()['prepare_card_unloss'].model_validate({'card':card.card_last4,'action':'loss'})
