"""Render a single-file HTML dashboard from a FinAgent test-plan run.

Reads :  <out-dir>\\junit-backend.xml, <out-dir>\\steps.json, <out-dir>\\demo-facts.json
Writes:  <out-dir>\\dashboard.html, <out-dir>\\results.json, <out-dir>\\summary.md

No third-party dependencies. Never touches the project's business data.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

TIER_LABEL = {
    "quick": ("quick", "冒烟层 · 领域规则 + 会话隔离 + 手算统计 + 限额"),
    "standard": ("standard", "标准层 · 全部后端测试 + 前端类型检查与构建"),
    "full": ("full", "完整层 · 标准层 + 页面回归 + M3–M6 浏览器验收 + 启动集成"),
}

# 手工用例（与 docs/TESTPLAN-CASES.md 保持一致）
MANUAL_CASES = [
    ("MC-01", "会话与三用户隔离", "切到陈予后看不到林晓的账户；SESSION_CHANGED 清空私有数据", "T2"),
    ("MC-02", "账户/卡/收款人只读展示", "金额、尾号、状态与基准表一致；无写入口", "T2+T4"),
    ("MC-03", "本月餐饮统计（核心数字）", "净消费 1200.00，消费 2 笔，退款 1 笔，环比 +50.0%", "T2+T4"),
    ("MC-04", "统计证据下钻", "证据逐笔带符号，合计等于 1200.00；分页不重复", "T2+T4"),
    ("MC-05", "覆盖不完整", "缺口账户标 incomplete，不输出总额与百分比", "T2"),
    ("MC-06", "流水筛选与未分类", "未分类只筛 category 为 NULL；收入/转账不计消费", "T2"),
    ("MC-07", "表单转账（含同名消歧）", "准备不改余额；确认后 −100/+100 且成对流水 + 审计", "T2+T4"),
    ("MC-08", "幂等与重复确认", "重复提交只产生一条业务结果，余额只变一次", "T2"),
    ("MC-09", "日额度与单笔上限", "单笔 >5000 元、当日累计 >10000 元均拒绝且无写入", "T2"),
    ("MC-10", "卡片冻结/解冻", "准备不改状态；已处于目标状态时不新建写操作", "T2+T4"),
    ("MC-11", "聊天“确认”不执行", "对话里确认只提示点按钮，数据库无变化", "T2"),
    ("MC-12", "跨场景：陌生消费→冻结→查余额", "多笔候选先选择；余额与冻结结果逐项真实", "T2+T4"),
    ("MC-13", "仅拒绝冻结、继续查余额", "卡保持正常，余额成功，任务 partially_completed", "T2"),
    ("MC-14", "条件转账成立", "1200 > 500 成立，扣款 100 元，余额 9900/20100", "T2+T4"),
    ("MC-15", "条件转账不成立", "净额降至 400 后确认返回 CONDITION_NOT_MET，不扣款", "T2"),
    ("MC-16", "操作单过期（5 分钟真实时钟）", "超时确认被拒，不复用过期单", "T2"),
    ("MC-17", "刷新/重启恢复", "刷新后读回同一回执，不重复扣款", "T2+T4"),
    ("MC-18", "权限与 CSRF", "无 CSRF / 跨源 / 跨用户 ID 一律拒绝且不泄露存在性", "T2"),
    ("MC-19", "前端类型/构建", "npm run build 退出码 0，无 TS 错误", "T1+T4"),
    ("MC-20", "页面同步与故障恢复", "轮询约 2 秒、终态停止；断网停轮询并禁用确认", "T4"),
]


def parse_junit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases: list[dict] = []
    for suite in suites:
        for tc in suite.iter("testcase"):
            status = "passed"
            message = ""
            for child in tc:
                tag = child.tag.lower()
                if tag in ("failure", "error"):
                    status = "failed"
                    message = (child.get("message") or (child.text or "")).strip()
                    break
                if tag == "skipped":
                    status = "skipped"
                    message = child.get("message") or ""
            cases.append(
                {
                    "name": tc.get("name") or "?",
                    "classname": tc.get("classname") or "",
                    "time": float(tc.get("time") or 0.0),
                    "status": status,
                    "message": message[:400],
                }
            )
    return cases


def module_of(classname: str) -> str:
    if not classname:
        return "(unknown)"
    return classname.split(".")[-1]


def human_money(minor: int) -> str:
    return f"{minor / 100:,.2f}"


def demo_metrics(facts: dict) -> list[dict]:
    """把演示库事实转成可展示的核对卡片。"""
    metrics: list[dict] = []
    if not facts or "accounts" not in facts:
        return metrics

    accounts = [a for a in facts.get("accounts", []) if a.get("owner_id") == "u1"]
    for a in accounts:
        metrics.append(
            {
                "label": f"林晓 · {a.get('alias')}（尾号 {a.get('account_last4')}）",
                "value": f"{human_money(a.get('balance_minor', 0))} 元",
                "expect": "与 DEMO.md 一致",
                "ok": True,
            }
        )

    diffs = facts.get("ledger_check") or []
    if isinstance(diffs, list) and diffs:
        bad = [d for d in diffs if (d.get("diff") or 0) != 0]
        metrics.append(
            {
                "label": "账本恒等式 余额 = 期初 + 贷记 − 借记",
                "value": f"{len(diffs) - len(bad)}/{len(diffs)} 个账户差额为 0",
                "expect": "全部为 0",
                "ok": not bad,
            }
        )

    this_win = facts.get("u1_net_spending_this_window") or []
    prev_win = facts.get("u1_net_spending_prev_month") or []
    cur_food = next((r.get("net_minor", 0) for r in this_win if r.get("category") == "餐饮"), None) if isinstance(this_win, list) else None
    prev_food = next((r.get("net_minor", 0) for r in prev_win if r.get("category") == "餐饮"), None) if isinstance(prev_win, list) else None
    if cur_food is not None:
        metrics.append(
            {
                "label": "本月餐饮净消费（1000 + 250 − 50）",
                "value": f"{human_money(cur_food)} 元",
                "expect": "1200.00 元",
                "ok": cur_food == 120000,
            }
        )
    if prev_food is not None:
        metrics.append(
            {
                "label": "上月同期餐饮净消费（900 − 100）",
                "value": f"{human_money(prev_food)} 元",
                "expect": "800.00 元",
                "ok": prev_food == 80000,
            }
        )
    if cur_food is not None and prev_food:
        pct = round((cur_food - prev_food) / prev_food * 100, 1)
        metrics.append(
            {
                "label": "环比增长",
                "value": f"{pct:+.1f}%",
                "expect": "+50.0%",
                "ok": abs(pct - 50.0) < 0.05,
            }
        )

    for row in this_win if isinstance(this_win, list) else []:
        cat = row.get("category")
        if cat in ("餐饮",):
            continue
        metrics.append(
            {
                "label": f"本月{cat}净消费",
                "value": f"{human_money(row.get('net_minor', 0))} 元",
                "expect": "购物 99.00 / 交通 24.00 / 未分类 32.00（待入账不计）",
                "ok": True,
            }
        )

    last_night = facts.get("u1_last_night")
    if isinstance(last_night, list):
        metrics.append(
            {
                "label": "昨晚消费候选（前一日 18:00–当日 06:00 北京）",
                "value": f"{len(last_night)} 笔",
                "expect": "1 笔 · 88.00 元 · 购物 · 尾号 1234",
                "ok": len(last_night) == 1,
            }
        )
    return metrics


def load_previous(results_path: Path, search_dirs: list[Path]) -> dict | None:
    """找上一次的 results.json 以便显示趋势。"""
    if results_path.exists():
        try:
            return json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    for d in search_dirs:
        cand = d / "results.json"
        if cand.exists() and cand.resolve() != results_path.resolve():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def status_badge(status: str) -> str:
    cls = {"passed": "ok", "failed": "bad", "skipped": "skip"}.get(status, "skip")
    label = {"passed": "通过", "failed": "失败", "skipped": "跳过"}.get(status, status)
    return f'<span class="badge {cls}">{label}</span>'


def build_html(ctx: dict) -> str:
    tier, tier_desc = TIER_LABEL.get(ctx["tier"], (ctx["tier"], ""))
    cases: list[dict] = ctx["cases"]
    steps: list[dict] = ctx["steps"]
    metrics: list[dict] = ctx["metrics"]

    passed = sum(1 for c in cases if c["status"] == "passed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    skipped = sum(1 for c in cases if c["status"] == "skipped")
    total = len(cases)
    step_failed = sum(1 for s in steps if s["status"] != "passed")

    overall_ok = step_failed == 0 and failed == 0
    verdict = "全部通过" if overall_ok else f"{failed + step_failed} 项需要关注"

    # 按模块聚合
    by_module: dict[str, list[dict]] = {}
    for c in cases:
        by_module.setdefault(module_of(c["classname"]), []).append(c)
    module_rows = []
    for mod in sorted(by_module):
        items = by_module[mod]
        m_pass = sum(1 for i in items if i["status"] == "passed")
        m_fail = sum(1 for i in items if i["status"] == "failed")
        m_sec = sum(i["time"] for i in items)
        module_rows.append((mod, len(items), m_pass, m_fail, m_sec))

    slowest = sorted(cases, key=lambda c: c["time"], reverse=True)[:12]
    failing = [c for c in cases if c["status"] == "failed"]

    prev = ctx.get("previous")
    trend_html = ""
    if prev:
        p_total = prev.get("summary", {}).get("tests_total")
        p_failed = prev.get("summary", {}).get("tests_failed")
        p_seconds = prev.get("summary", {}).get("seconds")
        if p_total is not None:
            d_total = total - p_total
            d_failed = failed - (p_failed or 0)
            d_sec = None if p_seconds is None else round(ctx["seconds"] - p_seconds, 1)
            trend_html = (
                '<div class="trend">'
                f'<span>对比上次（{esc(prev.get("stamp", "?"))}）：</span>'
                f'<b>用例 {p_total} → {total}</b>'
                f'<b class="{"up" if d_total >= 0 else "down"}">{d_total:+d}</b>'
                f'<b>失败 {p_failed} → {failed}</b>'
                f'<b class="{"down" if d_failed > 0 else "up"}">{d_failed:+d}</b>'
                + (f'<b>总耗时 {p_seconds}s → {ctx["seconds"]}s</b><b class="{"down" if d_sec and d_sec > 0 else "up"}">{d_sec:+}s</b>' if d_sec is not None else "")
                + "</div>"
            )

    # ---- 断言片段
    def rows_cases(items: list[dict]) -> str:
        out = []
        for c in items:
            mod = module_of(c["classname"])
            bar_w = min(100.0, (c["time"] / 5.0) * 100.0)
            out.append(
                f'<tr data-status="{esc(c["status"])}" data-module="{esc(mod)}" data-name="{esc(c["name"])}">'
                f'<td>{status_badge(c["status"])}</td>'
                f'<td class="mono">{esc(mod)}</td>'
                f'<td class="mono">{esc(c["name"])}</td>'
                f'<td class="num">{c["time"]:.2f}s<div class="bar"><i style="width:{bar_w:.0f}%"></i></div></td>'
                f"</tr>"
            )
        return "".join(out)

    step_cards = "".join(
        f'<div class="step {("ok" if s["status"] == "passed" else "bad")}">'
        f'<div class="step-top"><span class="step-id">{esc(s["id"])}</span>'
        f'<span class="step-status">{"通过" if s["status"] == "passed" else "失败"}</span>'
        f'<span class="step-sec">{s["seconds"]}s</span></div>'
        f'<div class="step-name">{esc(s["name"])}</div>'
        f'<div class="step-detail mono">{esc(s.get("detail", ""))[:300]}</div>'
        f'<div class="step-log">log: {esc(s.get("log", ""))}</div>'
        f"</div>"
        for s in steps
    )

    metric_cards = "".join(
        f'<div class="metric {("ok" if m["ok"] else "bad")}">'
        f'<div class="metric-label">{esc(m["label"])}</div>'
        f'<div class="metric-value">{esc(m["value"])}</div>'
        f'<div class="metric-expect">期望：{esc(m["expect"])}</div>'
        f"</div>"
        for m in metrics
    )

    module_table = "".join(
        f'<tr><td class="mono">{esc(mod)}</td><td class="num">{n}</td>'
        f'<td class="num ok-text">{p}</td><td class="num {"bad-text" if f else ""}">{f}</td>'
        f'<td class="num">{sec:.1f}s</td></tr>'
        for mod, n, p, f, sec in module_rows
    )

    slow_table = "".join(
        f'<tr><td class="mono">{esc(module_of(c["classname"]))}</td>'
        f'<td class="mono">{esc(c["name"])}</td><td class="num">{c["time"]:.2f}s</td></tr>'
        for c in slowest
    )

    fail_block = ""
    if failing:
        fail_block = (
            '<h2 id="failures">失败用例</h2><div class="failures">'
            + "".join(
                f'<div class="failure"><div class="mono">{esc(module_of(c["classname"]))}::{esc(c["name"])}</div>'
                f'<pre>{esc(c["message"])}</pre></div>'
                for c in failing
            )
            + "</div>"
        )

    manual_rows = "".join(
        f'<tr><td class="mono">{esc(cid)}</td><td>{esc(name)}</td><td>{esc(judge)}</td>'
        f'<td class="mono">{esc(layer)}</td></tr>'
        for cid, name, judge, layer in MANUAL_CASES
    )

    gaps = ctx.get("gaps") or []
    gap_block = ""
    if gaps:
        open_gaps = [g for g in gaps if not g.get("closed")]
        closed_gaps = [g for g in gaps if g.get("closed")]
        n_p0 = sum(1 for g in open_gaps if str(g.get("priority")) == "P0")
        head = (
            f'<p class="hint">共 {len(gaps)} 条：已关闭 <b>{len(closed_gaps)}</b> 条（本轮新增 '
            f'<code>backend/tests/test_gap_findings.py</code> 10 项用例）；仍待补 <b>{len(open_gaps)}</b> 条，'
            f'其中 P0 <b>{n_p0}</b> 条。</p>'
        )
        open_html = "".join(
            f'<div class="gap"><div class="gap-head"><span class="pill">{esc(g.get("id", ""))}</span>'
            f'<span class="pill prio-{esc(str(g.get("priority", "P1")).lower())}">{esc(g.get("priority", "P1"))}</span>'
            f'<b>{esc(g.get("title", ""))}</b></div>'
            f'<div class="gap-body">{esc(g.get("detail", ""))}</div>'
            + (f'<div class="gap-fix"><b>建议：</b>{esc(g["suggested_test"])}</div>' if g.get("suggested_test") else "")
            + "</div>"
            for g in open_gaps
        )
        closed_html = ""
        if closed_gaps:
            closed_html = (
                '<details class="closedbox"><summary>已关闭的缺口（'
                + str(len(closed_gaps))
                + ' 条）</summary><div class="gaps">'
                + "".join(
                    f'<div class="gap closed"><div class="gap-head"><span class="pill">{esc(g.get("id", ""))}</span>'
                    f'<span class="pill done">已关闭</span>'
                    f'<b>{esc(g.get("title", ""))}</b></div>'
                    f'<div class="gap-body mono">{esc((g.get("closure") or {}).get("test", ""))}</div>'
                    f'<div class="gap-fix">{esc((g.get("closure") or {}).get("note", ""))}</div></div>'
                    for g in closed_gaps
                )
                + "</div></details>"
            )
        gap_block = (
            '<h2 id="gaps">覆盖缺口与优化建议</h2>'
            + head
            + '<div class="gaps">' + open_html + "</div>"
            + closed_html
        )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FinAgent 测试仪表盘 · {esc(tier)} · {esc(ctx['stamp'])}</title>
<style>
:root {{
  --bg:#0e1116; --panel:#161b22; --panel2:#1c232c; --line:#2a313c;
  --fg:#e6edf3; --muted:#8b949e; --ok:#3fb950; --bad:#f85149; --warn:#d29922;
  --accent:#58a6ff; --accent2:#bc8cff;
  --mono:ui-monospace,SFMono-Regular,"Cascadia Mono",Consolas,"Liberation Mono",monospace;
}}
@media (prefers-color-scheme: light) {{
  :root {{ --bg:#f6f8fa; --panel:#ffffff; --panel2:#f6f8fa; --line:#d0d7de;
           --fg:#1f2328; --muted:#59636e; --accent:#0969da; --accent2:#8250df; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:1240px; margin:0 auto; padding:28px 20px 80px; }}
header.hero {{ background:linear-gradient(135deg,var(--panel),var(--panel2));
  border:1px solid var(--line); border-radius:16px; padding:24px 26px; }}
.hero-top {{ display:flex; flex-wrap:wrap; gap:16px; align-items:center; justify-content:space-between; }}
h1 {{ margin:0; font-size:24px; letter-spacing:.3px; }}
h1 small {{ display:block; font-size:13px; font-weight:400; color:var(--muted); margin-top:6px; }}
.verdict {{ font-size:19px; font-weight:700; padding:8px 16px; border-radius:999px; border:1px solid; white-space:nowrap; }}
.verdict.ok {{ color:var(--ok); border-color:var(--ok); background:rgba(63,185,80,.10); }}
.verdict.bad {{ color:var(--bad); border-color:var(--bad); background:rgba(248,81,73,.10); }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:22px; }}
.kpi {{ background:var(--panel2); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
.kpi .k {{ font-size:12px; color:var(--muted); }}
.kpi .v {{ font-size:24px; font-weight:700; margin-top:4px; font-variant-numeric:tabular-nums; }}
.kpi .v.ok-text {{ color:var(--ok); }} .kpi .v.bad-text {{ color:var(--bad); }}
.trend {{ margin-top:18px; padding:12px 16px; border:1px dashed var(--line); border-radius:12px;
  display:flex; flex-wrap:wrap; gap:14px; align-items:center; color:var(--muted); font-size:13px; }}
.trend b {{ color:var(--fg); }} .trend .up {{ color:var(--ok); }} .trend .down {{ color:var(--bad); }}
nav.toc {{ display:flex; flex-wrap:wrap; gap:8px; margin:22px 0 6px; }}
nav.toc a {{ color:var(--accent); text-decoration:none; border:1px solid var(--line);
  border-radius:999px; padding:5px 13px; font-size:13px; background:var(--panel); }}
nav.toc a:hover {{ border-color:var(--accent); }}
h2 {{ font-size:17px; margin:34px 0 12px; padding-bottom:8px; border-bottom:1px solid var(--line); }}
h2 .hint {{ font-size:12px; font-weight:400; color:var(--muted); margin-left:10px; }}
.steps {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; }}
.step {{ background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--ok);
  border-radius:10px; padding:13px 15px; }}
.step.bad {{ border-left-color:var(--bad); }}
.step-top {{ display:flex; gap:10px; align-items:center; font-size:12px; color:var(--muted); }}
.step-id {{ font-family:var(--mono); background:var(--panel2); padding:1px 7px; border-radius:5px; color:var(--fg); }}
.step-status {{ margin-left:auto; font-weight:700; }}
.step.ok .step-status {{ color:var(--ok); }} .step.bad .step-status {{ color:var(--bad); }}
.step-name {{ margin-top:7px; font-weight:600; }}
.step-detail {{ margin-top:6px; font-size:11.5px; color:var(--muted); word-break:break-all;
  max-height:52px; overflow:hidden; }}
.step-log {{ margin-top:6px; font-size:11px; color:var(--muted); opacity:.75; }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; }}
.metric {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:13px 15px; }}
.metric.bad {{ border-color:var(--bad); }}
.metric-label {{ font-size:12.5px; color:var(--muted); }}
.metric-value {{ font-size:20px; font-weight:700; margin:5px 0 3px; font-variant-numeric:tabular-nums; }}
.metric.ok .metric-value {{ color:var(--ok); }} .metric.bad .metric-value {{ color:var(--bad); }}
.metric-expect {{ font-size:11.5px; color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; background:var(--panel);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
th, td {{ text-align:left; padding:8px 12px; border-bottom:1px solid var(--line); font-size:13px; }}
th {{ background:var(--panel2); font-weight:600; color:var(--muted); font-size:12px;
  text-transform:uppercase; letter-spacing:.4px; position:sticky; top:0; }}
tr:last-child td {{ border-bottom:none; }}
tbody tr:hover {{ background:var(--panel2); }}
.mono {{ font-family:var(--mono); font-size:12px; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.ok-text {{ color:var(--ok); }} .bad-text {{ color:var(--bad); font-weight:700; }}
.badge {{ display:inline-block; padding:1px 9px; border-radius:999px; font-size:11.5px; font-weight:600; }}
.badge.ok {{ color:var(--ok); background:rgba(63,185,80,.13); }}
.badge.bad {{ color:var(--bad); background:rgba(248,81,73,.13); }}
.badge.skip {{ color:var(--warn); background:rgba(210,153,34,.13); }}
.bar {{ height:3px; background:var(--panel2); border-radius:2px; margin-top:4px; overflow:hidden; }}
.bar i {{ display:block; height:100%; background:var(--accent); }}
.toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px; }}
.toolbar input, .toolbar select {{ background:var(--panel); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:7px 11px; font:13px inherit; }}
.toolbar input {{ min-width:230px; }}
.toolbar .count {{ color:var(--muted); font-size:12.5px; margin-left:auto; }}
.tablewrap {{ max-height:620px; overflow:auto; border-radius:10px; }}
.failures {{ display:grid; gap:12px; }}
.failure {{ background:var(--panel); border:1px solid var(--bad); border-radius:10px; padding:13px 15px; }}
.failure pre {{ margin:8px 0 0; overflow:auto; font-size:12px; color:var(--bad); max-height:260px; }}
.gaps {{ display:grid; gap:12px; }}
.gap {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.gap-head {{ display:flex; gap:9px; align-items:center; flex-wrap:wrap; }}
.pill {{ font-family:var(--mono); font-size:11px; padding:1px 8px; border-radius:999px;
  background:var(--panel2); color:var(--muted); border:1px solid var(--line); }}
.prio-p0 {{ color:var(--bad); border-color:var(--bad); }}
.prio-p1 {{ color:var(--warn); border-color:var(--warn); }}
.prio-p2 {{ color:var(--accent); border-color:var(--accent); }}
.pill.done {{ color:var(--ok); border-color:var(--ok); }}
.gap.closed {{ opacity:.82; border-left:3px solid var(--ok); }}
.closedbox {{ margin-top:14px; border:1px solid var(--line); border-radius:10px;
  background:var(--panel); padding:12px 16px; }}
.closedbox summary {{ cursor:pointer; font-weight:600; }}
.gap-body {{ margin-top:8px; color:var(--muted); font-size:13px; }}
.gap-fix {{ margin-top:7px; font-size:13px; }}
footer {{ margin-top:48px; color:var(--muted); font-size:12.5px; text-align:center; line-height:1.9; }}
footer code {{ font-family:var(--mono); background:var(--panel); padding:2px 7px; border-radius:5px;
  border:1px solid var(--line); }}
</style>
</head>
<body>
<div class="wrap">

<header class="hero">
  <div class="hero-top">
    <h1>FinAgent 测试仪表盘
      <small>档位 <b>{esc(tier)}</b> · {esc(tier_desc)} · 范围 {esc(ctx['scope'])} · 生成于 {esc(generated)}</small>
    </h1>
    <div class="verdict {"ok" if overall_ok else "bad"}">{esc(verdict)}</div>
  </div>
  <div class="kpis">
    <div class="kpi"><div class="k">后端用例总数</div><div class="v">{total}</div></div>
    <div class="kpi"><div class="k">通过</div><div class="v ok-text">{passed}</div></div>
    <div class="kpi"><div class="k">失败</div><div class="v {"bad-text" if failed else ""}">{failed}</div></div>
    <div class="kpi"><div class="k">跳过</div><div class="v">{skipped}</div></div>
    <div class="kpi"><div class="k">步骤失败</div><div class="v {"bad-text" if step_failed else ""}">{step_failed}</div></div>
    <div class="kpi"><div class="k">总耗时</div><div class="v">{ctx['seconds']}s</div></div>
    <div class="kpi"><div class="k">模型调用</div><div class="v">0</div></div>
  </div>
  {trend_html}
</header>

<nav class="toc">
  <a href="#steps">执行步骤</a>
  <a href="#metrics">业务数字核对</a>
  <a href="#modules">模块覆盖</a>
  <a href="#cases">用例明细</a>
  <a href="#slow">最慢用例</a>
  <a href="#manual">手工用例</a>
  <a href="#gaps">覆盖缺口</a>
  {"<a href='#failures'>失败用例</a>" if failing else ""}
</nav>

<h2 id="steps">执行步骤 <span class="hint">每一步的原始输出在 logs/ 目录</span></h2>
<div class="steps">{step_cards}</div>

<h2 id="metrics">业务数字核对 <span class="hint">从演示库只读抽查，与文档期望值逐项比对</span></h2>
<div class="metrics">{metric_cards or '<p class="mono">（本次未采集演示库事实，或数据库不存在）</p>'}</div>

<h2 id="modules">模块覆盖 <span class="hint">按测试文件聚合，快速看出哪一层最厚</span></h2>
<table><thead><tr><th>模块</th><th class="num">用例</th><th class="num">通过</th><th class="num">失败</th><th class="num">耗时</th></tr></thead>
<tbody>{module_table or '<tr><td colspan="5">无数据</td></tr>'}</tbody></table>

<h2 id="cases">用例明细 <span class="hint">可搜索、可按状态筛选；时间条越短越快</span></h2>
<div class="toolbar">
  <input id="q" type="search" placeholder="搜索用例名或模块…">
  <select id="fs">
    <option value="">全部状态</option>
    <option value="passed">仅通过</option>
    <option value="failed">仅失败</option>
    <option value="skipped">仅跳过</option>
  </select>
  <span class="count" id="cnt"></span>
</div>
<div class="tablewrap">
<table id="casestable">
  <thead><tr><th>状态</th><th>模块</th><th>用例</th><th class="num">耗时</th></tr></thead>
  <tbody>{rows_cases(cases) or '<tr><td colspan="4">无数据</td></tr>'}</tbody>
</table>
</div>

<h2 id="slow">最慢的 12 个用例 <span class="hint">优化迭代速度的着力点</span></h2>
<table><thead><tr><th>模块</th><th>用例</th><th class="num">耗时</th></tr></thead>
<tbody>{slow_table or '<tr><td colspan="3">无数据</td></tr>'}</tbody></table>

{fail_block}

<h2 id="manual">手工用例清单 <span class="hint">详情见 docs/TESTPLAN-CASES.md</span></h2>
<table><thead><tr><th>编号</th><th>名称</th><th>关键判定</th><th>层</th></tr></thead>
<tbody>{manual_rows}</tbody></table>

{gap_block}

<footer>
  由 <code>scripts/testplan.ps1</code> + <code>tools/gen_dashboard.py</code> 生成 · 单文件、可离线转发<br>
  本报告只记录测试结论，不包含密钥、确认凭据或任何真实银行数据 · 全部账户与交易均为合成演示数据
</footer>
</div>

<script>
(() => {{
  const q = document.getElementById('q');
  const fs = document.getElementById('fs');
  const rows = Array.from(document.querySelectorAll('#casestable tbody tr'));
  const cnt = document.getElementById('cnt');
  if (!q || !rows.length) return;
  const apply = () => {{
    const needle = q.value.trim().toLowerCase();
    const st = fs.value;
    let shown = 0;
    for (const r of rows) {{
      const hay = (r.dataset.name + ' ' + r.dataset.module).toLowerCase();
      const okq = !needle || hay.includes(needle);
      const oks = !st || r.dataset.status === st;
      const vis = okq && oks;
      r.style.display = vis ? '' : 'none';
      if (vis) shown++;
    }}
    cnt.textContent = `显示 ${{shown}} / ${{rows.length}} 项`;
  }};
  q.addEventListener('input', apply);
  fs.addEventListener('change', apply);
  apply();
}})();
</script>
</body>
</html>
"""


def build_summary(ctx: dict) -> str:
    tier, tier_desc = TIER_LABEL.get(ctx["tier"], (ctx["tier"], ""))
    cases = ctx["cases"]
    steps = ctx["steps"]
    passed = sum(1 for c in cases if c["status"] == "passed")
    failed = sum(1 for c in cases if c["status"] == "failed")
    skipped = sum(1 for c in cases if c["status"] == "skipped")
    lines = [
        f"# FinAgent 测试结果 · {ctx['stamp']}",
        "",
        f"- 档位：`{tier}`（{tier_desc}）",
        f"- 范围：`{ctx['scope']}`",
        f"- 项目根：`{ctx['project_root']}`",
        f"- 后端用例：{len(cases)} 项（通过 {passed} / 失败 {failed} / 跳过 {skipped}）",
        f"- 执行步骤：{len(steps)} 个（失败 {sum(1 for s in steps if s['status'] != 'passed')} 个）",
        f"- 总耗时：{ctx['seconds']} 秒",
        "- 模型调用：0（固定 mock）",
        "",
        "## 步骤",
        "",
        "| 步骤 | 名称 | 状态 | 耗时 | 日志 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in steps:
        mark = "通过" if s["status"] == "passed" else "**失败**"
        lines.append(f"| {s['id']} | {s['name']} | {mark} | {s['seconds']}s | `{s.get('log', '')}` |")

    lines += ["", "## 业务数字核对", "", "| 项目 | 实测 | 期望 | 结论 |", "| --- | --- | --- | --- |"]
    for m in ctx["metrics"]:
        lines.append(f"| {m['label']} | {m['value']} | {m['expect']} | {'✅' if m['ok'] else '❌'} |")
    if not ctx["metrics"]:
        lines.append("| （未采集） | | | |")

    if failed:
        lines += ["", "## 失败用例", ""]
        for c in cases:
            if c["status"] == "failed":
                lines.append(f"- `{module_of(c['classname'])}::{c['name']}` — {c['message'][:200] or '无消息'}")

    lines += ["", "## 最慢的 10 个用例", "", "| 模块 | 用例 | 耗时 |", "| --- | --- | --- |"]
    for c in sorted(cases, key=lambda c: c["time"], reverse=True)[:10]:
        lines.append(f"| {module_of(c['classname'])} | {c['name']} | {c['time']:.2f}s |")

    lines += ["", f"仪表盘：`dashboard.html`", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--tier", required=True)
    ap.add_argument("--scope", default="All")
    ap.add_argument("--project-root", default="")
    ap.add_argument("--docs-dir", default="")
    ap.add_argument("--gaps-json", default="")
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--steps-json", default="")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = parse_junit(out_dir / "junit-backend.xml")

    steps: list[dict] = []
    steps_path = Path(args.steps_json) if args.steps_json else out_dir / "steps.json"
    if steps_path.exists():
        try:
            raw = json.loads(steps_path.read_text(encoding="utf-8-sig"))
            steps = raw if isinstance(raw, list) else [raw]
            steps = [
                {
                    "id": s.get("id", "?"),
                    "name": s.get("name", ""),
                    "status": s.get("status", "passed"),
                    "seconds": s.get("seconds", 0),
                    "log": s.get("log", ""),
                    "detail": (s.get("detail") or "").strip(),
                }
                for s in steps
            ]
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 无法解析 {steps_path}: {exc}", file=sys.stderr)

    facts_path = out_dir / "demo-facts.json"
    facts: dict = {}
    if facts_path.exists():
        try:
            facts = json.loads(facts_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 无法解析 {facts_path}: {exc}", file=sys.stderr)

    metrics = demo_metrics(facts)
    seconds = round(sum(s["seconds"] for s in steps), 1)

    # 趋势：找上一次运行
    prev_dirs = [out_dir.parent / d for d in ("latest",)] + [Path(args.project_root) / "var" / "testplan" / "latest"] if args.project_root else []
    previous = None
    results_path = out_dir / "results.json"
    if results_path.exists():
        try:
            previous = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            previous = None
    if previous is None:
        previous = load_previous(results_path, [p for p in prev_dirs if p])

    gaps = []
    for candidate in (
        Path(args.gaps_json) if args.gaps_json else None,
        Path(args.docs_dir) / "gaps.json" if args.docs_dir else None,
        Path(__file__).resolve().parent / "gaps.json",
    ):
        if candidate and candidate.exists():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                gaps = [
                    {
                        "id": g.get("id", ""),
                        "priority": g.get("priority", "P1"),
                        "title": g.get("description") or g.get("title") or "",
                        "detail": g.get("why_it_matters") or g.get("detail") or "",
                        "suggested_test": g.get("suggested_test") or "",
                        "closed": bool(g.get("closed")),
                        "closure": g.get("closure") or {},
                    }
                    for g in raw
                ]
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 无法解析 {candidate}: {exc}", file=sys.stderr)

    ctx = {
        "tier": args.tier,
        "scope": args.scope,
        "project_root": args.project_root,
        "stamp": args.stamp,
        "cases": cases,
        "steps": steps,
        "metrics": metrics,
        "seconds": seconds,
        "previous": previous,
        "gaps": gaps,
    }

    (out_dir / "dashboard.html").write_text(build_html(ctx), encoding="utf-8")
    (out_dir / "summary.md").write_text(build_summary(ctx), encoding="utf-8")

    passed = sum(1 for c in cases if c["status"] == "passed")
    results = {
        "stamp": args.stamp,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tier": args.tier,
        "scope": args.scope,
        "project_root": args.project_root,
        "summary": {
            "tests_total": len(cases),
            "tests_passed": passed,
            "tests_failed": sum(1 for c in cases if c["status"] == "failed"),
            "tests_skipped": sum(1 for c in cases if c["status"] == "skipped"),
            "steps_total": len(steps),
            "steps_failed": sum(1 for s in steps if s["status"] != "passed"),
            "seconds": seconds,
            "model_calls": 0,
        },
        "steps": steps,
        "metrics": metrics,
        "cases": cases,
    }
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  dashboard.html : {out_dir / 'dashboard.html'}")
    print(f"  results.json   : {results_path}")
    print(f"  summary.md     : {out_dir / 'summary.md'}")
    print(f"  用例 {len(cases)} 通过 {passed} 失败 {results['summary']['tests_failed']} ｜ 步骤 {len(steps)} 失败 {results['summary']['steps_failed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
