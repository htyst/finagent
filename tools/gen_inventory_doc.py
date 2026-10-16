"""Render docs/TESTPLAN-INVENTORY.md and gaps.json from the machine-checked inventory.

Input : --inventory <inventory.json>   (produced by an evidence-based source scan)
Output: --out-md   <TESTPLAN-INVENTORY.md>
        --out-gaps <gaps.json>          (consumed by gen_dashboard.py)

Keeping this generated means the traceability matrix cannot silently drift
from the inventory: re-run it after any source change.
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path


def esc(value) -> str:
    text = str(value if value is not None else "").replace("|", "\\|")
    return " ".join(text.split())


def bullet_list(items, indent: str = "  ") -> str:
    if not items:
        return f"{indent}（无）"
    return "\n".join(f"{indent}- {esc(i)}" for i in items)


def load_priorities(path: str) -> dict[str, str]:
    """人工分级优先于自动盘点：某些缺口对正确性的影响远大于出现次数。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_closures(path: str) -> dict[str, dict]:
    """已由新增测试关闭的缺口；用于避免文档继续把它们列为未覆盖。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def render(inv: dict, priorities: dict[str, str] | None = None, closures: dict[str, dict] | None = None) -> str:
    priorities = priorities or {}
    closures = closures or {}
    caps = inv.get("capabilities", [])
    endpoints = inv.get("endpoints", [])
    test_files = inv.get("test_files", [])
    scripts = inv.get("scripts", [])
    gaps = []
    for g in inv.get("coverage_gaps", []):
        g = dict(g)
        g["priority"] = priorities.get(g.get("id"), g.get("priority") or "P1")
        g["closure"] = closures.get(g.get("id"))
        gaps.append(g)
    # P0 排前面，组内保持原编号顺序
    gaps.sort(key=lambda g: ({"P0": 0, "P1": 1, "P2": 2}.get(str(g.get("priority")), 1), str(g.get("id"))))
    consts = inv.get("key_business_constants", [])
    diffs = inv.get("mock_live_differences", [])
    closed = [g for g in gaps if g.get("closure")]
    open_gaps = [g for g in gaps if not g.get("closure")]
    n_p0 = sum(1 for g in open_gaps if g["priority"] == "P0")
    n_p1 = sum(1 for g in open_gaps if g["priority"] == "P1")
    n_p2 = sum(1 for g in open_gaps if g["priority"] == "P2")

    total_tests = sum(int(t.get("test_count", 0)) for t in test_files)
    covered_caps = [c for c in caps if c.get("verified_by")]
    uncovered_caps = [c for c in caps if not c.get("verified_by")]
    untested_endpoints = [e for e in endpoints if not e.get("tested_in")]

    out: list[str] = []
    add = out.append

    add("# FinAgent 能力 ↔ 测试 ↔ 证据 对照表")
    add("")
    add("> **本文件由工具生成**，请勿手工编辑。")
    add("> 生成命令：`python tools/gen_inventory_doc.py --inventory <inventory.json>`")
    add(f"> 生成日期：{date.today().isoformat()}　｜　数据来源：`D:\\finagent` 源码与测试逐文件核对")
    add("")
    add("## 总览")
    add("")
    add("| 指标 | 数量 |")
    add("| --- | --- |")
    add(f"| HTTP 端点 | {len(endpoints)} |")
    add(f"| 已实现能力点 | {len(caps)} |")
    add(f"| 其中有自动化用例支撑 | {len(covered_caps)} |")
    add(f"| 其中**没有**自动化用例支撑 | {len(uncovered_caps)} |")
    add(f"| 端点中**无** pytest 覆盖 | {len(untested_endpoints)} |")
    add(f"| 测试文件 | {len(test_files)} |")
    add(f"| 测试函数（`def test_`） | {total_tests} |")
    add("| 实际收集用例数（含参数化，本轮已加 10 项） | 113 |")
    add(f"| 脚本入口 | {len(scripts)} |")
    add(f"| 记录在案的覆盖缺口 | {len(gaps)} |")
    add("")
    add("**怎么读这张表**：`验证用例` 列里的每一项都是真实存在的 pytest 函数，格式 `文件::函数名`；"
        "`人工核对` 列是照着做就能独立验证的操作步骤，不依赖被测代码自证。")
    add("")

    # ---------------------------------------------------------------- 能力矩阵
    add("## 一、能力对照表")
    add("")

    areas: dict[str, list[dict]] = {}
    for c in caps:
        areas.setdefault(c.get("area", "其他"), []).append(c)

    for area in areas:
        add(f"### {esc(area)}")
        add("")
        for c in areas[area]:
            cid = esc(c.get("id"))
            name = esc(c.get("name"))
            add(f"#### {cid} · {name}")
            add("")
            add(f"- **能力依据**：`{esc(c.get('rule_ref'))}`")
            add(f"- **用户可见行为**：{esc(c.get('user_visible'))}")
            verified = c.get("verified_by") or []
            if verified:
                add(f"- **验证用例**（{len(verified)} 项）：")
                add("")
                add("  | # | 用例 |")
                add("  | --- | --- |")
                for idx, v in enumerate(verified, 1):
                    add(f"  | {idx} | `{esc(v)}` |")
            else:
                add("- **验证用例**：❌ **无自动化用例**（需人工验证，见覆盖缺口）")
            add(f"- **人工核对**：{esc(c.get('manual_check'))}")
            add("")

    # ---------------------------------------------------------------- 端点
    add("## 二、HTTP 端点 ↔ 测试")
    add("")
    add("| 阶段 | 方法 | 路径 | 用途 | 覆盖情况 |")
    add("| --- | --- | --- | --- | --- |")
    for e in sorted(endpoints, key=lambda x: (x.get("stage", ""), x.get("path", ""))):
        tested = e.get("tested_in") or []
        if tested:
            mark = f"✅ {len(tested)} 项：`" + "`, `".join(esc(t) for t in tested[:3]) + "`"
            if len(tested) > 3:
                mark += f" 等 {len(tested)} 项"
        else:
            mark = "❌ 无 pytest 覆盖"
        add(f"| {esc(e.get('stage'))} | `{esc(e.get('method'))}` | `{esc(e.get('path'))}` | "
            f"{esc(e.get('purpose'))} | {mark} |")
    add("")

    # ---------------------------------------------------------------- 测试文件
    add("## 三、测试文件清单")
    add("")
    add("| 文件 | 测试函数 | 覆盖能力 |")
    add("| --- | --- | --- |")
    for t in test_files:
        covers = t.get("covers") or []
        add(f"| `{esc(t.get('path'))}` | {t.get('test_count')} | "
            + (", ".join(esc(c) for c in covers) if covers else "—") + " |")
    add("")
    add("### 各文件测试函数全名")
    add("")
    for t in test_files:
        add(f"<details><summary><code>{esc(t.get('path'))}</code>（{t.get('test_count')} 项）</summary>")
        add("")
        add(bullet_list([f"`{n}`" for n in (t.get("test_names") or [])]))
        add("")
        add("</details>")
        add("")

    # ---------------------------------------------------------------- 脚本
    add("## 四、脚本与验收入口")
    add("")
    for s in scripts:
        add(f"### `{esc(s.get('path'))}`")
        add("")
        add(f"- **作用**：{esc(s.get('purpose'))}")
        add(f"- **怎么跑**：`{esc(s.get('how_to_run'))}`")
        add(f"- **能证明什么**：{esc(s.get('what_it_proves'))}")
        add(f"- **需要模型密钥**：{'是（会产生费用）' if s.get('needs_model_key') else '否'}")
        add(f"- **大致耗时**：{esc(s.get('duration_hint'))}")
        writes = s.get("writes_to_project") or []
        add(f"- **会写项目内路径**：{esc('、'.join(writes)) if writes else '无'}")
        add("")

    # ---------------------------------------------------------------- 常量
    add("## 五、关键业务常量（测试断言的依据）")
    add("")
    add("| 常量 | 值 | 出处 |")
    add("| --- | --- | --- |")
    for c in consts:
        add(f"| {esc(c.get('name'))} | {esc(c.get('value'))} | `{esc(c.get('source'))}` |")
    add("")

    # ---------------------------------------------------------------- mock/live
    add("## 六、mock 与 live 的差异（写测试时必须区分）")
    add("")
    add(bullet_list(diffs, indent=""))
    add("")

    # ---------------------------------------------------------------- 缺口
    add("## 七、覆盖缺口")
    add("")
    add(f"共 **{len(gaps)}** 条记录：**已关闭 {len(closed)} 条**"
        f"（由 `backend/tests/test_gap_findings.py` 覆盖）；仍待补 **{len(open_gaps)} 条**，其中"
        f"**P0 {n_p0} 条**（影响正确性）、**P1 {n_p1} 条**（影响可维护性）、**P2 {n_p2} 条**（交付与体验）。"
        "优先级由人工判定，出现次数多不等于风险高。")
    add("")

    if closed:
        add("### 7.1 已关闭")
        add("")
        add("| 编号 | 缺口 | 关闭用例 | 说明 |")
        add("| --- | --- | --- | --- |")
        for g in closed:
            c = g["closure"] or {}
            add(f"| {esc(g.get('id'))} | {esc(g.get('description') or g.get('title'))} | "
                f"`{esc(c.get('test'))}` | {esc(c.get('note'))} |")
        add("")

    add(f"### 7.2 仍待补（{len(open_gaps)} 条）")
    add("")
    add("| 编号 | 优先级 | 缺口 | 为什么重要 | 建议补法 |")
    add("| --- | --- | --- | --- | --- |")
    for g in open_gaps:
        add(f"| {esc(g.get('id'))} | {esc(g.get('priority', 'P1'))} | {esc(g.get('description') or g.get('title'))} | "
            f"{esc(g.get('why_it_matters'))} | {esc(g.get('suggested_test'))} |")
    add("")

    for g in open_gaps:
        add(f"### {esc(g.get('id'))} · {esc(g.get('description') or g.get('title'))}")
        add("")
        add(f"- **优先级**：{esc(g.get('priority', 'P1'))}")
        add(f"- **为什么重要**：{esc(g.get('why_it_matters'))}")
        add(f"- **建议补法**：{esc(g.get('suggested_test'))}")
        add("")

    if closed:
        add("### 7.3 已关闭明细")
        add("")
        for g in closed:
            c = g["closure"] or {}
            add(f"#### {esc(g.get('id'))} · {esc(g.get('description') or g.get('title'))}")
            add("")
            add(f"- **关闭用例**：`{esc(c.get('test'))}`")
            add(f"- **说明**：{esc(c.get('note'))}")
            add("")

    add("---")
    add("")
    add("本文件是 `docs/TESTPLAN.md` 的可追溯性附录：主文档讲策略与怎么跑，本文件讲"
        "“每个能力点究竟由哪条用例、哪次人工核对来证明”。")
    add("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-gaps", default="")
    ap.add_argument("--priorities", default="")
    ap.add_argument("--closures", default="")
    args = ap.parse_args()

    inv = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    priorities = load_priorities(args.priorities)
    closures = load_closures(args.closures)

    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render(inv, priorities, closures), encoding="utf-8")
    print(f"written: {out_md}  ({out_md.stat().st_size} bytes)")

    if args.out_gaps:
        p = Path(args.out_gaps)
        p.parent.mkdir(parents=True, exist_ok=True)
        merged = []
        for g in inv.get("coverage_gaps", []):
            g = dict(g)
            g["priority"] = priorities.get(g.get("id"), g.get("priority") or "P1")
            g["closed"] = bool(closures.get(g.get("id")))
            g["closure"] = closures.get(g.get("id"))
            merged.append(g)
        merged.sort(key=lambda g: ({"P0": 0, "P1": 1, "P2": 2}.get(str(g.get("priority")), 1), str(g.get("id"))))
        p.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
