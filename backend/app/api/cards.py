"""Only UI dedicated confirmation and simulated approval grant authority."""
from fastapi import Depends,Request,Response
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from typing import Literal
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain.card_management import portfolio,issue_token,confirm,approve,cancel,payment
class ConfirmPayload(BaseModel):
 model_config=ConfigDict(extra='forbid')
 confirmation_token:str=Field(min_length=1,max_length=200)
class PaymentPayload(BaseModel):
 model_config=ConfigDict(extra='forbid')
 card_id:str=Field(min_length=1,max_length=100)
 amount_minor:StrictInt=Field(gt=0,le=100000000000)
 channel:Literal['online','offline']
 idempotency_key:str=Field(min_length=1,max_length=100)
def register_routes(app,get_db,require_demo_session,require_write_protection,envelope):
 @app.get('/api/card-management')
 def read(session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
  return envelope(portfolio(db,session.id))
 @app.post('/api/card-management/requests/{rid}/confirmation-token')
 def token(rid:str,request:Request,response:Response,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
  require_write_protection(request,session);response.headers['Cache-Control']='no-store';return envelope(issue_token(db,session_id=session.id,request_id=rid))
 @app.post('/api/card-management/requests/{rid}/confirm')
 def confirm_route(rid:str,payload:ConfirmPayload,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
  require_write_protection(request,session);return envelope(confirm(db,session_id=session.id,request_id=rid,confirmation_token=payload.confirmation_token))
 @app.post('/api/card-management/requests/{rid}/approve')
 def approve_route(rid:str,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
  from app.api.roles import require_approver
  require_write_protection(request,session);require_approver(request);return envelope(approve(db,session_id=session.id,request_id=rid))
 @app.post('/api/card-management/requests/{rid}/cancel')
 def cancel_route(rid:str,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
  require_write_protection(request,session);return envelope(cancel(db,session_id=session.id,request_id=rid))
 @app.post('/api/card-management/payments')
 def payment_route(payload:PaymentPayload,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
  require_write_protection(request,session);return envelope(payment(db,session_id=session.id,**payload.model_dump()))
