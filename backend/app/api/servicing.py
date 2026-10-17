"""Human-only confirmation uses the shared security channel; approvals require a separate role."""
from typing import Literal
from fastapi import Depends,Request
from pydantic import BaseModel,ConfigDict,Field
from app.domain import servicing


class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    decision:Literal['approve','reject']
    reason:str=Field(min_length=1,max_length=200)


def register_routes(app,get_db,require_demo_session,protect,envelope):
    @app.get('/api/service-requests')
    def records(kind:Literal['replace_card','credit_limit','cancel_contract']|None=None,
                session=Depends(require_demo_session),db=Depends(get_db)):
        return envelope(servicing.records(db,session_id=session.id,kind=kind))

    @app.post('/api/service-requests/{rid}/decision')
    def decision(rid:str,payload:Decision,request:Request,session=Depends(require_demo_session),db=Depends(get_db)):
        from app.api.roles import require_approver
        protect(request,session);require_approver(request)
        return envelope(servicing.decide(db,session_id=session.id,request_id=rid,**payload.model_dump()))
