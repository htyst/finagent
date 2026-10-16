"""Owner-scoped wealth reads and dedicated UI-only assessment/confirmation controls."""
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain.wealth import portfolio, submit_assessment, issue_token, confirm_order, cancel_order

class AssessmentPayload(BaseModel):
    model_config=ConfigDict(extra='forbid')
    answers: list[StrictInt] = Field(min_length=4,max_length=4)

class ConfirmationPayload(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmation_token: str = Field(min_length=1,max_length=200)

def register_routes(app,get_db,require_demo_session,require_write_protection,envelope):
    @app.get('/api/wealth')
    def wealth_read(session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        return envelope(portfolio(db,session_id=session.id))

    @app.post('/api/wealth/assessment')
    def risk_save(payload:AssessmentPayload,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(submit_assessment(db,session_id=session.id,answers=payload.answers))

    @app.post('/api/wealth/orders/{order_id}/confirmation-token')
    def token(order_id:str,request:Request,response:Response,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session);response.headers['Cache-Control']='no-store'
        return envelope(issue_token(db,session_id=session.id,order_id=order_id))

    @app.post('/api/wealth/orders/{order_id}/confirm')
    def confirm(order_id:str,payload:ConfirmationPayload,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(confirm_order(db,session_id=session.id,order_id=order_id,confirmation_token=payload.confirmation_token))

    @app.post('/api/wealth/orders/{order_id}/cancel')
    def cancel(order_id:str,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(cancel_order(db,session_id=session.id,order_id=order_id))
