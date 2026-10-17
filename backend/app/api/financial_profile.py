from fastapi import Depends,Query,Request
from sqlalchemy.orm import Session
from app.data.models import DemoSession
from app.domain.financial_profile import portfolio,imported_subscriptions

def check_parameters(request:Request):
    allowed={
        '/api/financial-history':{'table','start','end','card_id','liability_id','subscription_id','status','limit','offset'},
        '/api/financial-analysis':{'kind','start','end','account_id','card_id','category','merchant','top_n'},
        '/api/subscription-analysis':{'start','end','subscription_id','status','limit','offset','contract_limit','contract_offset'},
    }.get(request.url.path,set())
    if set(request.query_params)-allowed:
        from app.domain.errors import DomainError
        raise DomainError('INVALID_INPUT','请求包含不支持的筛选参数',422)

def register_routes(app,get_db,require_demo_session,envelope):
    @app.get('/api/financial-profile')
    def profile(session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        return envelope(portfolio(db,session.owner_id))
    @app.get('/api/financial-profile/subscriptions')
    def subscriptions(session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db)):
        return envelope(imported_subscriptions(db,session.id))

    @app.get('/api/financial-history')
    def history(table:str,start:str|None=None,end:str|None=None,card_id:str|None=None,liability_id:str|None=None,
                subscription_id:str|None=None,status:str|None=None,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),
                session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db),_guard=Depends(check_parameters)):
        from app.domain.financial_profile import read_history
        return envelope(read_history(db,session.owner_id,table,start=start,end=end,card_id=card_id,liability_id=liability_id,
            subscription_id=subscription_id,status=status,limit=limit,offset=offset,session_id=session.id))

    @app.get('/api/financial-analysis')
    def analysis(kind:str,start:str|None=None,end:str|None=None,account_id:str|None=None,card_id:str|None=None,
                 category:str|None=None,merchant:str|None=None,top_n:int=Query(10,ge=1,le=50),
                 session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db),_guard=Depends(check_parameters)):
        from app.domain.financial_analysis import analyze
        return envelope(analyze(db,session.owner_id,kind,start=start,end=end,account_id=account_id,card_id=card_id,
            category=category,merchant=merchant,top_n=top_n))

    @app.get('/api/subscription-analysis')
    def subscription_analysis(start:str|None=None,end:str|None=None,subscription_id:str|None=None,status:str|None=None,
                 limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),contract_limit:int=Query(50,ge=1,le=100),contract_offset:int=Query(0,ge=0),
                 session:DemoSession=Depends(require_demo_session),db:Session=Depends(get_db),_guard=Depends(check_parameters)):
        from app.domain.subscription_read import report
        return envelope(report(db,session.id,start=start,end=end,subscription_id=subscription_id,status=status,limit=limit,offset=offset,contract_limit=contract_limit,contract_offset=contract_offset))
