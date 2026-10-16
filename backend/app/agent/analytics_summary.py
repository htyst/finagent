"""Only server-computed ledger facts are turned into financial prose."""
from datetime import timedelta
from app.domain.clock import parse_day


def range_label(period):
    end = parse_day(period["end_exclusive_date"]) - timedelta(days=1)
    return f"{period['start_date']} 至 {end.isoformat()}"


def summary(name, result):
    if name == "inspect_anomalies":
        rules = result["rules"]
        large = rules["large_amount"]
        text = f"规则筛查 {range_label(result['period'])} 共 {result['scanned_count']} 笔已入账消费，{result['candidate_count']} 笔待核实，{result['unflagged_count']} 笔未标记。\n"
        text += f"大额规则：至少5笔样本；金额至少1000元且达到本范围中位数的10倍。当前中位数 {large['median_minor']/100:,.2f} 元，阈值 {large['threshold_minor']/100:,.2f} 元。重复规则：同账户、同商户、同金额，间隔不超过120秒。\n"
        for row in result["candidates"]:
            text += f"• {row['merchant']} {row['amount_minor']/100:,.2f} 元，{row['posted_at_local']}；" + "；".join(item["reason"] for item in row["reasons"]) + "。\n"
        if result["coverage"]["status"] != "complete":
            text += "本范围数据不完整，筛查仅针对已知流水。\n"
        return text + result["conclusion"]
    current = result["current"]
    totals = current["totals"]
    known = "" if totals["is_complete"] else "已知"
    text = (result.get("title", "消费统计") + f"：{range_label(current['period'])}（北京时间），本人所选账户，已入账消费减退款；收入和本人转账排除。\n"
            f"{known}消费 {totals['purchase_minor']/100:,.2f} 元，退款 {totals['refund_minor']/100:,.2f} 元，{known}净消费 {totals['net_minor']/100:,.2f} 元。\n")
    text += "\n".join(f"• {row['category']}：消费 {row['purchase_minor']/100:,.2f}，退款 {row['refund_minor']/100:,.2f}，净消费 {row['net_minor']/100:,.2f} 元。" for row in current["categories"])
    if current["categories"]:
        top = max(current["categories"], key=lambda row: row["net_minor"])
        text += f"\n主要支出：{top['category']}净消费 {top['net_minor']/100:,.2f} 元；逐笔依据见下方报告。"
    if not totals["is_complete"]:
        text += "\n数据不完整，已知金额不能当作完整期间总额。"
    if result.get("months"):
        if result["missing_months"]:
            text += "\n已完整覆盖月份：" + "、".join(result["complete_months"])
            text += "；缺失或不完整月份：" + "、".join(result["missing_months"]) + "。缺失月份不是零消费，无法确定全年净消费。"
        else:
            text += f"\n全年净消费 {totals['net_minor']/100:,.2f} 元，12个月数据完整。"
        highest = result["highest_months"]
        if highest:
            scope = "全年" if result["ranking_scope"] == "full_year" else "已完整覆盖月份中"
            text += f"\n{scope}净消费最高（所有并列）：" + "、".join(row["month"] for row in highest) + f"，每月 {highest[0]['net_minor']/100:,.2f} 元。"
    comparison = result.get("comparison")
    if comparison:
        period = comparison["period"]
        days = (parse_day(current["period"]["end_exclusive_date"]) - parse_day(current["period"]["start_date"])).days
        base_days = (parse_day(period["end_exclusive_date"]) - parse_day(period["start_date"])).days
        text += f"\n比较基准：{range_label(period)}，净消费 {comparison['totals']['net_minor']/100:,.2f} 元；当前区间 {days} 天，基准 {base_days} 天。"
        if days != base_days:
            text += "两个区间不等长，直接比较累计金额，不作为上期同期或消费速度结论。"
        if comparison["delta_minor"] is not None:
            text += f"净消费变化 {comparison['delta_minor']/100:+,.2f} 元"
            if comparison["percent_change"] is not None:
                text += f"（{comparison['percent_change']}%）"
            text += "。"
        else:
            text += "数据不完整，不能给出完整变化金额或比例。"
    return text
