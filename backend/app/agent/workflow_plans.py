"""Model-proposed read-only terms; only the existing consent API schedules work."""
from datetime import timedelta
from uuid import uuid4
import json
from pydantic import Field, StrictInt
from app.agent.dialogue_schema import SemanticModel
from app.domain import workflows as w
from app.domain.errors import DomainError


class ReviewDraft(SemanticModel):
    title: str = Field(min_length=1,max_length=100)
    run_at: str
    end_at: str
    interval_days: StrictInt = Field(ge=0,le=90)
    target_yuan: str | None = None
    instruction_quote: str = Field(min_length=1,max_length=1000)


def prepare(task,r,args,node):
    from app.agent.requirements import yuan_minor
    if args['instruction_quote'] not in r['request']:
        raise DomainError('UNGROUNDED_OPERATION','复查草稿必须引用本轮用户原话',422)
    amount=yuan_minor(args['target_yuan']) if args['target_yuan'] is not None else None
    if args['target_yuan'] is not None and (amount is None or amount<=0):
        raise DomainError('INVALID_REVIEW_AMOUNT','复查参考额须为正数',422)
    terms=w.ReviewInput(task_id=task.id,title=args['title'],run_at=args['run_at'],end_at=args['end_at'],
        interval_days=args['interval_days'],target_minor=amount,consent=False,request_id=uuid4().hex)
    instant=w.now()
    if not instant<w.parse_time(terms.run_at)<=w.parse_time(terms.end_at)<=instant+timedelta(days=366):
        raise DomainError('INVALID_REVIEW_TIME','复查须在未来开始，授权期限最多一年',422)
    ctx=json.loads(task.context_json)
    # A replacement draft grants nothing and leaves authorized reviews untouched.
    draft=terms.model_dump(exclude={'request_id','consent'})|dict(id=uuid4().hex,node_id=node['id'],status='draft',
        scope='仅本人人民币可用余额；不转账、不锁款、不读取订阅合同；页面确认后才生效。',
        notification='结果保存在联动计划页面，不代表已配置短信或站外推送。')
    ctx['workflow_review_draft']=draft;task.context_json=json.dumps(ctx,ensure_ascii=False)
    return dict(status='draft',draft=draft,requires_confirmation=True,scheduled=False,
        next_step='打开联动计划页面核对草稿，勾选只读范围并手动确认。')
