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
from typing import Any, Dict, List, Optional

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
    ("trigger_categories",    "trigger_categories",              "trigger_categories.json"),
    ("triggers",              "triggers",                        "triggers.json"),
    ("automations",           "automations",                     "automations.json"),
    ("macros",                "macros",                          "macros.json"),
    ("sla_policies",          "slas/policies",                   "sla_policies.json"),
    ("group_sla_policies",    "group_slas/policies",             "group_sla_policies.json"),
    ("schedules",             "business_hours/schedules",        "schedules.json"),
    ("routing_attributes",    "routing/attributes",              "routing_attributes.json"),
    # Custom ticket statuses (Enterprise/Suite) — referenced by trigger/automation conditions.
    # Plan-gated: silently skipped on plans that don't support them.
    ("custom_statuses",       "custom_statuses",                 "custom_statuses.json"),
    ("dynamic_content_items", "dynamic_content/items",           "dynamic_content.json"),
    ("webhooks",              "webhooks",                        "webhooks.json"),
    # Help Center
    ("categories",            "help_center/categories",          "hc_categories.json"),
    ("sections",              "help_center/sections",            "hc_sections.json"),
    ("articles",              "help_center/articles",            "hc_articles.json"),
    # Article translations are fetched per-article in a dedicated step after
    # the article list is extracted (see _export_article_translations).
    ("user_segments",         "help_center/user_segments",       "hc_user_segments.json"),
    # HC permission groups control who can edit articles/sections.
    # Must be extracted before articles so permission_group_id can be remapped.
    ("permission_groups",     "guide/permission_groups",         "hc_permission_groups.json"),
    # Users
    ("users",                 "users",                           "users.json"),
    # Membership links (created in Phase 5, after users exist)
    ("group_memberships",        "group_memberships",            "group_memberships.json"),
    ("organization_memberships", "organization_memberships",     "organization_memberships.json"),
    # User identities (secondary emails, Twitter, etc.) — fetched per-user
    # in a dedicated step after the user list is extracted.
    # (see _export_user_identities)
    # Help Center Themes (JSON metadata only; ZIP is exported separately)
    ("themes",                "guide/theming/themes",            "hc_themes.json"),
]

# Resources to skip silently if the account plan doesn't support them
PLAN_GATED = {
    "custom_roles", "group_sla_policies", "routing_attributes",
    "permission_groups", "custom_statuses",
}


def extract_all(client: ZendeskClient,
                phases: Optional[set] = None) -> Dict[str, List[Dict]]:
    """
    Fetch all resources from source and write them to exports/.
    Returns a dict keyed by resource_key for downstream use.
    Individual resource failures are logged and do NOT abort the entire run.

    `phases` — the set of phase numbers the caller intends to run (e.g.
    {1, 2, 3}).  When supplied, expensive per-item sub-exports that are only
    needed by a specific phase are skipped if that phase is not in the set:

      - user identities   → only needed by Phase 5
      - article translations → only needed by Phase 3
      - dynamic content variants → only needed by Phase 2
      - routing attribute values → only needed by Phase 2
      - live theme ZIP    → only needed by Phase 3

    The flat RESOURCES list is always fetched in full because later phases
    depend on earlier ones' id_maps (e.g. Phase 2 needs ticket_fields from
    Phase 1).  Only the expensive *nested* sub-exports are gated.
    """
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result: Dict[str, List[Dict]] = {}

    # Determine which phases are wanted. None means "all".
    want_all = phases is None
    want_p2  = want_all or 2 in phases
    want_p3  = want_all or 3 in phases
    want_p5  = want_all or 5 in phases

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

    # Export routing attribute values (skills) — nested under each attribute.
    # Only needed by Phase 2. Skip if Phase 2 is not selected.
    if want_p2:
        _export_routing_attribute_values(client, result)

    # Export article translations — nested under each article.
    # Only needed by Phase 3. Skip if Phase 3 is not selected.
    if want_p3:
        _export_article_translations(client, result)

    # Export user identities (secondary emails, social logins) — nested per user.
    # Only needed by Phase 5. With 5000+ users this is the single most
    # expensive sub-export (~5000 API calls). Skip entirely when Phase 5
    # is not selected — this is the primary cause of the "stuck on extract"
    # symptom when users phase is not opted in.
    if want_p5:
        _export_user_identities(client, result)
    else:
        logger.info("  Skipping user_identities export (Phase 5 not selected)")

    # Export dynamic content locale variants — nested under each item.
    # Only needed by Phase 2. Skip if Phase 2 is not selected.
    if want_p2:
        _export_dynamic_content_variants(client, result)

    # Export the live help center theme as a ZIP file.
    # Only needed by Phase 3. Skip if Phase 3 is not selected.
    if want_p3:
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


def _export_article_translations(
    client: "ZendeskClient",
    result: Dict[str, List[Dict]],
) -> None:
    """
    Export all non-default-locale translations for each Help Center article.

    Parallelised with a thread pool — each article needs one GET, and with
    93 articles the sequential version took ~2 min at 200 RPM. The rate
    limiter in ZendeskClient is thread-safe so concurrent workers are fine.
    """
    articles = result.get("articles", [])
    if not articles:
        return

    import threading
    from concurrent.futures import ThreadPoolExecutor

    all_translations: List[Dict] = []
    lock = threading.Lock()

    def _fetch_one(article: Dict) -> None:
        article_id = article.get("id")
        if article_id is None:
            return
        default_locale = article.get("locale", "")
        try:
            translations = list(client.get_all(
                f"help_center/articles/{article_id}/translations",
                "translations",
            ))
            batch = []
            for t in translations:
                if not isinstance(t, dict):
                    continue
                if t.get("locale", "") == default_locale:
                    continue
                t["article_id"] = article_id
                batch.append(t)
            if batch:
                with lock:
                    all_translations.extend(batch)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(f"Could not fetch translations for article {article_id}: {exc}")
        except Exception as exc:
            logger.warn(f"Unexpected error fetching translations for article {article_id}: {exc}")

    workers = min(8, len(articles))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_fetch_one, articles))

    result["article_translations"] = all_translations
    if all_translations:
        _save("article_translations.json", all_translations)
        logger.info(f"  Exported {len(all_translations):>4}  article_translations")


def _export_dynamic_content_variants(
    client: "ZendeskClient",
    result: Dict[str, List[Dict]],
) -> None:
    """
    Export all non-default locale variants for each dynamic content item.
    Parallelised — each item needs one GET.
    """
    items = result.get("dynamic_content_items", [])
    if not items:
        return

    import threading
    from concurrent.futures import ThreadPoolExecutor

    all_variants: List[Dict] = []
    lock = threading.Lock()

    def _fetch_one(item: Dict) -> None:
        item_id = item.get("id")
        if item_id is None:
            return
        default_locale_id = item.get("default_locale_id")
        try:
            variants = list(client.get_all(
                f"dynamic_content/items/{item_id}/variants", "variants"
            ))
            batch = [v for v in variants
                     if isinstance(v, dict) and v.get("locale_id") != default_locale_id]
            for v in batch:
                v["item_id"] = item_id
            with lock:
                all_variants.extend(batch)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(f"Could not fetch variants for dynamic content item {item_id}: {exc}")
        except Exception as exc:
            logger.warn(f"Unexpected error fetching variants for DC item {item_id}: {exc}")

    workers = min(8, len(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_fetch_one, items))

    result["dynamic_content_variants"] = all_variants
    if all_variants:
        _save("dynamic_content_variants.json", all_variants)
        logger.info(f"  Exported {len(all_variants):>4}  dynamic_content_variants")


def _export_user_identities(
    client: "ZendeskClient",
    result: Dict[str, List[Dict]],
) -> None:
    """
    Export all non-primary user identities (secondary emails, phone, etc.)
    for each user.

    PERFORMANCE: with 5863 users this function makes 5863 API calls.
    Sequential execution at 200 RPM takes ~29 minutes. We parallelise with
    a thread pool — the ZendeskClient rate limiter is thread-safe and gates
    all workers to the account's RPM ceiling, so we get maximum throughput
    without exceeding the plan limit.

    Worker count is capped at 16 — enough to saturate the rate limiter's
    token bucket without spinning up thousands of threads for large accounts.
    """
    users = result.get("users", [])
    if not users:
        return

    import threading
    from concurrent.futures import ThreadPoolExecutor

    OAUTH_TYPES = frozenset({"twitter", "facebook", "google"})
    all_identities: List[Dict] = []
    manual_count_ref = [0]  # mutable int via list for closure
    lock = threading.Lock()

    # Only fetch for real users (skip system user id=1)
    target_users = [u for u in users if isinstance(u, dict)
                    and u.get("id") is not None and u.get("id") != 1]

    def _fetch_one(user: Dict) -> None:
        user_id = user.get("id")
        try:
            identities = list(client.get_all(
                f"users/{user_id}/identities", "identities"
            ))
            batch = []
            manual = 0
            for ident in identities:
                if not isinstance(ident, dict):
                    continue
                if ident.get("primary") and ident.get("type") == "email":
                    continue  # already on the user record
                if ident.get("type") in OAUTH_TYPES:
                    manual += 1
                    continue
                ident["user_id"] = user_id
                batch.append(ident)
            with lock:
                all_identities.extend(batch)
                manual_count_ref[0] += manual
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(f"Could not fetch identities for user {user_id}: {exc}")
        except Exception as exc:
            logger.warn(f"Unexpected error fetching identities for user {user_id}: {exc}")

    workers = min(16, len(target_users))
    logger.info(f"  Fetching identities for {len(target_users)} users "
                f"({workers} parallel workers)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_fetch_one, target_users))

    result["user_identities"] = all_identities
    if all_identities:
        _save("user_identities.json", all_identities)
        logger.info(f"  Exported {len(all_identities):>4}  user_identities")
    if manual_count_ref[0]:
        logger.warn(
            f"  {manual_count_ref[0]} OAuth-backed user identities (Twitter/Facebook/Google) "
            "cannot be migrated — users must re-link them manually."
        )


def _export_routing_attribute_values(
    client: "ZendeskClient",
    result: Dict[str, List[Dict]],
) -> None:
    """
    Export the skill option values for each routing attribute.

    Zendesk stores routing attribute values (the discrete skill options, e.g.
    "Spanish", "Billing") at /routing/attributes/{id}/values — a sub-resource
    that is not returned by the top-level /routing/attributes list. Without
    these, agents on the target have no skills to assign even after the
    attribute definitions are migrated.

    Each value record is augmented with its parent `attribute_id` so the
    Phase 2 importer can create them under the correct target attribute.
    """
    attributes = result.get("routing_attributes", [])
    if not attributes:
        return

    all_values: List[Dict] = []
    for attr in attributes:
        attr_id = attr.get("id")
        if attr_id is None:
            continue
        try:
            values = list(client.get_all(
                f"routing/attributes/{attr_id}/values", "attribute_values"
            ))
            for v in values:
                if isinstance(v, dict):
                    v["attribute_id"] = attr_id  # stash parent for importer
                    all_values.append(v)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(
                f"Could not fetch values for routing attribute {attr_id}: {exc}"
            )
        except Exception as exc:
            logger.warn(
                f"Unexpected error fetching routing attribute {attr_id} values: {exc}"
            )

    result["routing_attribute_values"] = all_values
    if all_values:
        _save("routing_attribute_values.json", all_values)
        logger.info(f"  Exported {len(all_values):>4}  routing_attribute_values")


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
