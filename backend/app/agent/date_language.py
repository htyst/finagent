"""Deterministic dates after semantic selection; never chooses a business intent."""
import calendar,re
from datetime import date,timedelta
from app.domain.clock import parse_day,selected_range,shifted_month
from app.domain.errors import DomainError

def chinese_number(value):
    if value.isdigit():return int(value)
    digits={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if '十' in value:
        a,b=value.split('十',1);return (digits.get(a,1))*10+digits.get(b,0)
    return digits.get(value)

def explicit_span(text,demo,previous):
    text=re.sub(r'([一二两三四五六七八九十]{1,3})(?=月份?|日|号|季度)',lambda m:str(chinese_number(m[1])),text)
    iso=re.findall(r'\d{4}-\d{2}-\d{2}',text)
    if iso:
        if len(iso)>2:raise DomainError('INVALID_INPUT','发现多个日期，请明确唯一查询区间')
        span=(iso[0],iso[-1]);selected_range(*span);return span
    base=parse_day(previous['start']).year if previous.get('start') else demo.year
    if '去年' in text:base=demo.year-1
    elif '前年' in text:base=demo.year-2
    elif '今年' in text:base=demo.year
    try:
        # Specific day/range must precede the broader month/year patterns.
        day=r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]'
        match=re.search(day+r'\s*(?:至|到|—|~|～|-)\s*(?:(?:(\d{4})年)?(\d{1,2})月)?(\d{1,2})[日号]',text)
        if match:
            y,m,d,ey,em,ed=match.groups();y=int(y or base);m=int(m)
            first=date(y,m,int(d));last=date(int(ey or (y+1 if em and int(em)<m else y)),int(em or m),int(ed))
            selected_range(first.isoformat(),last.isoformat());return first.isoformat(),last.isoformat()
        match=re.search(day,text)
        if match:
            y,m,d=match.groups();value=date(int(y or base),int(m),int(d)).isoformat();return value,value
        match=re.search(r'(?:(\d{4})年)?(\d{1,2})月(?:份)?\s*(?:至|到|—|~|～|-)\s*(?:(\d{4})年)?(\d{1,2})月',text)
        if match:
            y,m,ey,em=match.groups();y=int(y or base);m=int(m);em=int(em);ey=int(ey or (y+1 if em<m else y))
            return date(y,m,1).isoformat(),date(ey,em,calendar.monthrange(ey,em)[1]).isoformat()
        match=re.search(r'(?:(\d{4})年)?第?([1-4])季度',text)
        if match:
            y=int(match[1] or base);m=(int(match[2])-1)*3+1
            return date(y,m,1).isoformat(),date(y,m+2,calendar.monthrange(y,m+2)[1]).isoformat()
        if re.search(r'最近一?年|近一?年|过去一?年|近12个月|过去12个月',text):
            return (shifted_month(demo,-12)+timedelta(days=1)).isoformat(),demo.isoformat()
        if '上季度' in text or '本季度' in text:
            start=demo.replace(month=((demo.month-1)//3)*3+1,day=1)
            if '上季度' in text:return shifted_month(start,-3).isoformat(),(start-timedelta(days=1)).isoformat()
            return start.isoformat(),demo.isoformat()
        match=re.search(r'(?:(\d{4})年)?(\d{1,2})月',text)
        if match:
            y,m=int(match[1] or base),int(match[2]);return date(y,m,1).isoformat(),date(y,m,calendar.monthrange(y,m)[1]).isoformat()
        match=re.search(r'(\d{4})年',text)
        if match and re.search(r'年度|全年|年报',text):
            y=int(match[1]);return date(y,1,1).isoformat(),date(y,12,31).isoformat()
    except (ValueError,OverflowError) as error:raise DomainError('INVALID_INPUT','查询日期无效') from error
    return None
