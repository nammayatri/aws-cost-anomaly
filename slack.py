from datetime import date as _date, timedelta as _td

from slack_sdk import WebClient

from detect import Anomaly


def _fmt_date(d: _date) -> str:
    return d.strftime("%a %d %b")


def _money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _ymoney(v: float) -> str:
    """Yellow-tinted dollar amount for use in the main message (uses the moneybag emoji)."""
    return f":moneybag: {_money(v)}"


def _pct_abs(v: float) -> str:
    if v == float("inf") or v == float("-inf"):
        return "from $0"
    return f"{abs(v):.0f}%"


def _direction_word(v: float) -> str:
    if v > 0:
        return "up"
    if v < 0:
        return "down"
    return "flat"


def _severity_emoji(pct: float) -> str:
    if pct == float("inf") or pct >= 50:
        return ":rotating_light:"
    if pct >= 20:
        return ":warning:"
    if pct >= 5:
        return ":large_yellow_circle:"
    if pct > -5:
        return ":white_circle:"
    return ":large_green_circle:"


def _trend_arrow(pct: float) -> str:
    return ":chart_with_upwards_trend:" if pct >= 0 else ":chart_with_downwards_trend:"


def _attachment_color(pct: float) -> str:
    if pct == float("inf") or pct >= 50:
        return "#d62728"   # red
    if pct >= 20:
        return "#ff7f0e"   # orange
    if pct >= 5:
        return "#f1c40f"   # yellow
    if pct > -5:
        return "#7f8c8d"   # gray
    return "#2ca02c"       # green


def _humanize_total_line(label: str, pct: float, abs_delta: float, baseline_date: _date, baseline_value: float) -> str:
    direction = _direction_word(abs_delta)
    arrow = _trend_arrow(pct)
    baseline_label = _fmt_date(baseline_date)
    if pct == float("inf"):
        return f"{arrow} {label}: *up {_ymoney(abs_delta)}* vs {baseline_label} (was {_ymoney(0)})"
    return (
        f"{arrow} {label}: *{direction} {_pct_abs(pct)}* "
        f"({_ymoney(abs(abs_delta))}) vs {baseline_label} ({_ymoney(baseline_value)})"
    )


def _mention_text(mention: str) -> str:
    """Slack mention syntax. Supports 'here', 'channel', user IDs (U...), and group IDs (S...)."""
    if not mention:
        return ""
    m = mention.strip().lstrip("@")
    if m in ("here", "channel", "everyone"):
        return f"<!{m}>"
    if m.startswith(("U", "W")):
        return f"<@{m}>"
    if m.startswith("S"):
        return f"<!subteam^{m}>"
    return m


def _main_color(dod_pct: float, wow_pct: float) -> str:
    """Side-bar color for the main message based on the overall trend."""
    worst_up = max(dod_pct, wow_pct)
    if worst_up >= 20:
        return "#d62728"   # red — meaningful overall increase
    if worst_up >= 5:
        return "#f1c40f"   # yellow — mild increase
    if min(dod_pct, wow_pct) <= -5:
        return "#2ca02c"   # green — overall down
    return "#7f8c8d"       # gray — flat


def build_header_payload(summary: dict, increase_count: int, decrease_count: int, mention: str = "") -> tuple[list[dict], list[dict]]:
    """Returns (top_level_blocks, attachments_for_colored_body)."""
    d = summary["date"]
    total = summary["total"]
    dod_pct = summary["total_dod_pct"]
    wow_pct = summary["total_wow_pct"]
    headline_emoji = _severity_emoji(max(dod_pct, wow_pct))

    prev_total = total - summary["total_dod_abs"]
    lw_total = total - summary["total_wow_abs"]
    dod_dir = _direction_word(summary["total_dod_abs"])
    wow_dir = _direction_word(summary["total_wow_abs"])
    summary_sentence = (
        f"Total spend was *{_ymoney(total)}* — "
        f"{dod_dir} {_pct_abs(dod_pct)} from the day before ({_ymoney(prev_total)}), "
        f"{wow_dir} {_pct_abs(wow_pct)} from the same day last week ({_ymoney(lw_total)})."
    )

    parts = []
    if increase_count:
        parts.append(f":chart_with_upwards_trend: *{increase_count}* service(s) increased")
    if decrease_count:
        parts.append(f":chart_with_downwards_trend: *{decrease_count}* service(s) decreased")
    verdict = " · ".join(parts) + "  ·  open the thread :arrow_down: for the breakdown"

    mention_str = _mention_text(mention)
    top_blocks: list[dict] = []
    if mention_str:
        top_blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": mention_str}})
    top_blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": f"💰 AWS Daily Cost Report — {d.strftime('%a %d %b %Y')}", "emoji": True},
    })

    body_attachment = {
        "color": _main_color(dod_pct, wow_pct),
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{headline_emoji}  {summary_sentence}"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{_humanize_total_line('Day-over-day', dod_pct, summary['total_dod_abs'], d - _td(days=1), prev_total)}\n"
                        f"{_humanize_total_line('Week-over-week', wow_pct, summary['total_wow_abs'], d - _td(days=7), lw_total)}"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": verdict}],
            },
        ],
    }
    return top_blocks, [body_attachment]


# Backwards-compat shim (--dry-run path).
def build_header_blocks(summary: dict, increase_count: int, decrease_count: int, mention: str = "") -> list[dict]:
    top, atts = build_header_payload(summary, increase_count, decrease_count, mention)
    return top + [{"type": "section", "text": {"type": "mrkdwn", "text": "(body rendered as colored attachment below)"}}] + atts[0]["blocks"]


def _anomaly_sentence(a: Anomaly) -> str:
    """Plain English explanation of what happened to this service."""
    parts = []
    # Headline change
    if a.dod_pct == float("inf"):
        parts.append(f"jumped from $0 to *{_money(a.today)}*")
    elif a.dod_pct >= 100:
        parts.append(f"*more than doubled* — from {_money(a.yesterday)} to {_money(a.today)}")
    elif a.dod_abs > 0:
        parts.append(
            f"went *up {_pct_abs(a.dod_pct)}* — from {_money(a.yesterday)} to {_money(a.today)} "
            f"(+{_money(a.dod_abs)})"
        )
    else:
        parts.append(f"was {_money(a.today)} yesterday (flat vs day before)")

    # Add the WoW context if it's meaningful and different from the DoD story
    if "WoW" in a.rules and a.wow_pct not in (float("inf"),):
        parts.append(
            f"and is *{_pct_abs(a.wow_pct)} higher* than the same day last week "
            f"(was {_money(a.last_week)}, +{_money(a.wow_abs)})"
        )
    elif a.wow_pct == float("inf"):
        parts.append(f"and was $0 the same day last week")

    return ", ".join(parts) + "."


def _explain_rules(rules: list[str]) -> str:
    if rules == ["DoD"]:
        return "_Triggered: spike vs yesterday_"
    if rules == ["WoW"]:
        return "_Triggered: spike vs same day last week_"
    return "_Triggered: spike vs both yesterday and last week_"


def _reason_lines(movers: list[dict]) -> list[str]:
    """For each usage type that went UP, explain in one line why it drove the service cost higher.
    Usage types that stayed flat or dropped are not 'reasons' — we hide them."""
    lines = []
    upward = [m for m in movers if m["dod_abs"] > 0 or m["wow_abs"] > 0]
    upward.sort(key=lambda m: max(m["dod_abs"], m["wow_abs"]), reverse=True)
    for m in upward:
        ut = m["usage_type"]
        if m["yesterday"] == 0 and m["dod_abs"] > 0:
            lines.append(
                f"   • `{ut}` — *brand-new charge yesterday* of {_money(m['today'])} "
                f"(was $0 the day before)"
            )
        elif m["dod_abs"] > 0 and m["wow_abs"] > 0:
            lines.append(
                f"   • `{ut}` — went from {_money(m['yesterday'])} → *{_money(m['today'])}* "
                f"(+{_money(m['dod_abs'])} vs day before, +{_money(m['wow_abs'])} vs last week)"
            )
        elif m["dod_abs"] > 0:
            lines.append(
                f"   • `{ut}` — went from {_money(m['yesterday'])} → *{_money(m['today'])}* "
                f"(+{_money(m['dod_abs'])} vs day before)"
            )
        else:  # only wow_abs > 0
            lines.append(
                f"   • `{ut}` — *{_money(m['today'])}* today, +{_money(m['wow_abs'])} vs last week"
            )
    return lines


def _terse_service_header(a: Anomaly, target: _date) -> str:
    prev = target - _td(days=1)
    lw = target - _td(days=7)
    sev = _severity_emoji(max(a.dod_pct, a.wow_pct))
    dod_part = (
        f"+{_money(a.dod_abs)} ({_pct_abs(a.dod_pct)}) vs {_fmt_date(prev)}"
        if a.dod_abs > 0 else f"{_money(a.dod_abs)} vs {_fmt_date(prev)}"
    )
    wow_part = (
        f"+{_money(a.wow_abs)} ({_pct_abs(a.wow_pct)}) vs {_fmt_date(lw)}"
        if a.wow_abs > 0 else f"{_money(a.wow_abs)} vs {_fmt_date(lw)}"
    )
    # Show the transition for whichever window actually triggered the flag, so the
    # arrow direction matches the alert. If only WoW tripped, compare to last week.
    if "DoD" in a.rules:
        base_val, base_date = a.yesterday, prev
    else:
        base_val, base_date = a.last_week, lw
    return (
        f"{sev} *{a.service}*  `{_money(base_val)} → {_money(a.today)}`  _(vs {_fmt_date(base_date)})_\n"
        f"{dod_part} · {wow_part}"
    )


def _fmt_qty(v: float, unit: str) -> str:
    u = (unit or "").lower()
    if "byte" in u or u == "gb":
        # CE returns bytes-based usage in GB already for most data-transfer types,
        # but some come as raw bytes. Show in GB if value looks like GB, else bytes→GB.
        gb = v if u == "gb" else v / 1_000_000_000
        return f"{gb:,.2f} GB"
    if "hour" in u or u == "hrs":
        return f"{v:,.1f} hours"
    if "request" in u or u == "requests":
        return f"{int(v):,} requests"
    if "second" in u:
        return f"{v:,.0f} sec"
    if "count" in u:
        return f"{int(v):,}"
    # fallback — show raw + the unit string AWS returned
    if v >= 1000:
        return f"{v:,.1f} {unit}".strip()
    return f"{v:,.2f} {unit}".strip()


def _qty_change_phrase(m: dict, basis: str) -> str:
    """basis is 'dod' or 'wow' — describe the usage-quantity change for that comparison."""
    unit = m.get("unit", "")
    if basis == "dod":
        q_now, q_then = m["qty_today"], m["qty_yesterday"]
    else:
        q_now, q_then = m["qty_today"], m["qty_last_week"]
    if q_now == 0 and q_then == 0:
        return ""
    return f"{_fmt_qty(q_then, unit)} → {_fmt_qty(q_now, unit)}"


def _terse_reason_lines(movers: list[dict], target: _date) -> list[str]:
    prev = target - _td(days=1)
    lw = target - _td(days=7)
    lines = []
    upward = [m for m in movers if m["dod_abs"] > 0 or m["wow_abs"] > 0]
    upward.sort(key=lambda m: max(m["dod_abs"], m["wow_abs"]), reverse=True)
    for m in upward:
        ut = m["usage_type"]
        basis = "dod" if m["dod_abs"] > 0 else "wow"
        baseline = prev if basis == "dod" else lw
        delta_cost = m["dod_abs"] if basis == "dod" else m["wow_abs"]
        qty_phrase = _qty_change_phrase(m, basis)

        # Cost baseline must match the window that triggered: yesterday for DoD, last week for WoW.
        base_cost = m["yesterday"] if basis == "dod" else m["last_week"]

        if base_cost == 0 and m["today"] > 0:
            usage_today = _fmt_qty(m["qty_today"], m.get("unit", ""))
            lines.append(
                f"• `{ut}` — *new charge* of {_money(m['today'])}; usage: {usage_today} "
                f"(was $0 / 0 on {_fmt_date(baseline)})"
            )
        else:
            qty_part = f" ; usage: {qty_phrase}" if qty_phrase else ""
            lines.append(
                f"• `{ut}` — cost {_money(base_cost)} → *{_money(m['today'])}* "
                f"(+{_money(delta_cost)} vs {_fmt_date(baseline)}){qty_part}"
            )
    return lines


def _down_header(a: Anomaly, target: _date) -> str:
    prev = target - _td(days=1)
    lw = target - _td(days=7)
    dod_part = f"{_money(a.dod_abs)} ({_pct_abs(a.dod_pct)}) vs {_fmt_date(prev)} ({_money(a.yesterday)})"
    wow_part = f"{_money(a.wow_abs)} ({_pct_abs(a.wow_pct)}) vs {_fmt_date(lw)} ({_money(a.last_week)})"
    return (
        f":large_green_circle: *{a.service}*  `{_money(a.yesterday)} → {_money(a.today)}`\n"
        f"{dod_part} · {wow_part}"
    )


def _down_reason_lines(movers: list[dict], target: _date) -> list[str]:
    prev = target - _td(days=1)
    lw = target - _td(days=7)
    downward = [m for m in movers if m["dod_abs"] < 0 or m["wow_abs"] < 0]
    downward.sort(key=lambda m: min(m["dod_abs"], m["wow_abs"]))
    lines = []
    for m in downward:
        ut = m["usage_type"]
        if m["dod_abs"] < 0:
            baseline, delta, base_cost, qty_then, qty_now = prev, m["dod_abs"], m["yesterday"], m["qty_yesterday"], m["qty_today"]
        else:
            baseline, delta, base_cost, qty_then, qty_now = lw, m["wow_abs"], m["last_week"], m["qty_last_week"], m["qty_today"]
        unit = m.get("unit", "")
        qty_part = f" ; usage: {_fmt_qty(qty_then, unit)} → {_fmt_qty(qty_now, unit)}" if (qty_then or qty_now) else ""
        lines.append(
            f"• `{ut}` — cost {_money(base_cost)} → *{_money(m['today'])}* "
            f"({_money(delta)} vs {_fmt_date(baseline)}){qty_part}"
        )
    return lines


def build_thread_attachments(summary: dict, increases: list[Anomaly], decreases: list[Anomaly], movers_by_service: dict[str, list[dict]]) -> list[dict]:
    target = summary["date"]
    attachments = []

    if increases:
        attachments.append({
            "color": "#34495e",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f":chart_with_upwards_trend: *Services that increased ({len(increases)})*"}}],
        })
        for a in increases:
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": _terse_service_header(a, target)}}]
            reasons = _terse_reason_lines(movers_by_service.get(a.service, []), target)
            if reasons:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(reasons)}})
            attachments.append({
                "color": _attachment_color(max(a.dod_pct, a.wow_pct)),
                "blocks": blocks,
            })

    if decreases:
        attachments.append({
            "color": "#34495e",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f":chart_with_downwards_trend: *Services that decreased ({len(decreases)})*"}}],
        })
        for a in decreases:
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": _down_header(a, target)}}]
            reasons = _down_reason_lines(movers_by_service.get(a.service, []), target)
            if reasons:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(reasons)}})
            attachments.append({
                "color": "#2ca02c",
                "blocks": blocks,
            })

    return attachments


def post(cfg: dict, summary: dict, increases: list[Anomaly], decreases: list[Anomaly], movers_by_service: dict[str, list[dict]]) -> None:
    client = WebClient(token=cfg["slack_bot_token"])

    top_blocks, body_attachments = build_header_payload(
        summary, len(increases), len(decreases), cfg.get("mention", "")
    )
    main = client.chat_postMessage(
        channel=cfg["slack_channel_id"],
        blocks=top_blocks,
        attachments=body_attachments,
        text=f"AWS daily cost report — {summary['date'].isoformat()}",
        unfurl_links=False,
        unfurl_media=False,
    )

    attachments = build_thread_attachments(summary, increases, decreases, movers_by_service)
    client.chat_postMessage(
        channel=main["channel"],
        thread_ts=main["ts"],
        text="Detailed breakdown",
        attachments=attachments,
        unfurl_links=False,
        unfurl_media=False,
    )
