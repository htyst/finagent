"""Small model facts and server-owned prose, separate from full persisted evidence."""
from copy import deepcopy
import json

QUANTITATIVE={'summarize_spending','generate_statement','inspect_anomalies','read_financial_profile',
              'read_financial_history','analyze_finances','read_subscriptions','identify_subscriptions','list_cards','list_transactions','list_accounts','read_wealth'}

def project(name,data):
    if not isinstance(data,dict):return data
    result=deepcopy(data)
    if name in {'summarize_spending','generate_statement'}:
        for period in [result.get('current'),*(result.get('months') or [])]:
            if not period:continue
            evidence=period.get('evidence',{})
            evidence['items']=evidence.get('items',[])[:5] if name=='summarize_spending' else []
            evidence['excerpt_only']=evidence.get('total',0)>len(evidence['items'])
        if result.get('comparison'):
            result['comparison'].pop('evidence',None)
    if name=='read_financial_profile':
        result.pop('overview',None) # legacy Chinese text duplicates facts and snapshot values
    if name in {'read_subscriptions','identify_subscriptions'}:
        target=result.get('confirmed',result)
        if isinstance(target,dict):
            events=target.get('billing_events',[])
            target['billing_events']=events[:5]
            target['billing_excerpt_only']=target.get('billing_total',len(events))>len(target['billing_events'])
            target['detail_tool']='read_subscriptions with subscription_id/start/end/status/offset'
    return result

def memory_reference(item):
    data=item.get('data') or {}
    if not isinstance(data,dict):data={}
    return {k:item[k] for k in ('tool','query','source_ref','coverage','error') if k in item}|{
        'summary':{key:data[key] for key in ('summary','analysis_kind','included_sources','excluded_sources','dataset_version','as_of') if key in data}}

def analysis_text(result):
    s=result['summary'];kind=result['analysis_kind'];lines=[result['basis']+'。']
    if kind=='cashflow':lines.append('收入 {:.2f} 元；现金流入 {:.2f} 元，现金流出 {:.2f} 元，净现金流 {:.2f} 元。'.format(s['income_minor']/100,s['inflow_minor']/100,s['outflow_minor']/100,s['net_cashflow_minor']/100))
    elif kind=='largest_expenses':
        lines.append(f"从全部{s['eligible_purchase_count']}笔已入账消费中选出前{s['returned_count']}笔：")
        lines += [f"• {r['posted_at_utc']} {r['merchant'] or '商户未提供'}：{r['amount_minor']/100:.2f}元，证据{r['id']}。" for r in result['items']]
    else:
        lines.append(f"全范围净消费{s['net_minor']/100:.2f}元；共{s['group_count']}组，当前展示{s['returned_groups']}组。")
        lines += [f"• {r['group']}：消费{r['purchase_minor']/100:.2f}元，退款{r['refund_minor']/100:.2f}元，净消费{r['net_minor']/100:.2f}元。" for r in result['items']]
    return '\n'.join(lines)

def history_text(result):
    lines=[f"{result['table']}档案共{result['total']}条；当前第{result['offset']+1}条起，展示{len(result['items'])}条，快照截至{result['snapshot_as_of']}。"]
    for row in result['items']:
        cells=[]
        for key,value in row.items():
            if value in ('',None) or key in {'owner_id','data_origin','user_name','note','valuation_basis'}:continue
            if key.endswith('_minor'):
                try:value=f'{int(value)/100:.2f}元'
                except (TypeError,ValueError):pass
            cells.append(f'{key}={value}')
        lines.append('• '+'；'.join(cells))
    if result.get('has_more'):lines.append(f"还有后续明细，可用同条件offset={result['offset']+result['limit']}继续查询。")
    return '\n'.join(lines)
