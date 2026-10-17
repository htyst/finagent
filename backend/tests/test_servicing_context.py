import pytest
from app.agent.servicing_flow import validate_context
from app.domain.errors import DomainError


@pytest.mark.parametrize('context',[
    {'conditions':['保留当前权益至原截止日期']},
    {'prohibitions':['不解除原有银行代扣授权','不要取消其余订阅']},
    {'time_requirements':['本期结束后停止续订']},
])
def test_fixed_contract_policy_accepts_equivalent_constraints(context):
    validate_context('prepare_merchant_contract_cancel',context)


@pytest.mark.parametrize('context',[
    {'conditions':['保留本期权益至2030年']},
    {'time_requirements':['明天再取消']},
    {'prohibitions':['保留旧卡继续使用']},
])
def test_unknown_constraints_are_not_silently_dropped(context):
    with pytest.raises(DomainError):validate_context('prepare_merchant_contract_cancel',context)
