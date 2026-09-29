"""Response targets and work ordering, computed without writing countdowns to storage."""

from datetime import datetime, time, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def parse_date(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, AttributeError):
        return None


def business_seconds(start, end):
    """Signed elapsed time intersecting weekday 09:00–17:00 Eastern windows."""
    if end < start:
        return -business_seconds(end, start)
    start, end = start.astimezone(EASTERN), end.astimezone(EASTERN)
    day = start.date()
    total = 0.0
    while day <= end.date():
        if day.weekday() < 5:
            opening = datetime.combine(day, time(9), EASTERN)
            closing = datetime.combine(day, time(17), EASTERN)
            total += max(0, (min(end, closing) - max(start, opening)).total_seconds())
        day += timedelta(days=1)
    return total


def add_business_hours(start, hours):
    cursor = start.astimezone(EASTERN)
    remaining = hours * 3600
    while True:
        opening = datetime.combine(cursor.date(), time(9), EASTERN)
        closing = datetime.combine(cursor.date(), time(17), EASTERN)
        if cursor.weekday() < 5:
            cursor = max(cursor, opening)
            available = max(0, (closing - cursor).total_seconds())
            if remaining <= available:
                return cursor + timedelta(seconds=remaining)
            remaining -= available
        cursor = datetime.combine(cursor.date() + timedelta(days=1), time(9), EASTERN)


def compact_time(seconds):
    if seconds == 0:
        return "0m"
    minutes = max(1, ceil(abs(seconds) / 60))
    # Whole hours round toward the deadline; show minutes in the final hour.
    text = f"{minutes // 60}h" if minutes >= 60 else f"{minutes}m"
    return ("-" if seconds < 0 else "") + text


def source_target(source):
    if source.get("sla_resolved"):
        return None
    facts = source["facts"]
    state = str(facts.get("state") or facts.get("status") or "").lower()
    if state in ("closed", "merged", "resolved"):
        return None
    since = parse_date(facts.get("awaiting_team_since"))
    if source["kind"] == "ticket":
        due = parse_date(facts.get("sla_due_at"))
        if due:
            return {"group": 0, "since": since, "due": due, "label": "Support deadline"}
    elif source["kind"] == "pr":
        since = parse_date(facts.get("review_waiting_since"))
        hours = facts.get("review_target_hours")
        if since and hours in (4, 8) and not facts.get("isDraft"):
            return {
                "group": 1,
                "since": since,
                "due": add_business_hours(since, hours),
                "label": (
                    f"PR {'initial review' if hours == 8 else 'follow-up'} ({hours} business hours)"
                    + (
                        " · approximate PR update time"
                        if parse_date(facts.get("review_head_updated_at")) == since
                        else ""
                    )
                ),
            }
    elif source["kind"] == "issue":
        if since and facts.get("needs_team_reply"):
            return {
                "group": 2,
                "since": since,
                "due": add_business_hours(since, 4),
                "label": "Issue response (4 business hours)",
                "action": "Reply",
            }
        team = parse_date(facts.get("last_team_reply_at"))
        external = parse_date(facts.get("last_external_reply_at"))
        if state == "open" and team and external and team >= external:
            return {
                "group": 3,
                "since": team,
                "due": add_business_hours(team, 16),
                "label": "Issue follow-up (16 business hours)",
                "action": "Follow up",
            }
    return None


def card_schedule(task, sources, moment=None):
    moment = moment or datetime.now(timezone.utc)
    targets = [
        {**target, "source_key": source.get("key"), "url": source.get("url", "")}
        for source in sources
        if (target := source_target(source))
    ]
    # Keep work categories and queue age available as sorting tie-breakers.
    support = task["kind"] == "support" or any(s["kind"] == "ticket" for s in sources)
    group = 0 if support else min((t["group"] for t in targets), default=3)
    if not support and not targets:
        if task["kind"] == "external_pr" and not any(
            "review_target_hours" in s["facts"] for s in sources if s["kind"] == "pr"
        ):
            group = 1
        elif any(
            s["kind"] == "issue"
            and s["facts"].get("needs_team_reply")
            and str(s["facts"].get("state", "")).lower() == "open"
            for s in sources
        ):
            group = 2
    applicable = [t for t in targets if t["group"] == group]
    selected = task.get("sla_source")
    if selected:
        target = next((t for t in targets if t["source_key"] == selected), None)
    else:
        support_targets = [t for t in targets if t["group"] == 0]
        target = (
            min(support_targets, key=lambda t: t["due"])
            if support_targets
            else max(
                targets, key=lambda t: (t["since"], t["due"], t["source_key"] or ""), default=None
            )
        )
    waiting = min((t["since"] for t in applicable if t["since"]), default=None)
    if support:
        waiting = min(
            (
                date
                for s in sources
                if s["kind"] == "ticket"
                and str(s["facts"].get("status", "")).lower() not in ("resolved", "closed")
                if (date := parse_date(s["facts"].get("awaiting_team_since")))
            ),
            default=None,
        )
    seconds = business_seconds(moment, target["due"]) if target else None
    active = task["status"] != "done"
    badge = compact_time(seconds) if active and seconds is not None else ""
    if badge and target.get("action"):
        badge = f"{target['action']} · {badge}"
    level = (
        "overdue"
        if seconds is not None and seconds < 0
        else "soon"
        if seconds is not None and seconds <= 7200
        else "normal"
    )
    tooltip = (
        f"{target['label']} · due {target['due'].astimezone(EASTERN):%a %b %d, %I:%M %p %Z}"
        " · weekdays 9–5 Eastern" + (f" · {target['url']}" if target["url"] else "")
        if target
        else ""
    )
    return {
        "badge": badge,
        "level": level,
        "tooltip": tooltip,
        "group": group,
        "waiting_since": waiting.isoformat() if waiting else None,
        "due_at": target["due"].isoformat() if target else None,
    }


def work_key(task, schedule):
    # Earliest deadline first; cards without a deadline follow timed cards at each priority.
    last = datetime.max.replace(tzinfo=timezone.utc)
    deadline = parse_date(schedule.get("due_at")) or last
    age = parse_date(schedule["waiting_since"]) or last
    return task["priority"], deadline, schedule["group"], age, task["id"]
