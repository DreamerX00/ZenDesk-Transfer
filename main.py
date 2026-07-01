"""
zd-transfer — Zendesk Account Configuration Migration CLI

Usage:
  python main.py pre-flight               Validate credentials + baseline scan
  python main.py format-target            Wipe target account (with confirmation)
  python main.py run [--phase N]          Run full migration (or a specific phase)
  python main.py migrate                  Safe: backup target → format → migrate
  python main.py restore [--path DIR]     Restore target from a previous backup
  python main.py verify                   Run verification + generate report
  python main.py cleanup                  Undo EVERYTHING this tool created (full rollback)
  python main.py rollback --phase N       Roll back one specific phase from target

Global flags:
  --dry-run    Preview all actions without making any writes
  --source     Path to source .env file  (default: config/source.env)
  --target     Path to target .env file  (default: config/target.env)

Authentication (two mutually exclusive methods per .env file):
  1. Email + API Token (default)
       ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN
  2. OAuth Bearer Token
       ZENDESK_SUBDOMAIN, ZENDESK_OAUTH_TOKEN
       (ZENDESK_EMAIL / ZENDESK_API_TOKEN are NOT required)

Security notes:
  - Credentials are read from .env files and never logged or printed.
  - The .env files are listed in .gitignore to prevent accidental commit.
  - Subdomain and email values are validated before use.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from rich.console import Console
from rich.prompt import Confirm

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.extractor import extract_all
from src.formatter import preview as fmt_preview, execute as fmt_execute
from src.backup import backup as bk_backup, restore as bk_restore, list_backups
from src.utils import logger

console = Console()

ROOT = Path(__file__).parent

# ------------------------------------------------------------------ #
#  Rollback resource definitions (shared by rollback + cleanup)       #
# ------------------------------------------------------------------ #
#
# ALL_RESOURCES is ordered from most-dependent to least-dependent.
# This is the correct deletion order: children before parents.
# Each entry: (id_map_key, delete_path_template)
#
ALL_RESOURCES_ORDERED = [
    # Help Center (deepest children first)
    ("hc_articles",           "help_center/articles/{id}"),
    ("hc_sections",           "help_center/sections/{id}"),
    ("hc_categories",         "help_center/categories/{id}"),
    ("hc_user_segments",      "help_center/user_segments/{id}"),
    ("themes",                "guide/theming/themes/{id}"),
    # Business logic
    ("webhooks",              "webhooks/{id}"),
    ("dynamic_content_items", "dynamic_content/items/{id}"),
    ("routing_attributes",    "routing/attributes/{id}"),
    ("schedules",             "business_hours/schedules/{id}"),
    ("group_sla_policies",    "group_slas/policies/{id}"),
    ("sla_policies",          "slas/policies/{id}"),
    ("macros",                "macros/{id}"),
    ("automations",           "automations/{id}"),
    ("triggers",              "triggers/{id}"),
    ("trigger_categories",    "trigger_categories/{id}"),
    ("views",                 "views/{id}"),
    # Foundation (least-dependent last)
    ("organizations",         "organizations/{id}"),
    ("ticket_forms",          "ticket_forms/{id}"),
    ("custom_roles",          "custom_roles/{id}"),
    ("organization_fields",   "organization_fields/{id}"),
    ("user_fields",           "user_fields/{id}"),
    ("ticket_fields",         "ticket_fields/{id}"),
    ("brands",                "brands/{id}"),
    ("groups",                "groups/{id}"),
]

PHASE_RESOURCE_KEYS = {
    1: [
        "organizations", "ticket_forms", "custom_roles",
        "organization_fields", "user_fields", "ticket_fields",
        "brands", "groups",
    ],
    2: [
        "webhooks", "dynamic_content_items", "routing_attributes",
        "schedules", "group_sla_policies", "sla_policies",
        "macros", "automations", "triggers", "trigger_categories", "views",
    ],
    3: [
        "hc_articles", "hc_sections", "hc_categories", "hc_user_segments",
        "themes",
    ],
}


# ------------------------------------------------------------------ #
#  Credential loading                                                 #
# ------------------------------------------------------------------ #

def _load_client(env_path: str, dry_run: bool = False) -> ZendeskClient:
    """
    Load credentials from a .env file and construct a ZendeskClient.

    Supports two mutually-exclusive authentication methods:

      1. Email + API Token (Basic Auth) — default Zendesk approach:
           ZENDESK_SUBDOMAIN, ZENDESK_EMAIL, ZENDESK_API_TOKEN

      2. OAuth Bearer Token — for apps using the Zendesk OAuth 2.0 flow:
           ZENDESK_SUBDOMAIN, ZENDESK_OAUTH_TOKEN
           (ZENDESK_EMAIL and ZENDESK_API_TOKEN are NOT required)

    dotenv_values() silently returns {} if the file doesn't exist, so
    we check existence first to give a clear "file not found" error.
    Credential values are never echoed in error messages.
    """
    path = Path(env_path)
    if not path.exists():
        logger.error(
            f"Credentials file not found: '{env_path}'\n"
            "  Copy and fill in the template:\n"
            f"    cp config/source.env.example {env_path}"
        )
        sys.exit(1)
    if not path.is_file():
        logger.error(f"'{env_path}' exists but is not a file.")
        sys.exit(1)

    cfg = dotenv_values(str(path))

    subdomain = cfg.get("ZENDESK_SUBDOMAIN", "").strip()
    if not subdomain:
        logger.error(
            f"ZENDESK_SUBDOMAIN is missing or empty in '{env_path}'."
        )
        sys.exit(1)

    oauth_token = cfg.get("ZENDESK_OAUTH_TOKEN", "").strip()
    email       = cfg.get("ZENDESK_EMAIL",     "").strip()
    api_token   = cfg.get("ZENDESK_API_TOKEN", "").strip()

    # Guard: do not allow mixing both auth methods in the same file
    if oauth_token and (email or api_token):
        logger.error(
            f"Ambiguous credentials in '{env_path}': "
            "ZENDESK_OAUTH_TOKEN cannot be combined with "
            "ZENDESK_EMAIL / ZENDESK_API_TOKEN. "
            "Use one auth method only."
        )
        sys.exit(1)

    try:
        if oauth_token:
            # --- OAuth Bearer token path ---
            oauth_refresh_token = cfg.get("ZENDESK_OAUTH_REFRESH_TOKEN", "").strip() or None
            oauth_client_id     = cfg.get("ZENDESK_CLIENT_ID", "").strip() or None
            oauth_client_secret = cfg.get("ZENDESK_CLIENT_SECRET", "").strip() or None
            client = ZendeskClient(
                subdomain=subdomain,
                oauth_token=oauth_token,
                oauth_refresh_token=oauth_refresh_token,
                oauth_client_id=oauth_client_id,
                oauth_client_secret=oauth_client_secret,
                env_path=env_path,
                dry_run=dry_run,
            )
        else:
            # --- Email + API token (Basic Auth) path ---
            missing = [
                k for k, v in [
                    ("ZENDESK_EMAIL", email),
                    ("ZENDESK_API_TOKEN", api_token),
                ]
                if not v
            ]
            if missing:
                logger.error(
                    f"The following required keys are missing from '{env_path}': "
                    + ", ".join(missing)
                    + "\n  (Alternatively, set ZENDESK_OAUTH_TOKEN for OAuth auth.)"
                )
                sys.exit(1)
            client = ZendeskClient(
                subdomain=subdomain,
                email=email,
                api_token=api_token,
                dry_run=dry_run,
            )
    except ValueError as exc:
        logger.error(f"Invalid credential format in '{env_path}': {exc}")
        sys.exit(1)

    # Pre-fetch the account's rate-limit ceiling. One cheap GET teaches the
    # throttle the account's true RPM (200 on Team, 700 on Enterprise, etc.)
    # so the very first workload request paces correctly. Failures are
    # silently tolerated — the conservative default keeps the run safe.
    rpm = client.prefetch_plan()
    logger.info(f"  Rate limit calibrated: {rpm} requests/min ({subdomain})")
    return client


# ------------------------------------------------------------------ #
#  Shared rollback engine                                             #
# ------------------------------------------------------------------ #

def _rollback_resources(target: ZendeskClient, id_map: dict,
                        resource_keys: list, label: str = "") -> int:
    """
    Delete target-account resources tracked in id_map for the given
    list of resource_keys (in the order provided — caller must supply
    correct dependency order: children first).

    Returns the number of successful deletions.
    """
    deleted = 0
    delete_path_map = dict(ALL_RESOURCES_ORDERED)

    for rkey in resource_keys:
        mappings = id_map.get(rkey)
        if not isinstance(mappings, dict) or not mappings:
            continue

        tpl = delete_path_map.get(rkey, "")
        if not tpl or "{id}" not in tpl:
            logger.warn(
                f"No delete path template for '{rkey}' — "
                "skipping to prevent accidental API calls."
            )
            continue

        logger.info(f"  {'[' + label + '] ' if label else ''}Deleting {len(mappings)} {rkey}...")
        for source_id, target_id in list(mappings.items()):
            if not target_id or str(target_id).strip() in ("", "None"):
                logger.warn(
                    f"Skipping {rkey} source_id={source_id}: "
                    f"invalid target_id={target_id!r}"
                )
                continue

            path = tpl.replace("{id}", str(target_id))
            try:
                target.delete(path)
                logger.log_purged(rkey, target_id, f"rollback source_id={source_id}")
                deleted += 1
            except ZendeskAPIError as exc:
                # 404 means already deleted — treat as success to keep rollback idempotent
                if exc.status_code == 404:
                    logger.log_skipped(rkey, source_id, "Already deleted (404)")
                    deleted += 1
                else:
                    logger.log_failed(rkey, source_id, str(exc))
            except ZendeskNetworkError as exc:
                logger.log_failed(rkey, source_id, str(exc))
            except Exception as exc:
                logger.log_failed(
                    rkey, source_id,
                    f"Unexpected error: {type(exc).__name__}: {exc}"
                )

    return deleted


# ------------------------------------------------------------------ #
#  Commands                                                           #
# ------------------------------------------------------------------ #

def cmd_preflight(args):
    logger.section("Pre-Flight Check")
    try:
        source = _load_client(args.source, dry_run=False)
        target = _load_client(args.target, dry_run=False)
    except SystemExit:
        raise

    for label, client in [("SOURCE", source), ("TARGET", target)]:
        try:
            info = client.ping()
            acct = info.get("account", info)
            acct_name = acct.get("name", "?") if isinstance(acct, dict) else "?"
            acct_sub = acct.get("subdomain", "?") if isinstance(acct, dict) else "?"
            logger.success(
                f"{label} connected → {acct_name} ({acct_sub}.zendesk.com)"
            )
        except ZendeskAPIError as exc:
            logger.error(f"{label} connection FAILED: {exc}")
            sys.exit(1)
        except ZendeskNetworkError as exc:
            logger.error(f"{label} network error: {exc}")
            sys.exit(1)
        except Exception as exc:
            logger.error(f"{label} unexpected error: {type(exc).__name__}: {exc}")
            sys.exit(1)

    logger.section("Target Baseline Scan")
    try:
        deletable = fmt_preview(target)
    except Exception as exc:
        logger.warn(f"Baseline scan failed: {exc}. Proceeding without scan.")
        deletable = []

    if not deletable:
        logger.success("Target account is clean. No existing user-created resources found.")
    else:
        rows = [{"Resource": r["resource"], "Count": r["count"]} for r in deletable]
        logger.print_table(
            "Existing target resources (will be PURGED on conflict)", rows,
            ["Resource", "Count"]
        )
        logger.warn("Conflicting resources will be automatically deleted during import.")
        logger.info(
            "Run 'python main.py format-target' for a full pre-migration wipe."
        )

    logger.success("Pre-flight complete.")


def cmd_format_target(args):
    try:
        target = _load_client(args.target, dry_run=args.dry_run)
    except SystemExit:
        raise

    logger.section("Format Target Account")

    try:
        deletable = fmt_preview(target)
    except Exception as exc:
        logger.error(f"Could not preview target resources: {exc}")
        sys.exit(1)

    if not deletable:
        logger.success("Target account is already clean. Nothing to delete.")
        return

    rows = [{"Resource": r["resource"], "Count": r["count"]} for r in deletable]
    logger.print_table("Resources to be DELETED", rows, ["Resource", "Count"])

    if args.dry_run:
        logger.warn("DRY-RUN: no deletions performed.")
        return

    if not Confirm.ask(
        "\n[bold red]⚠  This will permanently delete all listed resources "
        "from the TARGET account. Continue?[/bold red]",
        default=False,
    ):
        logger.info("Aborted.")
        return

    try:
        fmt_execute(target)
    except Exception as exc:
        logger.error(f"format-target encountered an error: {exc}")
        sys.exit(1)

    # After wiping the target, the on-disk id_map points at target IDs that
    # no longer exist. Leaving it in place causes the next run to skip
    # already-mapped resources or produce stale 404s during dependent
    # creates (e.g. sections POSTed to a deleted category). Offer to reset.
    from src.importer import STATE_DIR
    id_map_path = STATE_DIR / "id_map.json"
    if id_map_path.exists() and id_map_path.stat().st_size > 2:
        if Confirm.ask(
            "\nReset state/id_map.json and migration_log.jsonl so the next "
            "run starts fresh? (Recommended after a format.)",
            default=True,
        ):
            _reset_state(STATE_DIR)
            logger.success("State reset.")
        else:
            logger.warn(
                "State preserved. The id_map still references the deleted "
                "target IDs — a re-run may fail with stale-ID errors."
            )


def cmd_run(args):
    dry_run = args.dry_run
    try:
        source = _load_client(args.source, dry_run=False)
        target = _load_client(args.target, dry_run=dry_run)
    except SystemExit:
        raise

    phase = getattr(args, "phase", None)

    logger.section("Extracting from source account")
    try:
        # Build the phase set so extract_all can skip expensive sub-exports
        # (e.g. user identities) that aren't needed for the selected phases.
        _phase_set = None if phase is None else {phase}
        exports = extract_all(source, phases=_phase_set)
    except Exception as exc:
        logger.error(f"Extraction failed unexpectedly: {type(exc).__name__}: {exc}")
        sys.exit(1)

    from src.phases import (
        phase1_foundation, phase2_business_logic,
        phase3_content, phase4_verify, phase5_users,
    )

    try:
        if phase is None or phase == 1:
            phase1_foundation.run(source, target, exports)
        if phase is None or phase == 3:
            phase3_content.run(source, target, exports)
        if phase is None or phase == 2:
            phase2_business_logic.run(source, target, exports)
        if phase is None or phase == 5:
            phase5_users.run(
                source, target, exports,
                max_users=getattr(args, "max_users", None),
                users_from=getattr(args, "users_from", 0) or 0,
                assume_yes=bool(getattr(args, "yes", False)),
            )
        if phase is None or phase == 4:
            phase4_verify.run(source, target)
    except KeyboardInterrupt:
        logger.warn(
            "\nMigration interrupted by user. State saved to state/id_map.json. "
            "Resume by re-running the interrupted phase, or run 'cleanup' to undo."
        )
        sys.exit(130)
    except Exception as exc:
        logger.error(
            f"Migration phase failed with unexpected error: "
            f"{type(exc).__name__}: {exc}"
        )
        sys.exit(1)


def cmd_migrate(args):
    """
    Safe migration: backup → check → format (if needed) → run.

    Steps:
      1. Extract source resources (same as 'run')
      2. Backup all current target resources to backups/{timestamp}/
      3. If target is non-empty, prompt for confirmation then format it
      4. Run all four migration phases
    """
    try:
        source = _load_client(args.source, dry_run=False)
        target = _load_client(args.target, dry_run=args.dry_run)
    except SystemExit:
        raise

    logger.section("Step 1 — Extracting source account")
    try:
        exports = extract_all(source, phases=None)  # migrate always runs all phases
    except Exception as exc:
        logger.error(f"Extraction failed: {type(exc).__name__}: {exc}")
        sys.exit(1)

    logger.section("Step 2 — Backing up target account")
    try:
        backup_dir = bk_backup(target)
    except Exception as exc:
        logger.error(f"Backup failed: {type(exc).__name__}: {exc}")
        if Confirm.ask("Continue with migration despite backup failure?", default=False):
            backup_dir = None
        else:
            sys.exit(1)

    # Step 3 — check if target has resources
    logger.section("Step 3 — Checking target account state")
    try:
        deletable = fmt_preview(target)
    except Exception as exc:
        logger.warn(f"Could not preview target: {exc}. Skipping format step.")
        deletable = None

    if deletable:
        rows = [{"Resource": r["resource"], "Count": r["count"]} for r in deletable]
        logger.print_table(
            "Existing target resources (will be DELETED)", rows,
            ["Resource", "Count"],
        )
        if not args.dry_run and Confirm.ask(
            "\n[bold red]⚠  This will DELETE these resources from TARGET "
            "and then migrate source config in. Continue?[/bold red]",
            default=False,
        ):
            try:
                fmt_execute(target)
                logger.success("Target formatted.")
            except Exception as exc:
                logger.error(f"Format failed: {exc}")
                sys.exit(1)
        elif args.dry_run:
            logger.info("DRY-RUN: skipping format.")
        else:
            logger.info("Aborted.")
            sys.exit(0)
    else:
        logger.success("Target account is already clean — skipping format.")

    # Step 4 — migrate
    logger.section("Step 4 — Running migration")
    from src.phases import (
        phase1_foundation, phase2_business_logic,
        phase3_content, phase4_verify, phase5_users,
    )
    try:
        phase1_foundation.run(source, target, exports)
        phase3_content.run(source, target, exports)
        phase2_business_logic.run(source, target, exports)
        phase5_users.run(
            source, target, exports,
            max_users=getattr(args, "max_users", None),
            users_from=getattr(args, "users_from", 0) or 0,
            assume_yes=bool(getattr(args, "yes", False)),
        )
        phase4_verify.run(source, target)
    except KeyboardInterrupt:
        logger.warn("\nMigration interrupted. State saved.")
        sys.exit(130)
    except Exception as exc:
        logger.error(f"Migration failed: {type(exc).__name__}: {exc}")
        sys.exit(1)

    if backup_dir:
        logger.info(f"Backup saved to: {backup_dir}")
        logger.info("Run 'python main.py restore --path <backup_dir>' to undo.")


def cmd_restore(args):
    """
    Restore target account from a previous backup.
    Lists available backups if no --path is given.
    """
    try:
        target = _load_client(args.target, dry_run=args.dry_run)
    except SystemExit:
        raise

    backup_dir = None
    if args.path:
        p = Path(args.path)
        if p.is_dir():
            backup_dir = p
        else:
            logger.error(f"Backup path not found: {args.path}")
            sys.exit(1)
    else:
        available = list_backups()
        if not available:
            logger.error(
                "No backups found. Run 'python main.py migrate' first "
                "to create a backup."
            )
            sys.exit(1)
        logger.section("Available backups")
        for i, d in enumerate(available, 1):
            meta = d / "metadata.json"
            count = "?"
            if meta.exists():
                try:
                    count = json.loads(meta.read_text()).get("resource_count", "?")
                except (json.JSONDecodeError, OSError):
                    pass
            logger.info(f"  [{i}] {d.name}  ({count} resources)")
        choice = input("\nEnter backup number to restore (or 0 to cancel): ").strip()
        if not choice.isdigit() or int(choice) < 1 or int(choice) > len(available):
            logger.info("Canceled.")
            return
        backup_dir = available[int(choice) - 1]

    logger.section(f"Restoring from {backup_dir.name}")
    if not args.dry_run and not Confirm.ask(
        "\n[bold red]⚠  This will CREATE resources in the TARGET account. "
        "Continue?[/bold red]",
        default=False,
    ):
        logger.info("Aborted.")
        return

    try:
        bk_restore(target, backup_dir)
    except Exception as exc:
        logger.error(f"Restore failed: {type(exc).__name__}: {exc}")
        sys.exit(1)


def cmd_verify(args):
    try:
        source = _load_client(args.source, dry_run=False)
        target = _load_client(args.target, dry_run=False)
    except SystemExit:
        raise

    try:
        from src.phases import phase4_verify
        phase4_verify.run(source, target)
    except Exception as exc:
        logger.error(f"Verification failed: {type(exc).__name__}: {exc}")
        sys.exit(1)


def cmd_cleanup(args):
    """
    Full cleanup — delete EVERY resource this tool created in the target account.

    Unlike `format-target` (which lists ALL resources from the API and deletes
    everything it finds), `cleanup` only touches objects tracked in id_map.json.
    This is a surgical undo of exactly what zd-transfer created.

    After successful deletion the user is offered the option to reset
    state/id_map.json and state/migration_log.jsonl so a fresh run can start.
    """
    try:
        target = _load_client(args.target, dry_run=args.dry_run)
    except SystemExit:
        raise

    logger.section("Cleanup — Full Rollback of All Created Resources")

    from src.importer import load_id_map, STATE_DIR
    id_map = load_id_map()

    # Build a summary of what will be deleted
    summary_rows = []
    total = 0
    for rkey, _ in ALL_RESOURCES_ORDERED:
        mappings = id_map.get(rkey)
        if isinstance(mappings, dict) and mappings:
            count = len(mappings)
            summary_rows.append({"Resource": rkey, "Count": count})
            total += count

    if total == 0:
        logger.success(
            "id_map.json is empty — nothing was created by this tool "
            "or it has already been cleaned up."
        )
        return

    logger.print_table(
        f"Resources to be DELETED from TARGET ({total} total)",
        summary_rows,
        ["Resource", "Count"],
    )

    if args.dry_run:
        logger.warn("DRY-RUN: no deletions performed.")
        return

    if not Confirm.ask(
        f"\n[bold red]⚠  This will permanently delete {total} resources "
        "from the TARGET account that were created by zd-transfer. "
        "Continue?[/bold red]",
        default=False,
    ):
        logger.info("Cleanup aborted.")
        return

    # Execute deletions in correct dependency order (children first)
    all_keys_ordered = [rkey for rkey, _ in ALL_RESOURCES_ORDERED]
    try:
        deleted = _rollback_resources(target, id_map, all_keys_ordered, label="cleanup")
    except KeyboardInterrupt:
        logger.warn(
            "\nCleanup interrupted. Partially rolled back. "
            "Re-run 'cleanup' to continue — already-deleted resources will be skipped (404)."
        )
        sys.exit(130)
    except Exception as exc:
        logger.error(f"Cleanup encountered unexpected error: {type(exc).__name__}: {exc}")
        sys.exit(1)

    logger.success(f"Cleanup complete — {deleted} resource(s) deleted from target.")

    # Offer to reset state files
    if Confirm.ask(
        "\nReset state/id_map.json and state/migration_log.jsonl "
        "for a clean re-run?",
        default=True,
    ):
        _reset_state(STATE_DIR)
        logger.success("State files reset. You can now run a fresh migration.")
    else:
        logger.info(
            "State files preserved. Note: id_map.json still references "
            "deleted target IDs — re-run will attempt to re-create everything."
        )


def cmd_rollback(args):
    """Roll back one specific phase from target."""
    try:
        target = _load_client(args.target, dry_run=args.dry_run)
    except SystemExit:
        raise

    phase = args.phase
    logger.section(f"Rollback — Phase {phase}")

    resource_keys = PHASE_RESOURCE_KEYS.get(phase)
    if not resource_keys:
        logger.error(f"No rollback definition for phase {phase}.")
        sys.exit(1)

    from src.importer import load_id_map
    id_map = load_id_map()

    # Build summary
    summary_rows = []
    total = 0
    for rkey in resource_keys:
        mappings = id_map.get(rkey)
        if isinstance(mappings, dict) and mappings:
            count = len(mappings)
            summary_rows.append({"Resource": rkey, "Count": count})
            total += count

    if total == 0:
        logger.success(
            f"Nothing to roll back for phase {phase} — "
            "no entries in id_map.json for this phase."
        )
        return

    logger.print_table(
        f"Phase {phase} resources to DELETE ({total} total)",
        summary_rows,
        ["Resource", "Count"],
    )

    if args.dry_run:
        logger.warn("DRY-RUN: no deletions performed.")
        return

    if not Confirm.ask(
        f"\n[bold red]⚠  Delete {total} phase-{phase} resources from TARGET?[/bold red]",
        default=False,
    ):
        logger.info("Rollback aborted.")
        return

    try:
        deleted = _rollback_resources(target, id_map, resource_keys,
                                      label=f"phase{phase}")
    except KeyboardInterrupt:
        logger.warn("\nRollback interrupted. Re-run to continue.")
        sys.exit(130)
    except Exception as exc:
        logger.error(f"Rollback error: {type(exc).__name__}: {exc}")
        sys.exit(1)

    logger.success(f"Rollback of phase {phase} complete — {deleted} resource(s) deleted.")


# ------------------------------------------------------------------ #
#  State reset helper                                                 #
# ------------------------------------------------------------------ #

def _reset_state(state_dir: Path) -> None:
    """Safely reset id_map.json and migration_log.jsonl."""
    id_map_path = state_dir / "id_map.json"
    log_path = state_dir / "migration_log.jsonl"

    # Write empty id_map atomically
    try:
        fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({}, f)
        os.replace(tmp, id_map_path)
        logger.info(f"  Reset {id_map_path.name}")
    except Exception as exc:
        logger.warn(f"Could not reset id_map.json: {exc}")

    # Truncate migration log
    try:
        log_path.write_text("", encoding="utf-8")
        logger.info(f"  Reset {log_path.name}")
    except Exception as exc:
        logger.warn(f"Could not reset migration_log.jsonl: {exc}")


# ------------------------------------------------------------------ #
#  CLI setup                                                          #
# ------------------------------------------------------------------ #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zd-transfer",
        description="Zendesk account configuration migration tool.",
    )
    parser.add_argument(
        "--source", default="config/source.env",
        help="Path to source credentials .env file",
    )
    parser.add_argument(
        "--target", default="config/target.env",
        help="Path to target credentials .env file",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview all actions without making any writes",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "pre-flight", help="Validate credentials and scan target baseline"
    )
    sub.add_parser(
        "format-target", help="Wipe ALL user-created resources from target (broad)"
    )

    run_p = sub.add_parser(
        "run", help="Run the migration (all phases or specific phase)"
    )
    run_p.add_argument(
        "--phase", type=int, choices=[1, 2, 3, 4, 5],
        help="Run only this phase (1=foundation, 2=business, 3=content, "
             "4=verify, 5=users)",
    )
    run_p.add_argument(
        "--max-users", type=int, default=None,
        help=(
            "Cap how many users phase 5 will create in this run. Use "
            "with --users-from to slice the import into windows. "
            "Recommended when migrating >500 users — Zendesk's anomaly "
            "detection can suspend accounts that create users too "
            "quickly (see documentation.md → user suspension)."
        ),
    )
    run_p.add_argument(
        "--users-from", type=int, default=0,
        help=(
            "Skip the first N users from the source export before "
            "applying --max-users. Lets you migrate in chunks: "
            "--users-from 0 --max-users 500, then "
            "--users-from 500 --max-users 500, etc."
        ),
    )
    run_p.add_argument(
        "--yes", "-y", action="store_true",
        help=(
            "Skip the interactive confirmation that fires when phase 5 "
            "is about to create more than the suspension-risk threshold "
            "of users. Use in scripted runs after you've already "
            "completed the one-time Zendesk Support pre-approval step."
        ),
    )

    sub.add_parser("verify", help="Run verification and generate migration report")

    sub.add_parser(
        "cleanup",
        help=(
            "FULL rollback — delete only what this tool created "
            "(reads id_map.json, surgical undo)"
        ),
    )

    rb = sub.add_parser("rollback", help="Roll back one specific phase from target")
    rb.add_argument(
        "--phase", type=int, required=True, choices=[1, 2, 3],
        help="Phase number to roll back",
    )

    mig_p = sub.add_parser(
        "migrate",
        help=(
            "Safe migration: backup target → format → import from source "
            "(skips format if target is already empty)"
        ),
    )
    # Mirror the user-volume flags from `run` so a single `migrate`
    # invocation can also chunk the user import safely.
    mig_p.add_argument(
        "--max-users", type=int, default=None,
        help="Cap how many users phase 5 will create (see `run --help`).",
    )
    mig_p.add_argument(
        "--users-from", type=int, default=0,
        help="Skip the first N source users before applying --max-users.",
    )
    mig_p.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the suspension-risk confirmation prompt in phase 5.",
    )

    rp = sub.add_parser(
        "restore",
        help="Restore target account from a previous backup",
    )
    rp.add_argument(
        "--path", type=str, default=None,
        help="Path to a specific backup directory (omit to list available backups)",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Ensure the project's writable folders exist before any command runs.
    # On a fresh checkout (or if the user deleted state/exports while
    # debugging) the tool would silently mkdir these and hide the mistake
    # of running from the wrong working directory. We now ask first.
    from src.utils.paths import ensure_dirs
    config_dir = Path(getattr(args, "source", "config/source.env")).parent
    target_cfg_dir = Path(getattr(args, "target", "config/target.env")).parent
    required_dirs = {
        config_dir,
        target_cfg_dir,
        ROOT / "exports",
        ROOT / "state",
    }
    if not ensure_dirs(sorted(required_dirs, key=str), project_root=ROOT):
        sys.exit(1)

    COMMANDS = {
        "pre-flight":    cmd_preflight,
        "format-target": cmd_format_target,
        "run":           cmd_run,
        "migrate":       cmd_migrate,
        "restore":       cmd_restore,
        "verify":        cmd_verify,
        "cleanup":       cmd_cleanup,
        "rollback":      cmd_rollback,
    }

    handler = COMMANDS.get(args.command)
    if handler is None:
        logger.error(f"Unknown command: {args.command}")
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
