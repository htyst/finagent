"""Generated from actual Pydantic tools, registered policies and observation envelope."""
import hashlib,json
from pydantic import BaseModel,Field,ConfigDict
from typing import Literal

# Retain HTTP/old saved plans and validation compatibility; prefer dedicated names
# in new provider requests to avoid duplicate descriptions of the same action.
COMPATIBILITY_ONLY={'prepare_card_action','prepare_card_management','prepare_schedule','prepare_collection','prepare_subscription_reminder','prepare_subscription_revoke'}

class ObservationContract(BaseModel):
    model_config=ConfigDict(extra='allow')
    call_id: str
    status: Literal['completed','rejected']
    source_ref: str
    facts: list[dict] = Field(default_factory=list)
    data: object | None = None
    error: dict | None = None


def generate():
    from app.agent import dynamic_protocol as p,registry
    from app.agent.execution_graph import GraphContract
    entries=[]
    for name,model in sorted(p.models().items()):
        alias=p.ALIASES.get(name)
        entries.append(dict(name=name,model_visible=name not in COMPATIBILITY_ONLY,
            alias_of=alias[0] if alias else None,fixed_arguments=alias[1] if alias else {},
            description=p.DESCRIPTIONS.get(name,registry.DESCRIPTIONS.get(name,name)),
            input_schema=model.model_json_schema(),output_schema=ObservationContract.model_json_schema(),
            output_schema_scope='runtime observation envelope; domain data is tool-dependent, not claimed fully typed',
            risk_policy=registry.execution_policy(name),owner_check='server session+owner+dataset+task',
            authorization='independent fixed form; no model submit',idempotency='domain resource ID and receipt; prepare replay guard'))
    payload=dict(contract_version='tool-contracts-v1',tool_count=len(entries),model_tool_count=sum(e['model_visible'] for e in entries),
        tools=entries,execution_graph_schema=GraphContract.model_json_schema())
    payload['sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return payload
