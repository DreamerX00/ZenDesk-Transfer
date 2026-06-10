"""
Extractor — fetches every configuration resource from the SOURCE account
and saves raw JSON snapshots to the exports/ directory.

Bug fixes vs v1:
  - _save() now has full exception handling (disk full, permissions).
  - load_export() handles JSONDecodeError on corrupted files.
  - Extract continues on individual resource failures rather than crashing.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.utils import logger

EXPORTS_DIR = Path(__file__).parent.parent / "exports"

# (resource_key, api_path, export_filename)
RESOURCES = [
    # Foundation layer
    ("groups",                "groups",                          "groups.json"),
    ("brands",                "brands",                          "brands.json"),
    ("ticket_fields",         "ticket_fields",                   "ticket_fields.json"),
    ("user_fields",           "user_fields",                     "user_fields.json"),
    ("organization_fields",   "organization_fields",             "organization_fields.json"),
    ("custom_roles",          "custom_roles",                    "custom_roles.json"),
    ("ticket_forms",          "ticket_forms",                    "ticket_forms.json"),
    ("organizations",         "organizations",                   "organizations.json"),
    # Business logic layer
    ("views",                 "views",                           "views.json"),
    ("triggers",              "triggers",                        "triggers.json"),
    ("automations",           "automations",                     "automations.json"),
    ("macros",                "macros",                          "macros.json"),
    ("sla_policies",          "slas/policies",                   "sla_policies.json"),
    ("group_sla_policies",    "group_slas/policies",             "group_sla_policies.json"),
    ("schedules",             "business_hours/schedules",        "schedules.json"),
    ("routing_attributes",    "routing/attributes",              "routing_attributes.json"),
    ("dynamic_content_items", "dynamic_content/items",           "dynamic_content.json"),
    ("webhooks",              "webhooks",                        "webhooks.json"),
    # Help Center
    ("categories",            "help_center/categories",          "hc_categories.json"),
    ("sections",              "help_center/sections",            "hc_sections.json"),
    ("articles",              "help_center/articles",            "hc_articles.json"),
    ("user_segments",         "help_center/user_segments",       "hc_user_segments.json"),
    # Users
    ("users",                 "users",                           "users.json"),
    # Help Center Themes (JSON metadata only; ZIP is exported separately)
    ("themes",                "guide/theming/themes",            "hc_themes.json"),
]

# Resources to skip silently if the account plan doesn't support them
PLAN_GATED = {"custom_roles", "group_sla_policies", "routing_attributes"}


def extract_all(client: ZendeskClient) -> Dict[str, List[Dict]]:
    """
    Fetch all resources from source and write them to exports/.
    Returns a dict keyed by resource_key for downstream use.
    Individual resource failures are logged and do NOT abort the entire run.
    """
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result: Dict[str, List[Dict]] = {}

    logger.section("Extracting source account resources")

    for resource_key, api_path, filename in RESOURCES:
        try:
            items = list(client.get_all(api_path, resource_key))
            # Validate that we received a list of dicts
            items = [i for i in items if isinstance(i, dict)]
            result[resource_key] = items
            _save(filename, items)
            logger.info(f"  Exported {len(items):>4}  {resource_key}")

        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            if resource_key in PLAN_GATED:
                logger.log_skipped(
                    resource_key, "N/A",
                    f"Plan restriction or endpoint unavailable: {exc}"
                )
            else:
                logger.log_failed(resource_key, "N/A", str(exc))
            result[resource_key] = []

        except Exception as exc:
            # Unexpected error — log and continue rather than crash
            logger.log_failed(
                resource_key, "N/A",
                f"Unexpected error during extraction: {type(exc).__name__}: {exc}"
            )
            result[resource_key] = []

    # Export the live help center theme as a ZIP file
    _export_live_theme(client, result)

    return result


def load_export(filename: str) -> List[Dict]:
    """
    Load a previously saved export file.
    Returns empty list if file doesn't exist or is malformed.
    """
    # Sanitize filename — only allow safe characters, no path traversal
    safe_name = Path(filename).name  # strips any directory component
    path = EXPORTS_DIR / safe_name

    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warn(f"Export file '{filename}' did not contain a list — skipping.")
            return []
        return [i for i in data if isinstance(i, dict)]
    except json.JSONDecodeError as exc:
        logger.warn(f"Export file '{filename}' is malformed JSON ({exc}) — skipping.")
        return []
    except OSError as exc:
        logger.warn(f"Could not read export file '{filename}': {exc}")
        return []


def _export_live_theme(
    client: "ZendeskClient",
    result: Dict[str, List[Dict]],
) -> None:
    """
    Export the live help center theme from the source as a ZIP file.
    Saves the ZIP to exports/theme_live.zip and records metadata in result.
    The live theme is determined by checking the 'live' flag in the themes list.
    """
    themes = result.get("themes", [])
    if not themes:
        return

    import io as _io
    import zipfile as _zipfile

    live_theme = None
    for t in themes:
        if t.get("live") is True:
            live_theme = t
            break

    if not live_theme:
        logger.info("  No live theme found in source — skipping theme ZIP export.")
        return

    theme_id = str(live_theme.get("id", ""))
    theme_name = live_theme.get("name", "unknown")
    logger.info(f"  Exporting live theme '{theme_name}' (id={theme_id})...")

    try:
        zip_data = client.export_theme(theme_id)
    except Exception as exc:
        logger.log_failed("themes", theme_id, f"Export failed: {exc}", theme_name)
        return

    if not zip_data:
        logger.log_failed("themes", theme_id, "Export returned empty ZIP data", theme_name)
        return

    # Validate it's a real ZIP before saving
    try:
        with _zipfile.ZipFile(_io.BytesIO(zip_data)) as zf:
            if zf.testzip():
                logger.log_failed("themes", theme_id, "Exported ZIP is corrupt", theme_name)
                return
    except Exception as exc:
        logger.log_failed("themes", theme_id, f"Invalid ZIP data: {exc}", theme_name)
        return

    logger.info(f"  Theme ZIP size: {len(zip_data)} bytes")
    zip_filename = f"theme_{theme_id}.zip"
    fd, tmp_path = tempfile.mkstemp(dir=EXPORTS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(zip_data)
        os.replace(tmp_path, EXPORTS_DIR / zip_filename)
        logger.info(f"  Exported theme ZIP  {theme_name} → {zip_filename}")
        result.setdefault("theme_zips", {})[theme_id] = zip_filename
    except OSError as exc:
        logger.log_failed("themes", theme_id, f"Failed to save theme ZIP: {exc}", theme_name)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _save(filename: str, data: Any) -> None:
    """
    Atomically save data to an export file.
    Uses temp file + rename to prevent partial writes.
    """
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORTS_DIR / filename
    fd, tmp_path = tempfile.mkstemp(dir=EXPORTS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as exc:
        # Covers OSError (disk full), TypeError (non-serializable), etc.
        # Always clean up the temp file so we don't litter the exports dir.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise OSError(f"Failed to save export '{filename}': {exc}") from exc
