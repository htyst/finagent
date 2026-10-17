from datetime import date

import pytest
from sqlalchemy import select

from app.data.models import Account, Card, DemoSession, Recipient, User
from app.data.seed import check_ledger, reset_demo
from app.domain.directory import AddCard, AddRecipient, add_card, add_recipient
from app.domain.errors import DomainError
from app.domain.operations import prepare_card_action, prepare_transfer, issue_confirmation_token, confirm_proposal
from test_http import client, headers  # noqa: F401


def card_payload(**changes):
    return dict(alias='旅行卡', bank_name='合成银行', card_number='6222 0000 0000 8899',
                account_alias='旅行账户', opening_balance_minor=123456, **changes)


def recipient_payload():
    return dict(name='测试收款人', bank_name='合成银行', account_number='6222000000007788',
                phone='+86 13900000002', remark='旅行伙伴')


def test_add_card_new_account_is_spendable_and_freezable(bank):
    db, _ = bank
    row = add_card(db, 's1', AddCard(**card_payload()))
    account = db.get(Account, row['account_id'])
    assert account.balance_minor == account.opening_balance_minor == 123456
    assert 'card_number' not in row and row['card_mask'] == '**8899'
    draft = prepare_transfer(db, session_id='s1', source_account_id=account.id,
        target={'type': 'own_account', 'account_id': 'a1c'}, amount_minor=1000)
    pid = draft['proposal']['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    assert db.get(Account, account.id).balance_minor == 122456
    frozen = prepare_card_action(db, session_id='s1', card_id=row['id'], action='freeze')
    pid = frozen['proposal']['id']
    token = issue_confirmation_token(db, session_id='s1', proposal_id=pid)['confirmation_token']
    confirm_proposal(db, session_id='s1', proposal_id=pid, confirmation_token=token)
    assert db.get(Card, row['id']).status == 'frozen'
    assert check_ledger(db) == []


def test_card_existing_account_does_not_change_balance_and_duplicates_are_atomic(bank):
    db, _ = bank
    before = db.get(Account, 'a1c').balance_minor
    payload = AddCard(alias='副卡', bank_name='合成银行', card_number='6222000000008877', account_id='a1c')
    row = add_card(db, 's1', payload)
    assert row['account_id'] == 'a1c'
    assert db.get(Account, 'a1c').balance_minor == before
    with pytest.raises(DomainError, match='银行卡已添加'):
        add_card(db, 's1', payload)
    with pytest.raises(DomainError):
        add_card(db, 's1', AddCard(alias='跨用户卡', bank_name='合成银行', card_number='6222000000008866', account_id='a2c'))
    with pytest.raises(DomainError):
        add_card(db, 's1', AddCard(alias='改余额', bank_name='合成银行', card_number='6222000000008855', account_id='a1c', opening_balance_minor=1))
    assert db.get(Account, 'a1c').balance_minor == before


def test_duplicate_new_card_does_not_create_extra_account(bank):
    db, _ = bank
    payload = AddCard(**card_payload())
    add_card(db, 's1', payload)
    count = len(db.scalars(select(Account)).all())
    with pytest.raises(DomainError):
        add_card(db, 's1', payload)
    assert len(db.scalars(select(Account)).all()) == count


def test_added_recipient_can_receive_transfer_and_duplicate_is_rejected(bank):
    db, _ = bank
    payload = AddRecipient(**recipient_payload())
    row = add_recipient(db, 's1', payload)
    assert row['phone_mask'] == '139****0002' and row['account_mask'] == '**7788'
    assert db.get(Recipient, row['id']).phone == '13900000002'
    draft = prepare_transfer(db, session_id='s1', source_account_id='a1c',
        target={'type': 'saved_recipient', 'recipient_id': row['id']}, amount_minor=100)
    assert draft['proposal']['summary']['target']['id'] == row['id']
    with pytest.raises(DomainError, match='收款账号已保存'):
        add_recipient(db, 's1', payload)


def test_directory_http_csrf_validation_and_masking(client):
    protected = headers(client)
    assert client.post('/api/cards', json=card_payload()).status_code == 403
    assert client.post('/api/recipients', json=recipient_payload()).status_code == 403
    for payload in [dict(card_payload(), card_number='1234'), dict(card_payload(), opening_balance_minor=-1),
                    dict(card_payload(), owner_id='u2'), dict(card_payload(), alias='   ')]:
        assert client.post('/api/cards', headers=protected, json=payload).status_code == 422
    assert client.post('/api/recipients', headers=protected, json=dict(recipient_payload(), phone='1234')).status_code == 422
    result = client.post('/api/cards', headers=protected, json=card_payload())
    assert result.status_code == 201
    assert '6222000000008899' not in result.text
    result = client.post('/api/recipients', headers=protected, json=recipient_payload())
    assert result.status_code == 201
    found = client.get('/api/recipients?q=13900000002').json()['data']
    assert found[0]['remark'] == '旅行伙伴' and found[0]['bank_name'] == '合成银行'
    assert '6222000000007788' not in str(found)

