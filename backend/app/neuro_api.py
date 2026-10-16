"""Additive API routes with existing identity and page-CSRF protections."""
import json
import os
from datetime import timedelta
from threading import Event,Thread
from fastapi import Depends,Request
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.clock import get_clock,ManualClock,now,configure
from app.data.db import get_db,SessionLocal
from app.data.models import DemoSession,DemoSetting,Proposal
from app.neuro_models import TraceRecord,VerificationRecord,StandingOrder,StandingOccurrence,install
from app.domain.errors import DomainError
from app import standing

class Advance(BaseModel):
    model_config=ConfigDict(extra='forbid')
    days: StrictInt=Field(ge=0,le=366)
class Confirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmation_token: str

def attach(app):
    from app.main import require_demo_session,require_write_protection,envelope,fail
    stop=Event()
    @app.on_event('startup')
    def startup():
        # Add only our tables. Do not run legacy migrations or reset any data.
        # Dependency overrides keep tests away from the user's main database.
        provider=app.dependency_overrides.get(get_db)
        if provider:
            gen=provider()
            db=next(gen)
            try:
                install(db.get_bind())
            finally:
                gen.close()
            return
        with SessionLocal() as db:
            install(db.get_bind())
            setting=db.get(DemoSetting,1)
            configure(initial=setting.demo_date if setting else '2026-09-24')
        if not isinstance(get_clock(),ManualClock) and os.getenv('FINAGENT_SCHEDULER','on')=='on':
            stop.clear()
            def loop():
                import logging
                while not stop.wait(30):
                    try:
                        with SessionLocal() as db:
                            standing.tick_active_sessions(db)
                    except Exception:
                        logging.getLogger(__name__).exception('周期调度失败')
            app.state.standing_thread=Thread(target=loop,daemon=True)
            app.state.standing_thread.start()
    @app.on_event('shutdown')
    def shutdown():
        stop.set()

    def manual_only(request):
        from app.config import is_demo_admin_request
        if not isinstance(get_clock(),ManualClock):
            fail('CLOCK_FORBIDDEN','真实时钟模式不可使用调试日历',403)
        if not is_demo_admin_request(request.client.host if request.client else None):
            fail('CLOCK_FORBIDDEN','调试日历仅供本机演示',403)
    @app.get('/api/debug/clock')
    def clock_read(request:Request,session:DemoSession=Depends(require_demo_session)):
        manual_only(request)
        return envelope({'mode':'manual','now':now().isoformat(),'timezone':'Asia/Shanghai'})
    @app.post('/api/debug/clock/advance')
    def advance(payload:Advance,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        manual_only(request)
        require_write_protection(request,session)
        clock=get_clock()
        clock.advance(days=payload.days)
        # Calendar simulation keeps the initiating UI session usable, not business proposals.
        session.expires_at_utc=(now()+timedelta(hours=8)).isoformat()
        db.commit()
        notifications=standing.tick(db,session)
        return envelope({'now':now().isoformat(),'notifications':notifications})
    @app.get('/api/debug/trace/{trace_id}')
    def trace(trace_id:str,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        row=db.scalar(select(TraceRecord).where(TraceRecord.id==trace_id,TraceRecord.owner_id==session.owner_id))
        if not row:
            fail('RESOURCE_NOT_FOUND','轨迹不存在',404)
        return envelope(json.loads(row.payload_json))
    @app.get('/api/debug/verification/{record_id}')
    def verification(record_id:str,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        row=db.scalar(select(VerificationRecord).where(VerificationRecord.id==record_id,VerificationRecord.owner_id==session.owner_id))
        if not row:
            fail('RESOURCE_NOT_FOUND','验证记录不存在',404)
        return envelope(json.loads(row.payload_json))
    @app.get('/api/standing-orders')
    def orders(session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        ids=db.scalars(select(StandingOrder.id).where(StandingOrder.owner_id==session.owner_id,
                       StandingOrder.dataset_version==session.dataset_version)).all()
        return envelope([standing.view_order(db,session.owner_id,i) for i in ids])
    @app.post('/api/standing-orders/tick')
    def tick(request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(standing.tick(db,session))
    @app.post('/api/standing-occurrences/{occurrence_id}/claim')
    def claim(occurrence_id:str,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(standing.claim_occurrence(db,session,occurrence_id))
    @app.get('/api/standing-orders/notifications')
    def notifications(session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        order_ids=select(StandingOrder.id).where(StandingOrder.dataset_version==session.dataset_version)
        rows=db.scalars(select(StandingOccurrence).where(StandingOccurrence.owner_id==session.owner_id,
                      StandingOccurrence.order_id.in_(order_ids)).order_by(StandingOccurrence.created_at)).all()
        return envelope([{'id':r.id,'period':r.period,'status':(db.get(Proposal,r.proposal_id).status if r.proposal_id and db.get(Proposal,r.proposal_id) else r.status),'proposal_id':r.proposal_id,'message':r.message} for r in rows])
    @app.post('/api/standing-orders/{order_id}/{operation}')
    def manage(order_id:str,operation:str,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(standing.manage(db,session,order_id,operation))
    @app.post('/api/standing-confirmations/{cid}/confirmation-token')
    def token(cid:str,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(standing.issue_token(db,session,cid))
    @app.post('/api/standing-confirmations/{cid}/confirm')
    def confirm(cid:str,payload:Confirm,request:Request,session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        require_write_protection(request,session)
        return envelope(standing.confirm(db,session,cid,payload.confirmation_token))
