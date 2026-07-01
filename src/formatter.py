"""
Target Formatter — safely wipes all user-created resources from the
target account in reverse-dependency order before a migration run.

Optimizations vs the original sequential implementation:
  - Per-resource-type parallelism via ThreadPoolExecutor. The rate
    limiter in ZendeskClient gates concurrent calls account-wide, so we
    never exceed the plan's RPM. Resource *types* still run in their
    documented order (Webhooks → Triggers → ... → Groups) so a delete
    never races a dependent resource.
  - Bulk delete via `destroy_many` for resources that support it
    (currently: organizations). 100 IDs per call, asynchronous job_status
    polling. Saves ~99 % of round-trips when the target has hundreds of
    orgs.

Bug fixes vs v1 (preserved):
  - item_id=None guard: items with no ID are skipped.
  - delete_tpl missing {id} placeholder raises before mass-deletion.
  - Exception handling covers ZendeskAPIError and ZendeskNetworkError.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.utils import logger

# Deletion order: business logic first, foundation last.
# Each tuple: (display_label, list_api_path, list_rkey, delete_path_template, bulk_path_or_None)
# `bulk_path_or_None` is the `destroy_many` endpoint when Zendesk supports
# one for this resource — set to None for resources that only allow
# single DELETE (the vast majority).
FORMAT_ORDER = [
    ("HC Themes",              "guide/theming/themes",         "themes",               "guide/theming/themes/{id}",           None),
    ("Webhooks",               "webhooks",                     "webhooks",             "webhooks/{id}",                       None),
    ("Triggers",               "triggers",                     "triggers",             "triggers/{id}",                       None),
    ("Trigger Categories",     "trigger_categories",           "trigger_categories",   "trigger_categories/{id}",             None),
    ("Automations",            "automations",                  "automations",          "automations/{id}",                    None),
    ("Macros",                 "macros",                       "macros",               "macros/{id}",                         None),
    ("Views",                  "views",                        "views",                "views/{id}",                          None),
    ("SLA Policies",           "slas/policies",                "sla_policies",         "slas/policies/{id}",                  None),
    ("Group SLA Policies",     "group_slas/policies",          "group_sla_policies",   "group_slas/policies/{id}",            None),
    ("Schedules",              "business_hours/schedules",     "schedules",            "business_hours/schedules/{id}",       None),
    ("Routing Attributes",     "routing/attributes",           "attributes",           "routing/attributes/{id}",             None),
    ("Dynamic Content",        "dynamic_content/items",        "items",                "dynamic_content/items/{id}",          None),
    ("Custom Ticket Statuses", "custom_statuses",              "custom_statuses",      "custom_statuses/{id}",                None),
    ("HC Articles",            "help_center/articles",         "articles",             "help_center/articles/{id}",           None),
    ("HC Sections",            "help_center/sections",         "sections",             "help_center/sections/{id}",           None),
    ("HC Categories",          "help_center/categories",       "categories",           "help_center/categories/{id}",         None),
    ("HC User Segments",       "help_center/user_segments",    "user_segments",        "help_center/user_segments/{id}",      None),
    ("Ticket Forms",           "ticket_forms",                 "ticket_forms",         "ticket_forms/{id}",                   None),
    ("Ticket Fields (custom)", "ticket_fields",                "ticket_fields",        "ticket_fields/{id}",                  None),
    ("User Fields",            "user_fields",                  "user_fields",          "user_fields/{id}",                    None),
    ("Org Fields",             "organization_fields",          "organization_fields",  "organization_fields/{id}",            None),
    ("Custom Roles",           "custom_roles",                 "custom_roles",         "custom_roles/{id}",                   None),
    ("Users",                  "users",                        "users",                "users/{id}",                         None),
    ("Organizations",          "organizations",                "organizations",        "organizations/{id}",                  "organizations/destroy_many"),
    ("Brands",                 "brands",                       "brands",               "brands/{id}",                         None),
    ("Groups",                 "groups",                       "groups",               "groups/{id}",                         None),
]

# Worker count per resource type. Kept modest — the rate limiter is the
# real ceiling, and most format-target deletes are tiny so spinning up
# more threads buys nothing.
_MAX_WORKERS = 6

# Chunk size for destroy_many — Zendesk caps at 100 per call.
_BULK_DELETE_CHUNK = 100

# System/built-in types that must never be deleted.
# Must match SYSTEM_TICKET_FIELD_TYPES in remapper.py and backup.py —
# "tickettype" is the API value for the "Type" field and "custom_status"
# is the "Ticket status" field; both are built into every account and
# cannot be deleted regardless of plan.
SYSTEM_TYPES = frozenset({
    "subject", "description", "status", "custom_status",
    "priority", "tickettype", "type",
    "assignee", "group", "requester", "organization", "tags",
    "ticket_form_id", "brand_id", "due_at",
})


def _current_user_id(client: ZendeskClient) -> Optional[int]:
    """Return the ID of the user behind the current token, or None if the
    lookup fails. Used to make absolutely sure format-target never tries
    to delete the operator's own account."""
    try:
        me = client.get("users/me.json") or {}
        user = me.get("user") or me
        uid = user.get("id")
        return int(uid) if isinstance(uid, (int, str)) and str(uid).strip() else None
    except (ZendeskAPIError, ZendeskNetworkError, ValueError, TypeError):
        return None


def preview(client: ZendeskClient) -> List[Dict]:
    """
    Returns a summary of what would be deleted (used for confirmation prompt).
    Silently skips resources that aren't accessible (plan restrictions, etc.)
    """
    self_user_id = _current_user_id(client)
    summary = []
    for label, list_path, list_rkey, _, _ in FORMAT_ORDER:
        try:
            items = client.list_resource(list_path, list_rkey)
            deletable = [
                i for i in items
                if _is_deletable(i, label=label, self_user_id=self_user_id)
            ]
            if deletable:
                summary.append({"resource": label, "count": len(deletable)})
        except (ZendeskAPIError, ZendeskNetworkError):
            # Silently skip inaccessible resources in preview mode
            pass
        except Exception as exc:
            logger.warn(f"Unexpected error previewing {label}: {exc}")
    return summary


def execute(client: ZendeskClient) -> None:
    """
    Delete all user-created resources in the target in safe reverse-dependency order.
    Each resource type runs its deletes in parallel (rate-limited by the client);
    resource types themselves are processed sequentially so a delete never races
    a dependent.
    """
    logger.section("Formatting Target Account")

    # Identify the operator's own user before we start — needed so the
    # Users wipe never tries to delete the account we're authenticating
    # as (Zendesk returns 403 with "You cannot delete yourself").
    self_user_id = _current_user_id(client)
    if self_user_id is not None:
        logger.info(f"  Operator user id={self_user_id} will be protected from deletion.")

    for label, list_path, list_rkey, delete_tpl, bulk_path in FORMAT_ORDER:
        if not delete_tpl or "{id}" not in delete_tpl:
            logger.warn(
                f"Invalid delete path template for '{label}': '{delete_tpl}'. "
                "Skipping this resource type to prevent accidental mass deletion."
            )
            continue

        try:
            items = client.list_resource(list_path, list_rkey)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(f"Could not list {label}: {exc}. Skipping.")
            continue
        except Exception as exc:
            logger.warn(f"Unexpected error listing {label}: {exc}. Skipping.")
            continue

        deletable = [
            i for i in items
            if _is_deletable(i, label=label, self_user_id=self_user_id)
        ]
        if not deletable:
            continue

        # Filter out items with unusable IDs once, up front.
        valid: List[Dict] = []
        for item in deletable:
            item_id = item.get("id")
            if item_id is None:
                logger.warn(
                    f"Skipping {label} item with no 'id' field: "
                    f"{str(item)[:100]}"
                )
                continue
            if not isinstance(item_id, (int, str)) or str(item_id).strip() == "":
                logger.warn(
                    f"Skipping {label} item with invalid id={item_id!r}"
                )
                continue
            valid.append(item)

        if not valid:
            continue

        logger.info(f"  Deleting {len(valid)} {label}...")

        if bulk_path:
            _bulk_delete(client, label, bulk_path, valid)
        else:
            _parallel_delete(client, label, delete_tpl, valid)

    logger.success("Target account formatted. Ready for clean import.")


def _parallel_delete(
    client: ZendeskClient,
    label: str,
    delete_tpl: str,
    items: List[Dict],
) -> None:
    """Fan out single DELETEs across a thread pool, gated by the
    client's account-wide rate limiter."""
    workers = max(1, min(_MAX_WORKERS, len(items)))

    def _one(item: Dict) -> None:
        item_id = item["id"]
        item_name = (
            item.get("name") or item.get("title") or f"id={item_id}"
        )
        delete_path = delete_tpl.replace("{id}", str(item_id))
        # Retry on 409 Conflict — Zendesk HC articles/sections can be
        # temporarily locked by another client (e.g. a concurrent index
        # rebuild). Back off and retry up to 3 times before giving up.
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                client.delete(delete_path)
                logger.log_purged(label, item_id, item_name)
                break
            except ZendeskAPIError as exc:
                if exc.status_code == 409 and attempt < max_attempts:
                    import time as _time
                    wait = 2 ** attempt  # 2s, 4s, 8s
                    logger.warn(
                        f"{label} id={item_id} locked (409), "
                        f"retrying in {wait}s (attempt {attempt}/{max_attempts - 1})…"
                    )
                    _time.sleep(wait)
                    continue
                logger.log_failed(label, item_id, f"Delete failed: {exc}", item_name)
                break
            except ZendeskNetworkError as exc:
                logger.log_failed(label, item_id, f"Delete failed: {exc}", item_name)
                break
            except Exception as exc:
                logger.log_failed(
                    label, item_id,
                    f"Unexpected error during delete: {type(exc).__name__}: {exc}",
                    item_name,
                )
                break

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, it) for it in items]
        for f in as_completed(futures):
            # Errors are already logged inside _one — surface anything
            # that escaped (shouldn't happen) so we don't swallow bugs.
            exc = f.exception()
            if exc is not None:
                logger.warn(f"{label}: unexpected worker error: {exc}")


def _bulk_delete(
    client: ZendeskClient,
    label: str,
    bulk_path: str,
    items: List[Dict],
) -> None:
    """Delete via Zendesk's `destroy_many` endpoint in 100-id chunks.

    Each chunk kicks off a job; we poll it to completion before sending
    the next chunk. That keeps the load predictable and lets us log
    per-item outcomes (Zendesk's job results include success/failure per id).
    """
    by_id: Dict[str, Dict] = {str(it["id"]): it for it in items}
    ids = list(by_id.keys())

    for start in range(0, len(ids), _BULK_DELETE_CHUNK):
        chunk = ids[start:start + _BULK_DELETE_CHUNK]
        try:
            results = client.destroy_many(bulk_path, chunk)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            # Whole-chunk failure — log each item so the operator sees the
            # blast radius rather than a single mysterious row.
            logger.warn(
                f"{label}: destroy_many chunk failed ({len(chunk)} ids): {exc}"
            )
            for cid in chunk:
                it = by_id.get(cid, {})
                name = it.get("name") or it.get("title") or f"id={cid}"
                logger.log_failed(label, cid, f"Bulk delete failed: {exc}", name)
            continue

        # Match results back to source items. Zendesk's destroy_many
        # job_status results explicitly include each deleted `id` and a
        # `success` boolean, which we trust as the primary correlation
        # key. Position is used only as a fallback when the entry omits
        # `id` (which would indicate a malformed response we shouldn't
        # silently absorb).
        seen_ids = set()
        for pos, entry in enumerate(results):
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            cid = str(entry_id) if entry_id is not None else None
            if cid is None or cid not in by_id:
                # Fallback to positional correlation for malformed entries.
                if 0 <= pos < len(chunk):
                    cid = chunk[pos]
                else:
                    continue
            seen_ids.add(cid)
            it = by_id.get(cid, {})
            name = it.get("name") or it.get("title") or f"id={cid}"

            # Zendesk's documented success shape: {"success": true, "status": "Deleted", ...}
            # Failure shape: {"success": false, "error": "...", "details": "..."}
            success = entry.get("success")
            err = entry.get("error") or entry.get("details")
            if success is False or err:
                if isinstance(err, (dict, list)):
                    import json as _json
                    err = _json.dumps(err)[:300]
                logger.log_failed(label, cid, str(err or "Unknown failure")[:300], name)
            else:
                logger.log_purged(label, cid, name)

        # Anything in the chunk Zendesk silently dropped from the results
        # array is suspicious — log it instead of pretending it succeeded.
        for cid in chunk:
            if cid in seen_ids:
                continue
            it = by_id.get(cid, {})
            name = it.get("name") or it.get("title") or f"id={cid}"
            logger.warn(
                f"{label}: id={cid} ({name}) missing from destroy_many "
                f"results — outcome unknown."
            )


def _is_deletable(
    item: Dict,
    *,
    label: Optional[str] = None,
    self_user_id: Optional[int] = None,
) -> bool:
    """
    Return False for built-in/system resources that cannot or should not be deleted.
    Also returns False if the item has no ID (cannot be safely deleted).

    For Users specifically, additional guards apply (the Zendesk API
    refuses to delete admins, the account owner, and the user behind the
    current token, but we filter those out client-side so the request
    never goes out — that keeps the log clean of 403s):
      - id == 1               (Zendesk system / account-owner sentinel)
      - role == "admin"       (would 403 anyway)
      - id == self_user_id    ("You cannot delete yourself")
      - role_type == 0        (owner role on Suite plans)
      - any user whose `default_group_id` we can't verify is safer to skip
        if they are an agent — but we leave that to the caller, since
        agents are legitimately deletable in many migration scenarios.
    """
    if not isinstance(item, dict):
        return False
    if item.get("id") is None:
        return False
    if item.get("type") in SYSTEM_TYPES:
        return False
    if item.get("built_in"):
        return False
    # Live help center theme cannot be deleted (Zendesk API returns error)
    if item.get("live"):
        return False
    # Default groups, default ticket forms, default brands, etc. cannot be
    # deleted by Zendesk regardless of plan — attempting the DELETE always
    # 422s with "Default group / form / brand". Skip them silently.
    if item.get("default"):
        return False

    # Users get extra guards because user delete has the largest blast
    # radius (account owner, self-user, open-ticket requesters all
    # respond with confusing errors).
    if label == "Users":
        uid = item.get("id")
        # Never delete the Zendesk system user (id=1) or the account owner.
        if uid == 1:
            return False
        # Don't delete the user behind the current API token.
        if self_user_id is not None and uid == self_user_id:
            return False
        # Skip admins — Zendesk forbids deleting admins via this endpoint.
        if item.get("role") == "admin":
            return False
        # Skip account owners (role_type 0 on Suite). `role_type` is not
        # always present, so we tolerate missing values.
        if item.get("role_type") == 0:
            return False
        # The user behind any active API session that we know about
        # exposes `verified` and `active` — but Zendesk does not give us
        # a "has open tickets" signal in the list payload, so deletes
        # for ticket-requesters will still 422 at the API. Those are
        # logged per-item rather than silenced here.

    return True
