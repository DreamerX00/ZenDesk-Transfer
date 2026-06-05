"""
Backup & Restore — snapshot target resources before destructive operations.

- backup() saves every user-created resource from the target to
  backups/{timestamp}/ so the account state can be recovered later.
- restore() loads a backup directory and recreates resources in
  parent-first (creation) order — best-effort, no ID remapping.

Usage (via main.py):
  python main.py migrate          → backup → format → migrate
  python main.py restore [--path] → load backup → recreate
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.extractor import RESOURCES  # shared resource definitions
from src.remapper import strip_source_fields
from src.utils import logger

BACKUPS_DIR = Path(__file__).parent.parent / "backups"

# Zendesk API expects the singular form as the JSON wrapper key on create.
# Map plural resource_key → singular create key.
CREATE_RKEY: Dict[str, str] = {
    "groups":                "group",
    "brands":                "brand",
    "ticket_fields":         "ticket_field",
    "user_fields":           "user_field",
    "organization_fields":   "organization_field",
    "custom_roles":          "custom_role",
    "ticket_forms":          "ticket_form",
    "organizations":         "organization",
    "user_segments":         "user_segment",
    "categories":            "category",
    "sections":              "section",
    "articles":              "article",
    "views":                 "view",
    "triggers":              "trigger",
    "automations":           "automation",
    "macros":                "macro",
    "sla_policies":          "sla_policy",
    "group_sla_policies":    "group_sla_policy",
    "schedules":             "schedule",
    "routing_attributes":    "routing_attribute",
    "dynamic_content_items": "dynamic_content_item",
    "webhooks":              "webhook",
    "themes":                "theme",
}

# System ticket-field types that exist in every account and cannot be re-created.
SYSTEM_TICKET_FIELD_TYPES = frozenset({
    "subject", "description", "status", "priority", "type",
    "assignee", "group", "requester", "organization", "tags",
    "ticket_form_id", "brand_id", "due_at", "custom_status",
    "tickettype",
})

# Ticket-field keys that are managed by Zendesk AI/automation and can't be re-created.
SKIP_TICKET_FIELD_KEY_PREFIXES = (
    "standard::",
    "ml_triage_",
)

# Re-create in parent-first order (reverse of deletion order)
RESTORE_ORDER = [
    ("groups",                "groups"),
    ("brands",                "brands"),
    ("ticket_fields",         "ticket_fields"),
    ("user_fields",           "user_fields"),
    ("organization_fields",   "organization_fields"),
    ("custom_roles",          "custom_roles"),
    ("ticket_forms",          "ticket_forms"),
    ("organizations",         "organizations"),
    ("user_segments",         "help_center/user_segments"),
    ("categories",            "help_center/categories"),
    ("sections",              "help_center/sections"),
    ("articles",              "help_center/articles"),
    ("views",                 "views"),
    ("triggers",              "triggers"),
    ("automations",           "automations"),
    ("macros",                "macros"),
    ("sla_policies",          "slas/policies"),
    ("group_sla_policies",    "group_slas/policies"),
    ("schedules",             "business_hours/schedules"),
    ("routing_attributes",    "routing/attributes"),
    ("dynamic_content_items", "dynamic_content/items"),
    ("webhooks",              "webhooks"),
    ("themes",                "guide/theming/themes"),
]

SAFE_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


def backup(client: ZendeskClient) -> Optional[Path]:
    """
    Export all user-created resources from the target account into
    backups/{timestamp}/ and return the backup directory path.
    Returns None if nothing was exported (account already empty).
    """
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_dir = BACKUPS_DIR / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    logger.section(f"Backing up target account → {backup_dir}")

    total = 0
    for resource_key, api_path, filename in RESOURCES:
        try:
            items = list(client.get_all(api_path, resource_key))
            items = [i for i in items if isinstance(i, dict)]
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_skipped(resource_key, "N/A", f"Cannot list: {exc}")
            continue
        except Exception as exc:
            logger.warn(f"Unexpected error backing up {resource_key}: {exc}")
            continue

        if not items:
            continue

        _save_backup(backup_dir / filename, items)
        logger.info(f"  Backed up {len(items):>4}  {resource_key}")
        total += len(items)

        # For themes, also export the theme ZIPs
        if resource_key == "themes":
            total += _backup_themes(client, backup_dir, items)

    if total == 0:
        logger.success("Target account is empty — no backup needed.")
        return None

    # Write metadata
    _save_backup(
        backup_dir / "metadata.json",
        {
            "timestamp": ts,
            "resource_count": total,
            "tool_version": "zd-transfer",
        },
    )
    logger.success(f"Backup complete — {total} resources saved to {backup_dir}")
    return backup_dir


def list_backups() -> List[Path]:
    """Return backup directories sorted newest-first."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    candidates = sorted(BACKUPS_DIR.iterdir(), reverse=True)
    return [d for d in candidates if d.is_dir() and SAFE_TIMESTAMP_RE.match(d.name)]


def restore(client: ZendeskClient, backup_dir: Path) -> int:
    """
    Recreate resources from a backup into the target account.
    Runs in parent-first (creation) order — best-effort, no ID remapping.
    Returns the number of resources successfully created.
    """
    if not backup_dir.is_dir():
        logger.error(f"Backup directory not found: {backup_dir}")
        return 0

    logger.section(f"Restoring from backup → {backup_dir}")

    metadata_path = backup_dir / "metadata.json"
    if metadata_path.exists():
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            ts = meta.get("timestamp", "unknown")
            logger.info(f"Backup timestamp: {ts}")
        except (json.JSONDecodeError, OSError):
            pass

    created = 0
    for resource_key, api_path in RESTORE_ORDER:
        # Find the backup file for this resource
        filename = None
        for rk, _, fn in RESOURCES:
            if rk == resource_key:
                filename = fn
                break
        if not filename:
            continue

        filepath = backup_dir / filename
        if not filepath.exists():
            continue

        try:
            items = json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warn(f"Cannot read backup file {filename}: {exc}")
            continue

        if not isinstance(items, list) or not items:
            continue

        create_rkey = CREATE_RKEY.get(resource_key, resource_key)

        # Build name-based skip set for resources that can conflict
        existing_names: set = set()
        if resource_key in ("groups",):
            try:
                existing_items = client.get_all(api_path, resource_key)
                existing_names = {
                    e.get("name") for e in existing_items
                    if isinstance(e, dict) and e.get("name")
                }
            except (ZendeskAPIError, ZendeskNetworkError):
                pass

        logger.info(f"  Restoring {len(items)}  {resource_key}...")
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            item_name = item.get("name") or item.get("title") or f"id={item_id}"

            # Skip items whose name already exists in the target
            item_name_check = item.get("name") or item.get("title") or ""
            if item_name_check and item_name_check in existing_names:
                logger.log_skipped(resource_key, item_id,
                                   f"Name '{item_name_check}' already exists in target")
                continue

            # Skip system / built-in items that can't be re-created
            if resource_key == "ticket_fields":
                ftype = item.get("type")
                fkey = item.get("key") or ""
                if ftype in SYSTEM_TICKET_FIELD_TYPES:
                    logger.log_skipped(resource_key, item_id,
                                       f"System ticket field ({ftype}) — built into every account")
                    continue
                if fkey.startswith(SKIP_TICKET_FIELD_KEY_PREFIXES):
                    logger.log_skipped(resource_key, item_id,
                                       f"Zendesk-managed field (key={fkey}) — created automatically")
                    continue

            if resource_key == "user_segments" and item.get("built_in"):
                logger.log_skipped(resource_key, item_id, "Built-in user segment — skipped")
                continue

            if resource_key == "automations" and item.get("default"):
                logger.log_skipped(resource_key, item_id, "Default automation — replaced by Zendesk on create")
                continue

            # Strip server-managed fields
            payload = strip_source_fields(item)

            # Pre-process specific resource types
            if resource_key == "groups" and item.get("default"):
                logger.log_skipped(resource_key, item_id, "Default group — auto-created by Zendesk")
                continue

            if resource_key == "brands":
                if item.get("default"):
                    logger.log_skipped(resource_key, item_id, "Default brand — auto-created by Zendesk")
                    continue
                payload = _prepare_brand(payload)

            if resource_key == "ticket_fields":
                payload = _prepare_ticket_field(payload)

            if resource_key == "ticket_forms":
                payload = _prepare_ticket_form(payload)

            if resource_key == "custom_roles":
                payload = _prepare_custom_role(payload)

            try:
                resp = client.post(api_path, {create_rkey: payload})
                new_id = resp.get(create_rkey, {}).get("id") if isinstance(resp, dict) else None
                logger.log_created(resource_key, item_id, new_id, item_name)
                created += 1
            except ZendeskAPIError as exc:
                if exc.status_code == 402:
                    logger.log_skipped(resource_key, item_id,
                                       f"402 Payment Required — plan does not support this API")
                    continue
                logger.log_failed(resource_key, item_id, str(exc), item_name)
            except ZendeskNetworkError as exc:
                logger.log_failed(resource_key, item_id, str(exc), item_name)
            except Exception as exc:
                logger.log_failed(
                    resource_key, item_id,
                    f"Unexpected: {type(exc).__name__}: {exc}", item_name,
                )

    logger.success(f"Restore complete — {created} resources created.")
    return created


# ------------------------------------------------------------------ #
#  Internal helpers                                                   #
# ------------------------------------------------------------------ #


def _prepare_brand(payload: Dict) -> Dict:
    """Brands require globally unique subdomains — strip source subdomain."""
    payload = dict(payload)
    payload.pop("subdomain", None)
    payload.pop("host_mapping", None)
    payload.pop("help_center_state", None)
    payload.pop("has_help_center", None)
    payload.pop("ticket_form_ids", None)
    payload.pop("signature_template", None)
    brand_name = payload.get("name", "brand")
    slug = re.sub(r"[^a-z0-9-]", "", brand_name.lower().replace(" ", "-"))
    slug = slug[:40].strip("-")
    if not slug:
        slug = "brand"
    payload["subdomain"] = slug
    return payload


def _prepare_custom_role(payload: Dict) -> Dict:
    """Fix configuration values that the create API rejects."""
    payload = dict(payload)
    config = payload.get("configuration")
    if isinstance(config, dict):
        config = dict(config)
        # manage_roles: create API accepts "all-except-self" / "none"
        if config.get("manage_roles") in ("all", "full"):
            config["manage_roles"] = "all-except-self"
        if config.get("manage_roles") in ("readonly", "read"):
            config["manage_roles"] = "none"
        # manage_team_members: create API accepts "all-with-self-restriction" / "none" / "readonly"
        if config.get("manage_team_members") == "all":
            config["manage_team_members"] = "all-with-self-restriction"
        payload["configuration"] = config
    return payload


def _prepare_ticket_field(payload: Dict) -> Dict:
    """Strip nested collections that can't be created inline."""
    payload = dict(payload)
    payload.pop("custom_field_options", None)
    payload.pop("system_field_options", None)
    payload.pop("custom_statuses", None)
    return payload


def _prepare_ticket_form(payload: Dict) -> Dict:
    """Strip field IDs and conditions that reference other resources."""
    payload = dict(payload)
    payload.pop("ticket_field_ids", None)
    payload.pop("end_user_conditions", None)
    payload.pop("agent_conditions", None)
    payload.pop("restricted_brand_ids", None)
    return payload


def _backup_themes(
    client: ZendeskClient,
    backup_dir: Path,
    themes: List[Dict],
) -> int:
    """
    Export each non-system theme from the target as a ZIP file and save
    it to the backup directory. Returns the number of themes backed up.
    """
    count = 0
    for theme in themes:
        theme_id = str(theme.get("id", ""))
        if not theme_id:
            continue
        theme_name = theme.get("name", "unknown")
        try:
            zip_data = client.export_theme(theme_id)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed("themes", theme_id, f"Export failed: {exc}", theme_name)
            continue
        except Exception as exc:
            logger.log_failed("themes", theme_id, f"Export failed: {exc}", theme_name)
            continue
        if not zip_data:
            continue
        zip_filename = f"theme_{theme_id}.zip"
        try:
            (backup_dir / zip_filename).write_bytes(zip_data)
        except OSError as exc:
            logger.log_failed("themes", theme_id, f"Failed to save ZIP: {exc}", theme_name)
            continue
        logger.info(f"  Backed up theme ZIP  {theme_name} → {zip_filename}")
        count += 1
    return count


def _save_backup(path: Path, data: any) -> None:
    """Atomically write a backup file (temp + rename)."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
