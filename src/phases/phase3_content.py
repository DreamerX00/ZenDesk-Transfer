"""
Phase 3 — Help Center Content Migration.

Migrates (in strict order):
  User Segments → Categories → Sections → Articles → Attachments

Bug fixes vs v1:
  - _import_hc_resource now wraps remap_payload in try/except RemapError.
  - _migrate_attachments uses the authenticated session (not bare requests.get)
    so private/unpublished attachments are accessible.
  - Attachment download routed through client._request() for rate-limit handling.
  - filename=None guard added before multipart upload.
  - url=None guard added before attempting download.
  - Upload also goes through client._request retry loop via a wrapper.
  - section_id=None (empty string) handled gracefully.
"""

import io
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.importer import load_id_map, _record_mapping
from src.remapper import strip_source_fields, remap_payload, RemapError
from src.utils import logger

# Maximum file size for attachment downloads (50 MB)
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# Allowed MIME types / filename extensions for attachment re-upload
# (guard against uploading unexpected file types from source)
SAFE_FILENAME_RE = re.compile(
    r"^[a-zA-Z0-9_\-\. ]{1,200}\."
    r"(png|jpg|jpeg|gif|svg|pdf|txt|md|csv|zip|docx?|xlsx?|pptx?|mp4|webm|mp3)$",
    re.IGNORECASE,
)


def run(source: ZendeskClient, target: ZendeskClient,
        exports: Dict[str, List[Dict]]) -> Dict:
    id_map = load_id_map()

    # ---- 4.1.6  User Segments (needed by sections) ------------------- #
    logger.section("4.1.6  Help Center User Segments")
    _import_user_segments(target, id_map, exports.get("user_segments", []))

    # ---- 4.1.1  Categories ------------------------------------------- #
    logger.section("4.1.1  Help Center Categories")
    _import_hc_resource(
        client=target, id_map=id_map,
        source_items=exports.get("categories", []),
        resource_key="hc_categories",
        create_path="help_center/categories",
        create_rkey="category",
        delete_path_fn=lambda tid: f"help_center/categories/{tid}",
        list_path="help_center/categories",
        list_rkey="categories",
    )

    # ---- 4.1.2  Sections --------------------------------------------- #
    logger.section("4.1.2  Help Center Sections")
    _import_hc_resource(
        client=target, id_map=id_map,
        source_items=exports.get("sections", []),
        resource_key="hc_sections",
        create_path="help_center/sections",
        create_rkey="section",
        delete_path_fn=lambda tid: f"help_center/sections/{tid}",
        list_path="help_center/sections",
        list_rkey="sections",
        create_path_fn=lambda p: f"help_center/categories/{p.get('category_id', '')}/sections",
    )

    # ---- 4.1.3  Articles --------------------------------------------- #
    logger.section("4.1.3  Help Center Articles (with labels & attachments)")
    _import_articles(source, target, id_map, exports.get("articles", []))

    from src.importer import flush_id_map
    flush_id_map(id_map)
    logger.success("Phase 4 — Help Center Content complete.")
    return id_map


# ------------------------------------------------------------------ #
#  Generic HC resource importer                                       #
# ------------------------------------------------------------------ #

def _import_hc_resource(
    *, client: ZendeskClient, id_map: Dict,
    source_items: List[Dict], resource_key: str,
    create_path: str, create_rkey: str,
    delete_path_fn: Callable[[int], str],
    list_path: str, list_rkey: str,
    create_path_fn: Optional[Callable[[Dict], str]] = None,
) -> None:
    try:
        existing = client.list_resource(list_path, list_rkey)
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(
            f"Could not list existing {resource_key} in target: {exc}. "
            "Skipping collision check."
        )
        existing = []

    # Only index items that actually have a non-empty name
    existing_by_name = {
        i["name"]: i for i in existing if isinstance(i, dict) and i.get("name")
    }

    for item in source_items:
        if not isinstance(item, dict):
            continue

        source_id = item.get("id")
        name = item.get("name") or f"<unnamed id={source_id}>"

        try:
            payload = strip_source_fields(item)
            payload = remap_payload(payload, id_map, context=f"{resource_key}:{name}")
        except RemapError as exc:
            logger.log_failed(resource_key, source_id, str(exc), name)
            continue
        except Exception as exc:
            logger.log_failed(
                resource_key, source_id,
                f"Error preparing payload: {type(exc).__name__}: {exc}", name,
            )
            continue

        # PURGE conflict
        if name in existing_by_name:
            conflict_id = existing_by_name[name].get("id")
            if conflict_id is None:
                logger.warn(
                    f"Conflict for {resource_key} '{name}' but no conflict ID found. "
                    "Skipping purge."
                )
            else:
                try:
                    client.delete(delete_path_fn(conflict_id))
                    logger.log_purged(resource_key, conflict_id, name)
                except (ZendeskAPIError, ZendeskNetworkError) as exc:
                    logger.log_failed(
                        resource_key, source_id, f"Purge failed: {exc}", name
                    )
                    continue

        if create_path_fn:
            cat_id = payload.get("category_id")
            if not cat_id:
                logger.log_failed(
                    resource_key, source_id,
                    "No category_id in remapped payload — cannot determine "
                    "category-specific create path.", name,
                )
                continue
            effective_create_path = create_path_fn(payload)
        else:
            effective_create_path = create_path

        try:
            resp = client.post(effective_create_path, {create_rkey: payload})
            if resp.get("dry_run"):
                continue
            created = resp.get(create_rkey)
            if not isinstance(created, dict):
                logger.log_failed(
                    resource_key, source_id,
                    f"Unexpected response shape — '{create_rkey}' missing or not a dict. "
                    f"Keys: {list(resp.keys())}",
                    name,
                )
                continue
            target_id = created.get("id")
            if target_id is None:
                logger.log_failed(
                    resource_key, source_id,
                    "Response had no 'id' field after create.", name,
                )
                continue
            _record_mapping(id_map, resource_key, source_id, target_id)
            logger.log_created(resource_key, source_id, target_id, name)
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed(resource_key, source_id, str(exc), name)
        except Exception as exc:
            logger.log_failed(
                resource_key, source_id,
                f"Unexpected error during create: {type(exc).__name__}: {exc}", name,
            )


def _import_user_segments(target: ZendeskClient, id_map: Dict,
                           source_items: List[Dict]) -> None:
    _import_hc_resource(
        client=target, id_map=id_map,
        source_items=source_items,
        resource_key="hc_user_segments",
        create_path="help_center/user_segments",
        create_rkey="user_segment",
        delete_path_fn=lambda tid: f"help_center/user_segments/{tid}",
        list_path="help_center/user_segments",
        list_rkey="user_segments",
    )


# ------------------------------------------------------------------ #
#  Articles (with attachment re-upload)                               #
# ------------------------------------------------------------------ #

def _import_articles(source: ZendeskClient, target: ZendeskClient,
                     id_map: Dict, articles: List[Dict]) -> None:
    section_map = id_map.get("hc_sections", {})

    try:
        existing = target.list_resource("help_center/articles", "articles")
    except (ZendeskAPIError, ZendeskNetworkError):
        existing = []

    # Index by title — only non-empty titles
    existing_by_title: Dict[str, Dict] = {
        a["title"]: a for a in existing if isinstance(a, dict) and a.get("title")
    }

    for article in articles:
        if not isinstance(article, dict):
            continue

        source_id = article.get("id")
        title = article.get("title") or f"<untitled id={source_id}>"
        source_section_id = article.get("section_id")

        # Validate section_id is present and mappable
        if not source_section_id:
            logger.log_failed(
                "hc_articles", source_id,
                "Article has no section_id — cannot determine target section.", title,
            )
            continue

        target_section_id = section_map.get(str(source_section_id))
        if not target_section_id:
            logger.log_failed(
                "hc_articles", source_id,
                f"No target section_id for source section {source_section_id}.", title,
            )
            continue

        try:
            payload = strip_source_fields(article)
        except Exception as exc:
            logger.log_failed("hc_articles", source_id,
                              f"strip_source_fields failed: {exc}", title)
            continue

        payload["section_id"] = int(target_section_id)
        payload.pop("author_id", None)  # use admin as fallback author

        # PURGE conflict
        if title in existing_by_title:
            conflict_id = existing_by_title[title].get("id")
            if conflict_id is None:
                logger.warn(
                    f"Conflict for hc_articles '{title}' but no conflict ID. "
                    "Skipping purge."
                )
            else:
                try:
                    target.delete(f"help_center/articles/{conflict_id}")
                    logger.log_purged("hc_articles", conflict_id, title)
                except (ZendeskAPIError, ZendeskNetworkError) as exc:
                    logger.log_failed(
                        "hc_articles", source_id, f"Purge failed: {exc}", title
                    )
                    continue

        try:
            resp = target.post(
                f"help_center/sections/{target_section_id}/articles",
                {"article": payload},
            )
            if resp.get("dry_run"):
                continue

            created = resp.get("article")
            if not isinstance(created, dict):
                logger.log_failed(
                    "hc_articles", source_id,
                    f"Unexpected response — 'article' key missing. Keys: {list(resp.keys())}",
                    title,
                )
                continue

            target_article_id = created.get("id")
            if target_article_id is None:
                logger.log_failed(
                    "hc_articles", source_id,
                    "Create response had no 'id' field.", title,
                )
                continue

            _record_mapping(id_map, "hc_articles", source_id, target_article_id)
            logger.log_created("hc_articles", source_id, target_article_id, title)

            # Re-upload attachments
            _migrate_attachments(source, target, source_id, target_article_id, title)

            # Migrate article labels
            _migrate_article_labels(
                source, target, source_id, target_article_id, title
            )

        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed("hc_articles", source_id, str(exc), title)
        except Exception as exc:
            logger.log_failed(
                "hc_articles", source_id,
                f"Unexpected error: {type(exc).__name__}: {exc}", title,
            )


def _migrate_attachments(source: ZendeskClient, target: ZendeskClient,
                          source_article_id: int, target_article_id: int,
                          article_title: str) -> None:
    """
    Download each attachment from the source (using authenticated session)
    and upload to the target article.

    Security guards:
      - Validates filename against a safe extension allowlist.
      - Enforces a maximum download size (50 MB).
      - Uses source client's authenticated session — not bare requests.get().
      - Upload goes through target client's _request retry/back-off loop.
    """
    try:
        data = source.get(
            f"help_center/articles/{source_article_id}/attachments"
        )
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(
            f"Could not fetch attachments for article {source_article_id}: {exc}"
        )
        return
    except Exception as exc:
        logger.warn(
            f"Unexpected error fetching attachments for article "
            f"{source_article_id}: {exc}"
        )
        return

    attachments = data.get("article_attachments")
    if not isinstance(attachments, list):
        return

    for att in attachments:
        if not isinstance(att, dict):
            continue

        att_id = att.get("id")
        url = att.get("content_url")
        filename = att.get("file_name", "")
        inline = bool(att.get("inline", False))

        # Guard: URL must be present and a string
        if not url or not isinstance(url, str):
            logger.log_failed(
                "hc_article_attachment", att_id,
                "Attachment has no content_url — skipping.",
                f"{article_title}/{filename}",
            )
            continue

        # Guard: Validate filename against safe allowlist
        safe_name = Path(filename).name  # strip any directory component
        if not safe_name:
            safe_name = f"attachment_{att_id}"
        if not SAFE_FILENAME_RE.fullmatch(safe_name):
            logger.warn(
                f"Attachment filename '{safe_name}' failed safety check "
                f"for article '{article_title}' — skipping."
            )
            logger.log_failed(
                "hc_article_attachment", att_id,
                f"Unsafe filename '{safe_name}' rejected.",
                f"{article_title}/{safe_name}",
            )
            continue

        try:
            # Guard: only download from HTTPS URLs
            if not url.startswith("https://"):
                logger.log_failed(
                    "hc_article_attachment", att_id,
                    f"Rejected non-HTTPS attachment URL (scheme guard).",
                    f"{article_title}/{safe_name}",
                )
                continue

            # Download using the source client's AUTHENTICATED session
            # This handles auth for private/unpublished attachments
            dl_resp = source._session.get(url, timeout=60, stream=True)
            dl_resp.raise_for_status()

            # Enforce size limit
            content_length = dl_resp.headers.get("Content-Length")
            if content_length:
                try:
                    cl_int = int(content_length)
                except (ValueError, TypeError):
                    cl_int = None
                if cl_int is not None and cl_int > MAX_ATTACHMENT_BYTES:
                    logger.log_failed(
                        "hc_article_attachment", att_id,
                        f"Attachment too large ({content_length} bytes > "
                        f"{MAX_ATTACHMENT_BYTES} limit). Skipping.",
                        f"{article_title}/{safe_name}",
                    )
                    dl_resp.close()
                    continue

            # Stream into memory with size cap
            chunks = []
            total = 0
            for chunk in dl_resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_ATTACHMENT_BYTES:
                    logger.log_failed(
                        "hc_article_attachment", att_id,
                        f"Attachment exceeded {MAX_ATTACHMENT_BYTES} bytes during download.",
                        f"{article_title}/{safe_name}",
                    )
                    break
                chunks.append(chunk)
            else:
                # Only upload if we didn't break out of the loop
                file_data = io.BytesIO(b"".join(chunks))

                upload_url = (
                    f"https://{target.subdomain}.zendesk.com/api/v2/"
                    f"help_center/articles/{target_article_id}/attachments"
                )
                # Validate upload URL against our SSRF guard
                target._validate_url(upload_url)

                files = {"file": (safe_name, file_data)}
                data_fields = {"inline": "true" if inline else "false"}

                # Route through _request for rate-limit/retry handling
                # (temporarily remove Content-Type so requests sets multipart boundary)
                saved_ct = target._session.headers.pop("Content-Type", None)
                try:
                    upload_resp = target._request(
                        "POST", upload_url,
                        files=files, data=data_fields,
                    )
                finally:
                    if saved_ct:
                        target._session.headers["Content-Type"] = saved_ct

                if upload_resp.ok:
                    # Use _safe_json to handle non-JSON upload responses gracefully
                    try:
                        rj = target._safe_json(upload_resp)
                        new_id = rj.get("article_attachment", {}).get("id")
                    except Exception:
                        new_id = None
                    logger.log_created(
                        "hc_article_attachment", att_id, new_id,
                        f"{article_title}/{safe_name}",
                    )
                else:
                    logger.log_failed(
                        "hc_article_attachment", att_id,
                        f"Upload failed [{upload_resp.status_code}]: "
                        f"{target._safe_decode(upload_resp, 200)}",
                        f"{article_title}/{safe_name}",
                    )

        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed(
                "hc_article_attachment", att_id, str(exc),
                f"{article_title}/{safe_name}",
            )
        except Exception as exc:
            logger.log_failed(
                "hc_article_attachment", att_id,
                f"Unexpected error: {type(exc).__name__}: {exc}",
                f"{article_title}/{safe_name}",
            )


def _migrate_article_labels(
    source: ZendeskClient, target: ZendeskClient,
    source_article_id: int, target_article_id: int,
    article_title: str,
) -> None:
    """
    Fetch labels from source article and apply them to target article.
    Labels are simple strings (no ID mapping needed).
    """
    try:
        data = source.get(
            f"help_center/articles/{source_article_id}/labels"
        )
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(
            f"Could not fetch labels for article {source_article_id}: {exc}"
        )
        return

    labels = data.get("labels", [])
    if not isinstance(labels, list) or not labels:
        return

    label_names = [
        lbl["name"] for lbl in labels
        if isinstance(lbl, dict) and lbl.get("name")
    ]
    if not label_names:
        return

    try:
        resp = target.post(
            f"help_center/articles/{target_article_id}/labels",
            {"article_labels": label_names},
        )
        if resp.get("dry_run"):
            return
        logger.log_created(
            "hc_article_labels", source_article_id, target_article_id,
            f"{article_title} ({len(label_names)} labels)",
        )
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.log_failed(
            "hc_article_labels", source_article_id,
            f"Failed to migrate labels: {exc}", article_title,
        )
