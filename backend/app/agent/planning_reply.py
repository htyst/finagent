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
    actions: list[str] = Field(default_factory=list,max_length=2,description='最多两个与证据和目标相关的可行行动，不代表已办理，不自行决定多目标优先级。')
    questions: list[str] = Field(default_factory=list,max_length=2,description='最多两个会改变结论的必要问题，已有信息不重复问；无需追问可为空。')


def validate_judgment(text,selected_facts):
    buffers=[f for f in selected_facts if f.get('metric')=='funding_buffer']
    if buffers and all(f.get('verdict')=='positive_buffer' and float(f['value'])>0 for f in buffers):
        if re.search(r'几乎(?:没有|不留)|基本不留|没有额外(?:现金|缓冲|余量)',text):
            raise DomainError('ANALYSIS_JUDGMENT','当前目标测算在完整保留生活费、支付目标成本后仍有正数额外余量。请直接解释余量与未知应付款，不凭感觉说没有额外缓冲。',422)
        if re.search(r'(?:旅行|旅游|目标|预算|费用|成本|\{\{f\d+\}\}|\d+\s*元).{0,10}(?:只能|必须|不得不|必然).{0,16}(?:从|动用|挪用).{0,12}(?:预留|应急)',text):
            raise DomainError('ANALYSIS_JUDGMENT','这项测算仅使用当前活期，没有依赖未来收入，并已同时扣除生活费预留与目标成本。不能再称目标费用只能从预留里挪用；新增应付款须另列条件。',422)


def validate_assertions(text,r):
    # Same guard for structured synthesis and ordinary text; no output bypass.
    if any(f.get('known_dues_not_deducted') for f in r['facts']):
        from app.agent.analysis_workflow import unsupported_affordability_claim
        if unsupported_affordability_claim(text,personal=True):
            raise DomainError('ANALYSIS_JUDGMENT','基础缓冲未扣已知到期款，不能说完整预算足够；明确口径或补充应付款情景。',422)

    # These are unsupported economic assertions, not intent routes.
    if not r.get('completed_operations') and re.search(r'(?:我已|已为你|已经为你|已经替你).{0,6}(?:锁定|预留|转账|申购)',text):
        raise DomainError('ANALYSIS_JUDGMENT','没有实际业务回执，不能把预算测算说成已锁定或已办理。',422)
    for clause in re.split(r'[。；\n]',text):
        qualified=re.search(r'不能|不代表|无法|不等于|未核实|未知|不一定|不要|不应|假设|如果|按用户|按你|情景',clause)
        if not qualified and re.search(r'(?:经营回款|营业额|毛回款).{0,12}(?:就是|全部是|直接作为).{0,8}(?:利润|可自由支配|工资)',clause):
            raise DomainError('ANALYSIS_JUDGMENT','经营毛回款不是已核验利润或可自由支配收入，必须核对经营现金流与支出。',422)
        if not qualified and re.search(r'(?:净资产|授信额度|房车估值|定期本金).{0,12}(?:就是|直接作为|全部是).{0,8}(?:可用现金|可花|活期)',clause):
            raise DomainError('ANALYSIS_JUDGMENT','低流动性资产或授信不等于当前可用现金。',422)
        if not qualified and re.search(r'(?:贷款余额|负债余额).{0,12}(?:就是|全部是|作为).{0,8}(?:本月应付|近期应付|下月应还)',clause):
            raise DomainError('ANALYSIS_JUDGMENT','负债存量不等于近期应付款，应查分期与账单。',422)
        if not qualified and re.search(r'(?:购物|餐饮|文娱|经营成本).{0,12}(?:都是|全部是|全是|均为).{0,8}(?:浪费|可选|非必要|生活费)',clause):
            raise DomainError('ANALYSIS_JUDGMENT','分类或金额大小不证明必要性、浪费或可调整份额；请标明未知并核实。',422)
        flexible=re.search(r'(?:断收|收入中断|回款中断).{0,10}(?:进货|经营成本|经营支出).{0,8}(?:可放缓|可以减少|可以暂停|可暂停|可削减)',clause)
        if flexible and not re.search(r'假设|按用户|按你|不能假定|不能认为|未核实|未知',clause[:flexible.start()]):
            raise DomainError('ANALYSIS_JUDGMENT','收入中断不证明经营支出可同步减少，须核实合同、库存和付款刚性；只能列为待确认假设。',422)
        if not qualified and re.search(r'(?:削减|压缩|减少|节省).{0,12}(?:[0-9]+\s*%|百分之|三成|两成)',clause):
            raise DomainError('ANALYSIS_JUDGMENT','不能凭分类金额建议固定削减比例；需要用户明确的调整假设或已核验依据。',422)
def validate_meaning(synthesis,r):
    text='\n'.join([synthesis.conclusion,*[p.text for p in synthesis.reasons],*[p.text for p in synthesis.scenarios],*synthesis.actions])
    validate_assertions(text,r)
    known={f['key'] for f in r['facts']}
    explicit_reserve=any(a['source']=='用户明确参数' and '预留' in a['label'] for f in r['facts'] for a in f.get('assumptions',[]))
    for question in synthesis.questions:
        if explicit_reserve and re.search(r'(?:预留|保留).{0,12}(?:几个月|多少个月|多久)|更多.{0,6}生活费',question):
            raise DomainError('ANALYSIS_REDUNDANT_QUESTION','用户已明确预留期限，不重复追问或引导改动；只核实仍未知的决策变量。',422)
        if 'available_liquid' in known and re.search(r'(?:当前|现在|你的).{0,8}(?:活期|可用余额|可用现金).{0,8}(?:多少|提供)',question):
            raise DomainError('ANALYSIS_REDUNDANT_QUESTION','当前可用现金已经查询，不重复索取；只问影响目标的未知成本或义务。',422)
        if 'monthly_expense_mean' in known and re.search(r'月均(?:净)?消费.{0,8}多少',question):
            raise DomainError('ANALYSIS_REDUNDANT_QUESTION','历史月均净消费已有依据，不能当作未查询信息重复追问。',422)


def response_text(synthesis,r):
    validate_meaning(synthesis,r)
    by_id={f['id']:f for f in r['facts']};chosen=set();refs=[]
    for point in [*synthesis.reasons,*synthesis.scenarios]:
        if any(k not in by_id for k in point.fact_ids):
            raise DomainError('ANALYSIS_REFERENCE','分析要点必须引用本轮未过期的facts.id，不能沿用旧轮数据。',422)
        chosen.update(point.fact_ids)
        refs.extend(by_id[k]['source_ref'] for k in point.fact_ids)
    for scenario in synthesis.scenarios:
        if not any(by_id[k].get('tool')=='calculate' and by_id[k]['key']=='result' for k in scenario.fact_ids):
            raise DomainError('ANALYSIS_REFERENCE','预算情景须引用服务端calculate结果；未核算时请先计算或说明缺项。',422)
    texts=[synthesis.conclusion,*[p.text for p in synthesis.reasons],*[p.text for p in synthesis.scenarios],*synthesis.actions,*synthesis.questions]
    selected=set(re.findall(r'\{\{(f\d+)\}\}','\n'.join(texts)))
    if selected-chosen:
        # Conclusion may use another relevant fact; still limit the full answer.
        if any(k not in by_id for k in selected):raise DomainError('ANALYSIS_REFERENCE','分析引用了不存在或已过期的数字。',422)
        refs.extend(by_id[k]['source_ref'] for k in selected)
    # Focus is primarily a writing goal. Do not spend repeated model calls on a
    # harmless ninth fact; section lengths already bound the rendered answer.
    if len(chosen|selected)>16:
        raise DomainError('ANALYSIS_FOCUS','关联证据过多，请保留最影响当前决策的依据，避免复述整个档案。',422)
    selected_facts=[by_id[k] for k in chosen|selected]
    if any(f.get('known_dues_not_deducted') for f in selected_facts) and not any(f.get('metric')=='commitment_buffer' for f in selected_facts):
        from app.agent.analysis_workflow import unsupported_affordability_claim
        if unsupported_affordability_claim('\n'.join(texts),personal=True):
            raise DomainError('ANALYSIS_JUDGMENT','这项余量未扣底稿中的已知到期款，不能作为完整可负担结论；改用应付款情景或明确该测算的口径。',422)
    validate_judgment('\n'.join(texts),selected_facts)
    text=synthesis.conclusion+'\n\n'+'\n'.join('• '+p.text for p in synthesis.reasons)
    if synthesis.scenarios:text+='\n\n'+'\n'.join('• '+p.text for p in synthesis.scenarios)
    if synthesis.actions:text+='\n\n'+'\n'.join('• '+a for a in synthesis.actions)
    if synthesis.questions:text+='\n\n'+'\n'.join(synthesis.questions)
    return text,list(dict.fromkeys(refs)),chosen|selected


def notes(r,selected):
    basis=next((e.get('data') for e in reversed(r['evidence']) if e['tool']=='financial_planning_basis' and not e.get('error')),None)
    lines=[]
    if basis:
        coverage=basis['coverage']
        lines.append('依据：'+basis['period_start']+' 至 '+basis['period_end']+'的完整月流水；可用活期为本轮查询，其他资产负债快照截至'+basis['snapshot_as_of']+'。')
        if coverage['status']!='complete':lines.append('部分月份缺失；均值仅取完整覆盖月份，不能代表全部区间。')
    if any(f['id'] in selected and f.get('known_dues_not_deducted') for f in r['facts']):
        lines.append('所列基础缓冲测算尚未扣除底稿已知近期应付款，不是完整可支配预算；已付账单不再次扣减。')
    if any(f['id'] in selected and f.get('metric')=='commitment_buffer' for f in r['facts']):
        lines.append('应付款情景只含直接现金消费及可见到期款，不含尚未出账的信用卡消费或未记录义务。')
    assumptions={}
    for f in r['facts']:
        if f['id'] in selected:
            for a in f.get('assumptions',[]):assumptions[(a['label'],a['value'],a['unit'],a['source'])]=a
    if assumptions:
        lines.append('情景参数：'+'；'.join(a['label']+'='+a['value']+('元' if a['unit']=='yuan' else '')+
            ('（用户提供）' if a['source']=='用户明确参数' else '（假设，待确认）') for a in list(assumptions.values())[:4])+'。')
    return '\n'.join(lines)
