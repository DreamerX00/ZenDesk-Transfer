import math
import sys
from typing import Any, Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.importer import flush_id_map, load_id_map, _record_mapping
from src.remapper import strip_source_fields, remap_payload, RemapError
from src.utils import logger

BATCH_SIZE = 100

# When more than this many users would be created in a single migration
# run, Zendesk's internal anomaly detection has been observed to flag
# the account and put it into a suspended/under-review state — a costly
# detour to recover from. We surface a warning at this threshold and ask
# the operator to confirm or supply a smaller --max-users window.
#
# Pragmatic number based on field experience plus Zendesk's docs that
# the in-product bulk-importer caps each batch at 2,000 rows but
# explicitly recommends contacting Zendesk Support to *enable* data
# imports before doing large jobs. There is no published hard limit
# for "imports per hour" — this threshold errs on the safe side.
SUSPENSION_RISK_THRESHOLD = 500


def _emit_suspension_warning(count_to_create: int) -> None:
    """Print the one-time-step warning when the planned user count is
    high enough to risk tripping Zendesk's anomaly detection."""
    logger.warn(
        "─────────────────────────────────────────────────────────────────\n"
        f"⚠  HIGH-VOLUME USER CREATION ({count_to_create} users planned)\n"
        "─────────────────────────────────────────────────────────────────\n"
        "  Creating many users in a short window can trip Zendesk's\n"
        "  internal anomaly detection and put the TARGET account into a\n"
        "  suspended / under-review state. Recovering from that takes a\n"
        "  manual support exchange and adds days to the migration.\n"
        "\n"
        "  ONE-TIME prerequisite step (do this BEFORE proceeding):\n"
        "    1. The target account OWNER must email Zendesk Support and\n"
        "       request that 'data imports be enabled and bulk user\n"
        "       creation be pre-approved for an upcoming migration'.\n"
        "    2. Contact:  suspensions@zendesk.com  (the practical fast\n"
        "       lane) — or open a regular ticket via Zendesk Customer\n"
        "       Support (support.zendesk.com). Include:\n"
        "         • Target subdomain\n"
        "         • Estimated user count and timeframe\n"
        "         • Source subdomain (so they see this is a migration)\n"
        "    3. Wait for written confirmation that imports are enabled.\n"
        "       Without this, anomaly detection may suspend the account\n"
        "       mid-run.\n"
        "\n"
        "  Mitigations this tool already applies:\n"
        "    • verified=true on every user → no welcome / verification\n"
        "      email goes out (suppresses the spike Zendesk watches for).\n"
        "    • Token-bucket rate limiter keyed to your plan's RPM.\n"
        "    • create_many batches of 100 (the API-documented max).\n"
        "\n"
        "  To migrate in smaller chunks instead, re-run with:\n"
        "    python main.py run --phase 5 \\\n"
        "                       --max-users 250 \\\n"
        "                       --users-from 0          # first 250\n"
        "    python main.py run --phase 5 \\\n"
        "                       --max-users 250 \\\n"
        "                       --users-from 250        # next 250\n"
        "─────────────────────────────────────────────────────────────────"
    )


def _confirm_proceed(count_to_create: int, assume_yes: bool) -> bool:
    """Ask the operator whether to proceed past the suspension-risk
    threshold. Auto-proceed if --yes was passed; auto-abort if stdin
    isn't a TTY (CI/script context) to avoid silent danger.
    """
    if assume_yes:
        logger.info("  --yes set — proceeding without confirmation.")
        return True
    if not sys.stdin.isatty():
        logger.warn(
            "  stdin is not a TTY and --yes was not passed. "
            "Refusing to create a high-volume batch in non-interactive mode. "
            "Re-run with --max-users to chunk, or pass --yes."
        )
        return False
    try:
        ans = input(
            f"  Proceed with creating {count_to_create} users? "
            "[y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def _probe_create_many(target: ZendeskClient) -> bool:
    """Check if the account supports bulk user creation (create_many)."""
    try:
        url = target._url("users/create_many")
        resp = target._request(
            "POST", url, json={"users": [{"name": "_probe_", "verified": True}]}
        )
        return resp.ok
    except Exception:
        return False


def _create_single(
    target: ZendeskClient, payload: Dict, source_id: int, name: str,
) -> Dict:
    url = target._url("users")
    resp = target._request("POST", url, json={"user": payload})
    if not resp.ok:
        raise ZendeskAPIError(
            resp.status_code, target._safe_decode(resp, 500), url
        )
    return resp.json()


def run(
    source: ZendeskClient,
    target: ZendeskClient,
    exports: Dict[str, List[Dict]],
    *,
    max_users: Optional[int] = None,
    users_from: int = 0,
    assume_yes: bool = False,
) -> None:
    """Phase 5 — migrate end-users from source to target.

    max_users  — cap how many users to create in this run. None = all.
    users_from — skip the first N source users (offset). Combined with
                 max_users this lets you slice the import into windows
                 to keep anomaly detection from tripping. Example:
                   first run:  users_from=0,   max_users=500
                   second run: users_from=500, max_users=500
    assume_yes — skip the interactive confirmation prompt that fires
                 when the planned count exceeds SUSPENSION_RISK_THRESHOLD.
    """
    logger.section("Phase 5 — User Migration")
    id_map = load_id_map()

    source_users: List[Dict] = exports.get("users", [])
    if not source_users:
        logger.info("No users in export — skipping.")
        return

    # Apply the user-supplied slice BEFORE any other processing so log
    # output reflects the actual window being attempted.
    total_available = len(source_users)
    if users_from < 0:
        users_from = 0
    if users_from >= total_available:
        logger.info(
            f"users_from={users_from} is past the end of the source "
            f"({total_available} users available) — nothing to do."
        )
        return
    sliced = source_users[users_from:]
    if max_users is not None and max_users > 0:
        sliced = sliced[:max_users]
    source_users = sliced

    if total_available != len(source_users):
        logger.info(
            f"Source users to process: {len(source_users)} "
            f"(window users_from={users_from}, "
            f"max_users={max_users if max_users else 'all'}; "
            f"{total_available} total in export)"
        )
    else:
        logger.info(f"Source users to process: {len(source_users)}")

    # Build target user email lookup for conflict detection
    try:
        target_users = target.list_resource("users", "users")
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(f"Could not list target users: {exc}. Skipping user migration.")
        return

    target_by_email: Dict[str, Dict] = {}
    for u in target_users:
        email = (u.get("email") or "").strip().lower()
        if email:
            target_by_email[email] = u

    logger.info(f"Target users found: {len(target_users)}")

    # Pre-process, strip, remap, and queue
    queued: List[Dict] = []
    skipped = 0
    failed = 0

    for user in source_users:
        if not isinstance(user, dict):
            continue

        source_id = user.get("id")
        email = (user.get("email") or "").strip().lower()
        name = user.get("name", f"<unnamed id={source_id}>")

        # Skip pre-defined/system agent
        if source_id == 1:
            logger.log_skipped("users", source_id, "Pre-defined system agent (id=1)")
            skipped += 1
            continue

        # Skip conflicts by email
        if email and email in target_by_email:
            conflict = target_by_email[email]
            logger.log_skipped(
                "users", source_id,
                f"Email '{email}' already exists in target (id={conflict.get('id')})"
            )
            skipped += 1
            continue

        # Strip server-managed fields
        try:
            payload = strip_source_fields(user)
        except Exception as exc:
            logger.log_failed("users", source_id,
                              f"strip_source_fields failed: {exc}", name)
            failed += 1
            continue

        # Remap IDs (organization_id, custom role IDs, etc.)
        try:
            payload = remap_payload(payload, id_map, context=f"users:{name}")
        except RemapError as exc:
            logger.log_failed("users", source_id, str(exc), name)
            failed += 1
            continue
        except Exception as exc:
            logger.log_failed("users", source_id,
                              f"Unexpected remapper error: {exc}", name)
            failed += 1
            continue

        # Strip null values so Zendesk applies defaults
        for field in list(payload.keys()):
            if payload[field] is None:
                del payload[field]

        # Strip user_fields (custom user field values) — we don't remap them yet
        payload.pop("user_fields", None)

        # Suppress verification/welcome email
        payload["verified"] = True

        queued.append({
            "source_id": source_id,
            "name": name,
            "payload": payload,
        })

    if not queued:
        logger.info(f"No users to create. Skipped={skipped} Failed={failed}")
        return

    # Suspension-risk gate. Above the threshold we surface a loud
    # warning explaining the suspensions@zendesk.com one-time step and
    # require explicit confirmation (or --yes) before proceeding.
    if len(queued) > SUSPENSION_RISK_THRESHOLD:
        _emit_suspension_warning(len(queued))
        if not _confirm_proceed(len(queued), assume_yes):
            logger.warn(
                f"Aborting phase 5 at user's request. "
                f"{len(queued)} users were prepared but not created. "
                f"Re-run with --max-users <N> to migrate in safe chunks."
            )
            return

    # Probe: check if bulk create_many is available
    use_bulk = _probe_create_many(target)
    if use_bulk:
        logger.info(
            f"Creating {len(queued)} users (skipped={skipped}, failed={failed}) "
            f"via create_many (batch size={BATCH_SIZE})..."
        )
    else:
        logger.info(
            f"Bulk create_many not available (403). "
            f"Creating {len(queued)} users individually..."
        )

    created_count = 0

    if use_bulk:
        # Batch mode
        total_batches = math.ceil(len(queued) / BATCH_SIZE)
        for batch_idx in range(total_batches):
            start = batch_idx * BATCH_SIZE
            end = start + BATCH_SIZE
            batch = queued[start:end]
            batch_payloads = [item["payload"] for item in batch]

            try:
                results = target.create_many(
                    "users/create_many",
                    "users",
                    batch_payloads,
                )
            except (ZendeskAPIError, ZendeskNetworkError) as exc:
                for item in batch:
                    logger.log_failed(
                        "users", item["source_id"],
                        f"create_many batch {batch_idx + 1}/{total_batches} failed: {exc}",
                        item["name"],
                    )
                continue
            except Exception as exc:
                for item in batch:
                    logger.log_failed(
                        "users", item["source_id"],
                        f"Unexpected batch error: {type(exc).__name__}: {exc}",
                        item["name"],
                    )
                continue

            # Process results. Zendesk's job_status `results` array is
            # returned in submission order; the documented `index` field
            # is sometimes absent in practice (confirmed empirically and
            # in the public docs), so we trust array position and only
            # use `index` as a defensive tie-breaker.
            for pos, result in enumerate(results or []):
                raw_idx = result.get("index")
                if isinstance(raw_idx, int) and 0 <= raw_idx < len(batch):
                    bi = raw_idx
                elif 0 <= pos < len(batch):
                    bi = pos
                else:
                    continue

                item = batch[bi]
                source_id = item["source_id"]
                item_name = item["name"]

                if result.get("dry_run"):
                    logger.log_skipped(
                        "users", source_id, "dry-run (user would be created)"
                    )
                    continue

                target_id = result.get("id")
                if target_id is not None:
                    _record_mapping(id_map, "users", source_id, target_id)
                    logger.log_created("users", source_id, target_id, item_name)
                    created_count += 1
                else:
                    error = result.get("error") or result.get("details") or "unknown"
                    logger.log_failed(
                        "users", source_id, f"User creation failed: {error}", item_name
                    )
    else:
        # Individual mode
        for item in queued:
            source_id = item["source_id"]
            name = item["name"]
            payload = item["payload"]

            try:
                result = _create_single(target, payload, source_id, name)
            except (ZendeskAPIError, ZendeskNetworkError) as exc:
                logger.log_failed(
                    "users", source_id,
                    f"User creation failed: {exc}",
                    name,
                )
                failed += 1
                continue
            except Exception as exc:
                logger.log_failed(
                    "users", source_id,
                    f"Unexpected error: {type(exc).__name__}: {exc}",
                    name,
                )
                failed += 1
                continue

            user_data = (result or {}).get("user", {})
            target_id = user_data.get("id") if isinstance(user_data, dict) else None
            if target_id is not None:
                _record_mapping(id_map, "users", source_id, target_id)
                logger.log_created("users", source_id, target_id, name)
                created_count += 1
            else:
                logger.log_failed(
                    "users", source_id,
                    f"User creation returned no id: {result}",
                    name,
                )
                failed += 1

    flush_id_map(id_map)
    logger.success(
        f"User migration complete: {created_count} created, "
        f"{failed} failed, {skipped} skipped."
    )
