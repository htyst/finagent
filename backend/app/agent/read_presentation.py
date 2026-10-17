"""Presentation of verified read results, including safe partial-answer recovery."""
import re
from app.agent import analysis_workflow as facts


def fact_block(runtime):
    lines=[];seen=set()
    for f in facts.focused_facts(runtime['facts']):
        label=re.sub(r'（[A-Za-z_][A-Za-z0-9_.]*）','',f['label'])
        if f['key'].startswith('comparison.'):label='对照区间'+label
        signature=(f['source_ref'],label,f['value'],f['unit'])
        if signature in seen:continue
        seen.add(signature);value=facts.decimal(f['value'])
        lines.append('• '+label+'：'+(f'{value:,.2f} 元' if f['unit']=='yuan' else format(value.normalize(),'f')))
    return '已查询的数据：\n'+'\n'.join(lines) if lines else ''


def spending_summary(data):
    from app.agent.analytics_summary import range_label
    current=data['current'];totals=current['totals'];category=data.get('category') or '全部分类'
    known='' if totals['is_complete'] else '已记录'
    lines=[f"{range_label(current['period'])}，按账本“{category}”分类统计：",
        f"{known}消费 {totals['purchase_minor']/100:,.2f} 元，{known}退款 {totals['refund_minor']/100:,.2f} 元，{known}净支出 {totals['net_minor']/100:,.2f} 元。"]
    if not totals['is_complete']:lines.append('这段时间有部分流水缺失，以上是已记录金额，不能视为完整总支出。')
    comparison=data.get('comparison')
    if comparison:
        prior=comparison['totals'];prefix='' if prior['is_complete'] else '已记录'
        lines.append(f"对照区间 {range_label(comparison['period'])}，{prefix}净支出 {prior['net_minor']/100:,.2f} 元。")
        if totals['is_complete'] and prior['is_complete'] and comparison.get('delta_minor') is not None:
            lines.append(f"净支出变化 {comparison['delta_minor']/100:+,.2f} 元"+(f"（{comparison['percent_change']}%）" if comparison.get('percent_change') is not None else '')+'。')
        else:lines.append('对照区间或本期存在数据缺口，不能据此判断实际支出的增减。')
    return '\n'.join(lines)


def scope_notes(runtime):
    from app.agent.analytics_summary import range_label
    lines=[]
    for e in runtime['evidence']:
        if e.get('error') or e['tool']!='summarize_spending':continue
        data=e.get('raw_data') or e.get('data');current=data['current']
        lines.append('统计区间：'+range_label(current['period'])+'（北京时间）。'+(
            '本期流水覆盖完整。' if current['totals']['is_complete'] else '本期有部分流水缺失，金额仅反映已记录数据。'))
        prior=data.get('comparison')
        if prior:lines.append('对照区间：'+range_label(prior['period'])+'。'+('对照数据完整。' if prior['totals']['is_complete'] else '对照数据有缺口，无法判断实际增减。'))
    return '\n'.join(dict.fromkeys(lines))


def fallback(runtime,code):
    sections=[];seen=set()
    for e in reversed(runtime['evidence']):
        if e.get('error') or e['tool']!='summarize_spending':continue
        data=e.get('raw_data') or e.get('data')
        if not isinstance(data,dict) or 'current' not in data:continue
        key=(repr(data['current']['period']),data.get('category'),data.get('merchant'),tuple(data.get('account_ids',[])),data.get('card_id'))
        if key in seen:continue
        seen.add(key);sections.append(spending_summary(data))
        if len(sections)==3:break
    if sections:
        text='\n\n'.join(reversed(sections))
        if code not in {'ANALYSIS_INCOMPLETE','ANALYSIS_ANSWER_INVALID','UNSUPPORTED_ANSWER'}:
            text+='\n\n已保留查询结果，进一步分析暂未完成，可稍后继续。'
    else:
        text='已保留查询结果，进一步分析暂未完成，可稍后继续。' if runtime['evidence'] else '暂时未能完成查询，请稍后重试或缩小查询范围。'
        block=fact_block(runtime)
        if block:text+='\n\n'+block
    if runtime.get('completed_operations'):text+='\n\n已经办理的结果请查看原操作单和回执。'
    return text


def count_facts(data):
    result=[]
    def walk(value,path=''):
        if isinstance(value,dict):
            for key,child in value.items():
                p=path+'.'+key if path else key
                if (key.endswith('_count') or key=='count') and isinstance(child,int) and not isinstance(child,bool):
                    label={'purchase_count':'消费笔数','refund_count':'退款笔数','transaction_count':'交易笔数','count':'笔数'}.get(key,'记录笔数')
                    result.append(dict(key=p,label=label,value=str(child),unit='number'))
                elif key not in {'evidence','billing_events','accounts','cards'}:walk(child,p)
        elif isinstance(value,list):
            for i,row in enumerate(value[:12]):walk(row,path+'.'+str(i))
    walk(data)
    return result[:32]
