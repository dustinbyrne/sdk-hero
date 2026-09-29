"""Human-readable summaries of observed source changes."""

from collections import Counter


def check_summary(checks):
    counts = Counter()
    for check in checks:
        status = str(check.get("status") or "").upper()
        result = str(check.get("conclusion") or check.get("state") or "").upper()
        if status in {"IN_PROGRESS", "QUEUED", "WAITING", "PENDING", "REQUESTED"}:
            label = "running" if status == "IN_PROGRESS" else "pending"
        else:
            label = {
                "SUCCESS": "passed",
                "FAILURE": "failed",
                "ERROR": "failed",
                "TIMED_OUT": "timed out",
                "CANCELLED": "cancelled",
                "SKIPPED": "skipped",
                "NEUTRAL": "neutral",
                "ACTION_REQUIRED": "need action",
                "PENDING": "pending",
                "EXPECTED": "pending",
            }.get(result, "unknown")
        counts[label] += 1
    order = (
        "failed",
        "timed out",
        "need action",
        "cancelled",
        "passed",
        "running",
        "pending",
        "neutral",
        "skipped",
        "unknown",
    )
    return "CI: " + (
        ", ".join(f"{counts[key]} {key}" for key in order if counts[key]) or "no checks reported"
    )


def source_change_summary(source, changes):
    kind = {"pr": "PR", "issue": "Issue", "ticket": "Support ticket", "run": "Workflow run"}.get(
        source["kind"], "Source"
    )
    prefix = f"{kind} #{source['remote_id']}"
    parts = []
    handled = set()

    def add(keys, text):
        if set(keys) & changes.keys():
            parts.append(text)
            handled.update(keys)

    if "state" in changes:
        state = changes["state"]["after"]
        add(["state"], f"State: {str(state).lower()}" if state is not None else "State unavailable")
    if "reviewDecision" in changes:
        decision = changes["reviewDecision"]["after"]
        add(
            ["reviewDecision"],
            {
                "APPROVED": "Approved",
                "CHANGES_REQUESTED": "Changes requested",
                "REVIEW_REQUIRED": "Review required",
            }.get(decision, "Review status updated"),
        )
    add(["headRefOid"], "PR revision changed")
    if "checks" in changes:
        add(["checks"], check_summary(changes["checks"]["after"] or []))
    if "comments" in changes:
        count = changes["comments"]["after"]
        add(
            ["comments"], f"Comments: {count}" if count is not None else "Comment count unavailable"
        )
    add(["last_external_reply_at"], "External reply record updated")
    add(["last_team_reply_at"], "Team reply record updated")
    if "needs_team_reply" in changes:
        needed = changes["needs_team_reply"]["after"]
        add(
            ["needs_team_reply"],
            "Team reply needed"
            if needed is True
            else "No team reply pending"
            if needed is False
            else "Reply status unavailable",
        )
    review_timing = ["review_target_hours", "review_waiting_since", "review_head_updated_at"]
    cleared = "review_target_hours" in changes and changes["review_target_hours"]["after"] is None
    add(review_timing, "Review SLA cleared" if cleared else "Review timing updated")
    add(["awaiting_team_since", "sla_due_at"], "Reply timing updated")
    add(["title"], "Title updated")
    add(["labels"], "Labels updated")
    add(["author", "author_association"], "Author details updated")
    for field in sorted(changes.keys() - handled):
        parts.append(f"{field.replace('_', ' ').capitalize()} updated")
    return prefix + ": " + "; ".join(parts) + "."
