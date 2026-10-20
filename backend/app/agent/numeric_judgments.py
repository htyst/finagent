"""Check supported quantitative claims without guessing missing operands."""
import re
import calendar
from decimal import Decimal
from app.domain.errors import DomainError


def reject():
    raise DomainError('ANALYSIS_JUDGMENT','比较结论与已查金额不一致；请按单次合同价、实际大小关系和资金缺口重写，不把年费折月后与单笔限额比较。',422)


def validate(text,r):
    from app.agent.requirements import NUMBER,yuan_minor
    users='\n'.join(r.get('user_messages',[]))
    limits=re.findall(r'(?:单笔(?:交易)?|交易)?限额(?:设为|设成|改为|改成|设置为|为|是|调整为)?\s*('+NUMBER+r')\s*元',users)
    if limits:
        limit=yuan_minor(limits[-1])
        contracts=[]
        for e in r.get('evidence',[]):
            if e.get('error') or e['tool'] not in {'read_subscriptions','identify_subscriptions'}:continue
            data=e.get('raw_data') or e.get('data') or {};contracts=(data.get('confirmed',data)).get('contracts',[])
        if limit is not None and any(Decimal(str(c['current_price_minor']))>limit for c in contracts):
            for clause in re.split('[。；\n]',text):
                if re.search('不能说|无法判断|不能判断|并非|不是所有|并不都',clause):continue
                if re.search(r'(?:都|全部|均).{0,12}(?:低于|不超过|小于)|不会.{0,12}任何.{0,12}(?:失败|超限)|(?:全部|所有).{0,12}(?:不会失败|不超限)',clause):reject()
    buffers=[f for f in r.get('facts',[]) if f.get('metric')=='funding_buffer']
    if any(Decimal(f['value'])<0 for f in buffers) and re.search(r'(?:其中一项都|每一项都|各项均|所有方案都).{0,12}(?:余量|足够|够用|可行)',text):reject()
    means=[Decimal(f['value']) for f in r.get('facts',[]) if f['key']=='monthly_expense_mean']
    if len(set(means))!=1:return
    for e in r.get('evidence',[]):
        if e.get('error') or e['tool']!='summarize_spending':continue
        query=e.get('query',{});start=query.get('start','');end=query.get('end','')
        if not re.fullmatch(r'\d{4}-\d{2}-01',start) or not end.startswith(start[:7]) or query.get('category') or query.get('merchant') or query.get('card_id') or query.get('account_id'):continue
        year,month=map(int,start[:7].split('-'))
        if end!=f'{year:04}-{month:02}-{calendar.monthrange(year,month)[1]}':continue
        data=e.get('raw_data') or e.get('data') or {};totals=data.get('current',{}).get('totals',{})
        if not totals.get('is_complete') or 'net_minor' not in totals:continue
        current=Decimal(totals['net_minor'])/100
        for match in re.finditer(str(int(start[5:7]))+r'月[^。；\n]{0,30}(高于|低于)[^。；\n]{0,15}月均',text):
            if re.search('不高于|不低于|不能判断|无法判断|假设|如果',match[0]):continue
            if current<=means[0] and match[1]=='高于' or current>=means[0] and match[1]=='低于':reject()
