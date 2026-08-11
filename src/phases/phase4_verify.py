"""
Phase 4 — Verification & Report Generation.

Bug fixes vs v1:
  - _count now distinguishes "ERR" from numeric counts — two ERR counts
    no longer show as a false ✅ match.
  - Report generation wrapped in try/except — a write failure doesn't
    crash the whole verify step.
  - _read_log handles IOError (not just JSONDecodeError).
"""

import json
from collections import defaultdict
from typing import Dict, List

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.utils import logger
from src.utils.runctx import state_dir, migration_log_path

VERIFY_RESOURCES = [
    ("Groups",               "groups",                    "groups"),
    ("Brands",               "brands",                    "brands"),
    ("Ticket Fields",        "ticket_fields",             "ticket_fields"),
    ("User Fields",          "user_fields",               "user_fields"),
    ("Org Fields",           "organization_fields",       "organization_fields"),
    ("Ticket Forms",         "ticket_forms",              "ticket_forms"),
    ("Organizations",        "organizations",             "organizations"),
    ("Views",                "views",                     "views"),
    ("Triggers",             "triggers",                  "triggers"),
    ("Trigger Categories",   "trigger_categories",        "trigger_categories"),
    ("Automations",          "automations",               "automations"),
    ("Macros",               "macros",                    "macros"),
    ("SLA Policies",         "slas/policies",             "sla_policies"),
    ("Custom Ticket Statuses","custom_statuses",           "custom_statuses"),
    ("Schedules",            "business_hours/schedules",  "schedules"),
    ("Webhooks",             "webhooks",                  "webhooks"),
    ("HC Categories",        "help_center/categories",    "categories"),
    ("HC Sections",          "help_center/sections",      "sections"),
    ("HC Articles",          "help_center/articles",      "articles"),
    ("HC Themes",            "guide/theming/themes",      "themes"),
    ("Users",                "users",                     "users"),
    ("Group Memberships",        "group_memberships",         "group_memberships"),
    ("Organization Memberships", "organization_memberships",  "organization_memberships"),
]

# Resources where a count match is NOT enough: order (position) and/or status
# (active/draft) must also match the source for a true exact copy. We match
# source→target objects by a stable name/key and compare position ORDER
# (relative sequence, robust to system items occupying fixed target slots) and
# the status field. (label, api_path, rkey, name_field, status_field|None)
DEEP_VERIFY = [
    ("Ticket Fields",  "ticket_fields",            "ticket_fields",       "title", "active"),
    ("Ticket Forms",   "ticket_forms",             "ticket_forms",        "name",  "active"),
    ("User Fields",    "user_fields",              "user_fields",         "key",   "active"),
    ("Org Fields",     "organization_fields",      "organization_fields", "key",   "active"),
    ("Views",          "views",                    "views",               "title", "active"),
    ("Triggers",       "triggers",                 "triggers",            "title", "active"),
    ("Automations",    "automations",              "automations",         "title", "active"),
    ("Macros",         "macros",                   "macros",              "title", "active"),
    ("SLA Policies",   "slas/policies",            "sla_policies",        "title", None),
    ("HC Sections",    "help_center/sections",     "sections",            "name",  None),
    ("HC Articles",    "help_center/articles",     "articles",            "title", "draft"),
]

_SENTINEL_ERR = object()  # unique sentinel — not equal to any string


def run(source: ZendeskClient, target: ZendeskClient) -> None:
    logger.section("Phase 5 — Verification & Report")
    rows = []

    for label, api_path, rkey in VERIFY_RESOURCES:
        src_count = _count(source, api_path, rkey)
        tgt_count = _count(target, api_path, rkey)

        # Bug fix: use sentinel to distinguish error from matching counts
        # Two ERR values should NOT show as ✅
        if src_count is _SENTINEL_ERR or tgt_count is _SENTINEL_ERR:
            match = "⚠ ERR"
            src_display = "ERR" if src_count is _SENTINEL_ERR else str(src_count)
            tgt_display = "ERR" if tgt_count is _SENTINEL_ERR else str(tgt_count)
        else:
            match = "✅" if src_count == tgt_count else "❌"
            src_display = str(src_count)
            tgt_display = str(tgt_count)

        rows.append({
            "Resource": label,
            "Source": src_display,
            "Target": tgt_display,
            "Match": match,
        })

    logger.print_table(
        "Migration Count Verification",
        rows,
        ["Resource", "Source", "Target", "Match"],
    )

    # Per-object content/order/status comparison — a count match alone can hide
    # wrong order, wrong status, or entirely different objects that happen to
    # tally the same.
    deep_rows, deep_ok = _deep_verify(source, target)
    logger.print_table(
        "Content, Order & Status Verification",
        deep_rows,
        ["Resource", "Matched", "Missing", "Status≠", "Order"],
    )

    # Aggregate pass/fail gate — a count ❌/ERR or any deep mismatch fails the
    # whole verification so a wrong migration cannot end on a green "complete".
    count_ok = all(r["Match"] == "✅" for r in rows)
    overall_ok = count_ok and deep_ok

    try:
        _generate_report(rows, deep_rows, overall_ok)
    except Exception as exc:
        logger.warn(f"Could not write migration report: {exc}")

    if overall_ok:
        logger.success(
            f"Verification PASSED — counts, order and status match source. "
            f"Report → {state_dir() / 'migration_report.md'}"
        )
    else:
        logger.warn(
            "Verification FAILED — the target is NOT yet an exact copy. See the "
            "Content/Order/Status table above and the MANUAL/FAILED sections of "
            f"the report → {state_dir() / 'migration_report.md'}"
        )


def _deep_verify(source: ZendeskClient, target: ZendeskClient):
    """
    For each ordered/status-bearing resource, match source→target objects by
    name/key and check: all present, status equal, and relative position order
    preserved. Returns (rows, all_ok).

    Order is compared as a RELATIVE sequence of the objects present in both
    accounts (not absolute position numbers) so fixed system slots on the
    target don't cause false failures.
    """
    findings: List[Dict] = []
    all_ok = True

    for label, api_path, rkey, name_field, status_field in DEEP_VERIFY:
        try:
            src_items = source.list_resource(api_path, rkey)
            tgt_items = target.list_resource(api_path, rkey)
        except (ZendeskAPIError, ZendeskNetworkError, Exception):
            findings.append({
                "Resource": label, "Matched": "ERR", "Missing": "ERR",
                "Status≠": "ERR", "Order": "⚠ ERR",
            })
            all_ok = False
            continue

        tgt_by_name = {
            i.get(name_field): i
            for i in tgt_items
            if isinstance(i, dict) and i.get(name_field)
        }

        matched = missing = status_mismatch = 0
        common = []  # (name, src_position, tgt_position) for order comparison
        for s in src_items:
            if not isinstance(s, dict):
                continue
            name = s.get(name_field)
            if not name:
                continue
            t = tgt_by_name.get(name)
            if t is None:
                missing += 1
                continue
            matched += 1
            if status_field and s.get(status_field) != t.get(status_field):
                status_mismatch += 1
            s_pos, t_pos = s.get("position"), t.get("position")
            if s_pos is not None and t_pos is not None:
                common.append((name, s_pos, t_pos))

        src_seq = [n for (n, sp, tp) in sorted(common, key=lambda x: (x[1], x[0]))]
        tgt_seq = [n for (n, sp, tp) in sorted(common, key=lambda x: (x[2], x[0]))]
        order_ok = src_seq == tgt_seq

        res_ok = missing == 0 and status_mismatch == 0 and order_ok
        all_ok = all_ok and res_ok
        findings.append({
            "Resource": label,
            "Matched": str(matched),
            "Missing": str(missing),
            "Status≠": str(status_mismatch) if status_field else "—",
            "Order": "✅" if order_ok else "❌",
        })

    return findings, all_ok


def _count(client: ZendeskClient, path: str, rkey: str):
    """
    Return count of items (int), or _SENTINEL_ERR on failure.
    Using a sentinel instead of the string "ERR" prevents false ✅ matches.
    """
    try:
        items = client.list_resource(path, rkey)
        return len(items)
    except (ZendeskAPIError, ZendeskNetworkError):
        return _SENTINEL_ERR
    except Exception:
        return _SENTINEL_ERR


def _generate_report(count_rows: List[Dict],
                     deep_rows: List[Dict] = None,
                     overall_ok: bool = None) -> None:
    deep_rows = deep_rows or []
    log_entries = _read_log()
    stats: Dict[str, int] = defaultdict(int)
    for entry in log_entries:
        action = entry.get("action")
        if isinstance(action, str):
            stats[action] += 1

    manual_items = [e for e in log_entries if e.get("action") == "MANUAL"]
    failed_items = [e for e in log_entries if e.get("action") == "FAILED"]
    purged_items = [e for e in log_entries if e.get("action") == "PURGED"]
    skipped_items = [e for e in log_entries if e.get("action") == "SKIPPED"]

    lines = ["# Zendesk Migration Report\n"]
    if overall_ok is not None:
        verdict = "✅ PASS — exact copy" if overall_ok else "❌ FAIL — not an exact copy"
        lines.append(f"**Verification result: {verdict}**\n")
    lines += [
        "## Summary\n",
        "| Action  | Count |",
        "|---------|-------|",
    ]
    for action, count in sorted(stats.items()):
        lines.append(f"| {action} | {count} |")

    lines += [
        "\n## Resource Count Verification\n",
        "| Resource | Source | Target | Match |",
        "|----------|--------|--------|-------|",
    ]
    for row in count_rows:
        lines.append(
            f"| {row['Resource']} | {row['Source']} | {row['Target']} | {row['Match']} |"
        )

    if deep_rows:
        lines += [
            "\n## Content, Order & Status Verification\n",
            "Matched = objects found on both by name/key · Missing = on source but "
            "not target · Status≠ = active/draft differs · Order = relative "
            "position preserved.\n",
            "| Resource | Matched | Missing | Status≠ | Order |",
            "|----------|---------|---------|---------|-------|",
        ]
        for row in deep_rows:
            lines.append(
                f"| {row['Resource']} | {row['Matched']} | {row['Missing']} "
                f"| {row['Status≠']} | {row['Order']} |"
            )

    if skipped_items:
        lines += ["\n## Skipped Resources (conflict — not imported)\n"]
        for e in skipped_items:
            resource = e.get("resource", "?")
            sid = e.get("source_id", "?")
            reason = e.get("reason", "")
            lines.append(f"- `{resource}` source_id={sid}  {reason}")

    if purged_items:
        lines += ["\n## Purged Resources (deleted from target before re-import)\n"]
        for e in purged_items:
            tid = e.get("target_id", "?")
            name = e.get("name", "")
            resource = e.get("resource", "?")
            lines.append(f"- `{resource}` target_id={tid}  {name}")

    if failed_items:
        lines += ["\n## Failed Resources\n"]
        for e in failed_items:
            resource = e.get("resource", "?")
            sid = e.get("source_id", "?")
            name = e.get("name", "")
            # Truncate error to avoid extremely long report lines
            error = str(e.get("error", ""))[:300]
            lines.append(
                f"- `{resource}` source_id={sid}  {name}  **Error**: {error}"
            )

    if manual_items:
        lines += ["\n## Manual Action Required\n"]
        for e in manual_items:
            resource = e.get("resource", "?")
            note = e.get("note", "")
            lines.append(f"- `{resource}` — {note}")

    lines += [
        "\n## Cutover Checklist\n",
        "- [ ] Re-verify email sender addresses in target",
        "- [ ] Reconnect social channels (OAuth)",
        "- [ ] Reinstall Marketplace apps from `exports/installed_apps.json`",
        "- [ ] Update webhook endpoints with new signing secrets (see MANUAL entries)",
        "- [ ] Review MANUAL entries for unmigrated owners/authors & subdomain URLs",
        "- [ ] Verify agent→group memberships (auto-migrated; confirm coverage)",
        "- [ ] Test a sample ticket end-to-end",
        "- [ ] Confirm Help Center articles are published",
        "- [ ] Update DNS/CNAME if custom domain was used",
    ]

    report_dir = state_dir()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "migration_report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"  Report saved → {report_path}")


def _read_log() -> List[Dict]:
    log_path = migration_log_path()
    if not log_path.exists():
        return []
    entries = []
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        entries.append(entry)
                except json.JSONDecodeError:
                    pass  # skip malformed lines
    except OSError as exc:
        logger.warn(f"Could not read migration log: {exc}")
    return entries
