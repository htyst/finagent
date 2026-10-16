"""Isolated, reproducible M7 task-set evaluation. No confirmation is issued."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from time import perf_counter
from statistics import median
from app.agent.telemetry import collect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.agent.provider import MAX_TOOL_REQUESTS, MODEL_RETRIES, MODEL_TIMEOUT_SECONDS, PROMPT_SHA256
from app.agent.service import create, send
from app.agent.eval_cases import load_cases
from sqlalchemy import select, func
from app.config import ROOT
from app.data.db import make_engine
from app.data.models import Base, DemoSession, BusinessResult
from app.data.seed import SEED_VERSION, reset_demo
from app.domain.spending import RULES_VERSION

def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["mock", "live"], required=True)
    parser.add_argument("--split", choices=["dev", "holdout"], required=True)
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--suite", choices=["full", "key"], default="full")
    args = parser.parse_args()
    path = ROOT / "evals" / f"tasks.{args.split}.jsonl"
    cases = load_cases(args.split)
    if args.suite == "key":
        if args.split != "dev":
            parser.error("key suite uses dev only; holdout stays untouched")
        key_ids = {"S01", "S05", "R04", "R07", "R08", "R09", "C02", "A01", "B01", "B02"}
        cases = [case for case in cases if case["id"] in key_ids]
        assert len(cases) == 10
    cases = [dict(case, run=run) for run in range(1, args.repeat + 1) for case in cases]
    if args.mode == "live" and not os.getenv("MODEL_API_KEY"):
        raise SystemExit("live 评估需要本地 MODEL_API_KEY")
    results = []
    output_dir = ROOT / "var" / "evals"
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="m7-eval-", dir=output_dir) as temp:
        for case in cases:
            engine = make_engine(Path(temp) / f"{case['id']}-{case['run']}.sqlite3")
            started = perf_counter()
            attempts = []
            try:
                Base.metadata.create_all(engine)
                with Session(engine) as db:
                    assert case["initial_state"]["seed_version"] == SEED_VERSION
                    dataset = reset_demo(db, date.fromisoformat(case["initial_state"]["demo_date"]))
                    session = DemoSession(id=uuid4().hex, owner_id=case["user"],
                        dataset_version=dataset, csrf_token="eval-only",
                        expires_at_utc=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat())
                    db.add(session)
                    db.commit()
                    with collect() as attempts:
                        started = perf_counter()
                        task = create(db, session, args.mode, case["messages"][0])
                        for message in case["messages"][1:]:
                            task = send(db, session, task["id"], message)
                    active_steps = [step for step in task["steps"] if step["status"] not in {"pending", "skipped", "cancelled"}]
                    last_tool = active_steps[-1]["tool_name"] if active_steps else None
                    reply = task["messages"][-1]["content"]
                    checks = {"status": task["status"] == case["expected_status"],
                              "tool": last_tool == case.get("tool"),
                              "reply": case["contains"] in reply}
                    checks["no_business_write"] = db.scalar(select(func.count()).select_from(BusinessResult)) == case["expected_business_results"]
                    if "intent" in case:
                        checks["intent"] = task["intent"] == case["intent"]
                    if "step_statuses" in case:
                        checks["steps"] = [step["status"] for step in task["steps"]] == case["step_statuses"]
                    proposal = task.get("proposal")
                    if "amount_minor" in case:
                        checks["amount"] = proposal is not None and proposal["summary"].get("amount_minor") == case["amount_minor"]
                    if "target_mask" in case:
                        checks["target"] = proposal is not None and proposal["summary"].get("target", {}).get("mask") == case["target_mask"]
                    results.append({"id": case["id"], "passed": all(checks.values()), "checks": checks,
                                    "status": task["status"], "tool": last_tool,
                                    "error_code": (reply.rsplit("（",1)[-1].split("）")[0] if "（" in reply and "）" in reply else None)})
            except Exception as error:
                results.append({"id": case["id"], "passed": False, "checks": {},
                                "status": "exception", "tool": None, "error_code": type(error).__name__})
            finally:
                results[-1].update(run=case["run"], seconds=round(perf_counter()-started, 6),
                                   model_attempts=attempts, model_calls=len(attempts))
                engine.dispose()
            print(f"{case['id']}: {'PASS' if results[-1]['passed'] else 'FAIL'}", flush=True)
    try:
        version = subprocess.check_output(["git", "-c", f"safe.directory={ROOT.as_posix()}",
                                           "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        version = "unknown"
    digest = hashlib.sha256()
    for source in (ROOT / "backend/app/agent/service.py", ROOT / "backend/app/agent/registry.py",
                   ROOT / "backend/app/agent/provider.py", ROOT / "backend/app/agent/language.py", ROOT / "backend/app/agent/telemetry.py",
                   ROOT / "backend/app/agent/evaluate.py", ROOT / "backend/app/main.py",
                   ROOT / "backend/app/agent/crossflow.py", ROOT / "backend/app/agent/plans.py",
                   ROOT / "backend/app/domain/conditions.py", ROOT / "backend/app/domain/operations.py", path):
        digest.update(source.relative_to(ROOT).as_posix().encode())
        digest.update(source.read_bytes())
    from app.agent.language import LANGUAGE_PROMPT
    report = {"conversation_prompt_sha256": hashlib.sha256(LANGUAGE_PROMPT.encode()).hexdigest(), "source_sha256": digest.hexdigest(), "mode": args.mode, "split": args.split, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "code_version": version, "seed_version": SEED_VERSION, "rules_version": RULES_VERSION,
              "model_provider": os.getenv("MODEL_PROVIDER", "deepseek") if args.mode == "live" else "mock",
              "model_name": (os.getenv("MODEL_NAME") or "deepseek-flash") if args.mode == "live" else "mock",
              "prompt_sha256": PROMPT_SHA256, "tool_budget": MAX_TOOL_REQUESTS,
              "model_timeout_seconds": MODEL_TIMEOUT_SECONDS, "model_retries": MODEL_RETRIES,
              "total": len(results), "passed": sum(item["passed"] for item in results), "results": results}
    durations = [item["seconds"] for item in results]
    attempts = [attempt for item in results for attempt in item["model_attempts"]]
    report.update(suite=args.suite, repeat=args.repeat, metrics={
        "seconds": {"median": median(durations), "min": min(durations), "max": max(durations)},
        "model_calls": len(attempts), "attempts_without_usage": sum(a["usage"] is None for a in attempts),
        "reported_tokens": {key: sum((a["usage"] or {}).get(key, 0) for a in attempts)
                            for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        "actual_cost": None, "confirmation_wait_seconds": None,
        "scope": "conversation and preparation; no confirmation; durations exclude DB seeding"})
    outfile = output_dir / f"m7-{args.mode}-{args.split}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    outfile.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果：{report['passed']}/{report['total']}；报告：{outfile}", flush=True)
    return 0 if report["passed"] == report["total"] else 1

if __name__ == "__main__":
    raise SystemExit(run())
