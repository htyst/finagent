"""Evidence-linked financial synthesis, independent of tool selection or execution."""
import re
from pydantic import Field
from app.agent.dialogue_schema import SemanticModel
from app.domain.errors import DomainError


class Point(SemanticModel):
    text: str = Field(min_length=1,max_length=160)
    fact_ids: list[str] = Field(min_length=1,max_length=4,description='支持这项判断的本轮 facts.id；情景至少引用一个 calculate 结果。')


class Synthesis(SemanticModel):
    goal: str = Field(min_length=1,max_length=100,description='沿用用户真实目标，不把未知预算、期限或风险偏好写成已知事实。')
    conclusion: str = Field(min_length=1,max_length=160,description='一句判断，先回答当前目标的最重要取舍，不列数据清单、不复述情景；未知成本时不宣称预算足够。')
    reasons: list[Point] = Field(min_length=1,max_length=3,description='每项连接相关事实与用户决策，不逐项复述资产清单或罗列通用知识。')
    scenarios: list[Point] = Field(default_factory=list,max_length=2,description='可选，最多两个已经 calculate 核算的预算情景，注明用户条件或假设。')
    questions: list[str] = Field(default_factory=list,max_length=2,description='最多两个会改变结论的必要问题，已有信息不重复问；无需追问可为空。')


def validate_judgment(text,selected_facts):
    buffers=[f for f in selected_facts if f.get('metric')=='funding_buffer']
    if buffers and all(f.get('verdict')=='positive_buffer' and float(f['value'])>0 for f in buffers):
        if re.search(r'几乎(?:没有|不留)|基本不留|没有额外(?:现金|缓冲|余量)',text):
            raise DomainError('ANALYSIS_JUDGMENT','当前目标测算在完整保留生活费、支付目标成本后仍有正数额外余量。请直接解释余量与未知应付款，不凭感觉说没有额外缓冲。',422)
        if re.search(r'(?:旅行|旅游|目标|预算|费用|成本|\{\{f\d+\}\}|\d+\s*元).{0,10}(?:只能|必须|不得不|必然).{0,16}(?:从|动用|挪用).{0,12}(?:预留|应急)',text):
            raise DomainError('ANALYSIS_JUDGMENT','这项测算仅使用当前活期，没有依赖未来收入，并已同时扣除生活费预留与目标成本。不能再称目标费用只能从预留里挪用；新增应付款须另列条件。',422)


def response_text(synthesis,r):
    by_id={f['id']:f for f in r['facts']};chosen=set();refs=[]
    for point in [*synthesis.reasons,*synthesis.scenarios]:
        if any(k not in by_id for k in point.fact_ids):
            raise DomainError('ANALYSIS_REFERENCE','分析要点必须引用本轮未过期的facts.id，不能沿用旧轮数据。',422)
        chosen.update(point.fact_ids)
        refs.extend(by_id[k]['source_ref'] for k in point.fact_ids)
    for scenario in synthesis.scenarios:
        if not any(by_id[k].get('tool')=='calculate' and by_id[k]['key']=='result' for k in scenario.fact_ids):
            raise DomainError('ANALYSIS_REFERENCE','预算情景须引用服务端calculate结果；未核算时请先计算或说明缺项。',422)
    texts=[synthesis.conclusion,*[p.text for p in synthesis.reasons],*[p.text for p in synthesis.scenarios],*synthesis.questions]
    selected=set(re.findall(r'\{\{(f\d+)\}\}','\n'.join(texts)))
    if selected-chosen:
        # Conclusion may use another relevant fact; still limit the full answer.
        if any(k not in by_id for k in selected):raise DomainError('ANALYSIS_REFERENCE','分析引用了不存在或已过期的数字。',422)
        refs.extend(by_id[k]['source_ref'] for k in selected)
    # Focus is primarily a writing goal. Do not spend repeated model calls on a
    # harmless ninth fact; section lengths already bound the rendered answer.
    if len(chosen|selected)>16:
        raise DomainError('ANALYSIS_FOCUS','关联证据过多，请保留最影响当前决策的依据，避免复述整个档案。',422)
    validate_judgment('\n'.join(texts),[by_id[k] for k in chosen|selected])
    text=synthesis.conclusion+'\n\n'+'\n'.join('• '+p.text for p in synthesis.reasons)
    if synthesis.scenarios:text+='\n\n'+'\n'.join('• '+p.text for p in synthesis.scenarios)
    if synthesis.questions:text+='\n\n'+'\n'.join(synthesis.questions)
    return text,list(dict.fromkeys(refs)),chosen|selected


def notes(r,selected):
    basis=next((e.get('data') for e in reversed(r['evidence']) if e['tool']=='financial_planning_basis' and not e.get('error')),None)
    lines=[]
    if basis:
        coverage=basis['coverage']
        lines.append('依据：'+basis['period_start']+' 至 '+basis['period_end']+'的完整月流水；可用活期为本轮查询，其他资产负债快照截至'+basis['snapshot_as_of']+'。')
        if coverage['status']!='complete':lines.append('部分月份缺失；均值仅取完整覆盖月份，不能代表全部区间。')
    assumptions={}
    for f in r['facts']:
        if f['id'] in selected:
            for a in f.get('assumptions',[]):assumptions[(a['label'],a['value'],a['unit'],a['source'])]=a
    if assumptions:
        lines.append('情景参数：'+'；'.join(a['label']+'='+a['value']+('元' if a['unit']=='yuan' else '')+
            ('（用户提供）' if a['source']=='用户明确参数' else '（假设，待确认）') for a in list(assumptions.values())[:4])+'。')
    return '\n'.join(lines)
