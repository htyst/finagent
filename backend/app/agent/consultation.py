"""Read-only consultation boundaries after Live semantic interpretation."""
import re

from app.agent.dialogue_schema import DialogueTurn, Goal


def general_guidance(text):
    # Deliberately whole-message: a mixed request such as "理财建议，并申购P1"
    # must retain its separate write goal and follow the fixed confirmation flow.
    compact = re.sub(r'[\s，,。.!！?？：:]', '', text)
    return bool(re.fullmatch(
        r'(?:请|麻烦)?(?:你)?(?:给我|帮我|我想要|我想了解|我想咨询|我想听听)?'
        r'(?:一些|一点|点|一下)?(?:关于)?(?:个人|通用|一般)?'
        r'(?:理财|投资理财|财务规划)(?:方面|这方面|的)?(?:的)?'
        r'(?:建议|咨询|规划建议)(?:吧|可以吗|好吗)?', compact))


def normalize(decision, text, *, locked=False):
    """An explicit general-advice request cannot become a payment form.

    Interpretation still runs first. Lock the read-only boundary throughout its
    tool loop so a later answer cannot turn retrieved guidance into an order.
    """
    if not (locked or general_guidance(text) or decision.intent == 'wealth_guidance'):
        return decision
    decision = decision.model_copy(deep=True)
    misrouted = decision.intent not in {'wealth_guidance', 'reply'}
    previous = decision.dialogue
    from app.agent.dialogue import READ_TOOLS
    tools = [r for r in previous.tools if r.name in READ_TOOLS] if previous else []
    decision.intent = 'wealth_guidance'
    decision.follow_up = False
    decision.dialogue = previous.model_copy(deep=True) if previous else DialogueTurn(behavior='knowledge')
    decision.dialogue.behavior = 'knowledge'
    decision.dialogue.goals = previous.goals if previous and previous.goals else [Goal(text=text,kind='knowledge')]
    decision.dialogue.tools = tools
    # Transaction slots in a consultation are model output, not user instructions.
    for field in ('source', 'target', 'target_bank', 'target_account', 'amount_yuan',
                  'product', 'units', 'card', 'action', 'target_note', 'schedule_text',
                  'clarification_kind', 'intent_candidates', 'missing_fields'):
        setattr(decision, field, None)
    if misrouted or re.search(r'请(?:明确|补充|提供).*(?:付款账户|产品编号|转账金额)', decision.reply or ''):
        decision.reply = None
    return decision
