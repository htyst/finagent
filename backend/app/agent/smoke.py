"""M1 opt-in DeepSeek read-only compatibility probe.

No tool executes in this module; it only verifies model requests and validates
returned arguments. It never prints the API key or raw response body.
"""

from __future__ import annotations

import json
import os

import httpx
from pydantic import BaseModel, ConfigDict

from app.config import ROOT  # noqa: F401 - loads the project-local .env


class TransactionRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_date: str
    end_date: str


def main() -> None:
    key = os.getenv("MODEL_API_KEY")
    if not key:
        raise SystemExit("MODEL_API_KEY 未配置；live 小试验未运行")
    base_url = (os.getenv("MODEL_BASE_URL") or "https://api.deepseek.com/beta").rstrip("/")
    model = os.getenv("MODEL_NAME") or "deepseek-flash"
    tools = [{"type": "function", "function": {
        "name": "list_transactions", "strict": True,
        "description": "只读查询当前演示用户在指定日期范围内的模拟流水。不得提交任何业务。",
        "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["start_date", "end_date"], "additionalProperties": False},
    }}]
    common = {"model": model, "thinking": {"type": "disabled"}, "max_tokens": 150}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=30.0) as client:
        first = client.post(base_url + "/chat/completions", headers=headers, json={
            **common,
            "messages": [{"role": "system", "content": "你只选择允许的只读工具，不编造流水。"},
                         {"role": "user", "content": "请查询 2026-09-01 到 2026-09-24 的模拟流水。"}],
            "tools": tools, "tool_choice": {"type": "function", "function": {"name": "list_transactions"}},
        })
        if first.status_code != 200:
            raise SystemExit(f"只读工具调用失败：HTTP {first.status_code}；不输出服务端原始响应")
        tool_calls = first.json()["choices"][0]["message"].get("tool_calls") or []
        if len(tool_calls) != 1 or tool_calls[0]["function"]["name"] != "list_transactions":
            raise SystemExit("模型没有返回预期的只读工具调用")
        args = TransactionRange.model_validate(json.loads(tool_calls[0]["function"]["arguments"]))
        if args.start_date != "2026-09-01" or args.end_date != "2026-09-24":
            raise SystemExit("模型工具参数与用户指定日期不一致")
        print("只读工具调用与严格参数校验：通过")

        second = client.post(base_url + "/chat/completions", headers=headers, json={
            **common,
            "messages": [{"role": "system", "content": "你是模拟银行助手。转账缺少金额或收款人有同名歧义时，必须追问；不要声称已执行。"},
                         {"role": "user", "content": "给张三转账。已保存收款人有两个张三，尾号分别 3821、9156。"}],
        })
        if second.status_code != 200:
            raise SystemExit(f"缺信息追问失败：HTTP {second.status_code}；不输出服务端原始响应")
        answer = second.json()["choices"][0]["message"].get("content") or ""
        if not any(marker in answer for marker in ("金额", "多少")) or not any(marker in answer for marker in ("3821", "9156", "哪个", "哪位")):
            raise SystemExit("模型未同时追问金额与同名收款人")
        print("缺金额和同名收款人追问：通过")
    print(f"DeepSeek M1 小试验完成：模型={model}，端点={base_url}；没有执行任何业务工具")


if __name__ == "__main__":
    main()
