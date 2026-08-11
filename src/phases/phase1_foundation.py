"""
Phase 1 — Foundation Layer Migration.

Migrates (in strict dependency order):
  Groups → Brands → Ticket Fields → User Fields → Org Fields →
  Custom Roles → Ticket Forms → Organizations

Bug fixes vs v1:
  - skip_system_ticket_field return type uses Optional[Dict] instead of
    Python 3.10+ Dict | None union syntax for 3.8/3.9 compatibility.
"""

import re
from typing import Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.importer import (
    import_resource, load_id_map, _record_mapping, restore_positions,
)
from src.remapper import (
    strip_source_fields,
    remap_payload,
    sanitize_custom_field_values,
    RemapError,
)
from src.utils import logger

SYSTEM_TYPES = {
    "subject", "description", "status", "custom_status",
    "priority", "tickettype", "type",
    "assignee", "group", "requester", "organization", "tags",
    "ticket_form_id", "brand_id", "due_at",
}


def skip_system_ticket_field(item: Dict,
                              _id_map: Dict) -> Optional[Dict]:
    """Skip built-in system fields — they already exist in every account.

    Field `type` values are the ones the Zendesk API actually returns: the
    "Type" field is ``tickettype`` (not ``type``) and the "Ticket status"
    field is ``custom_status``. Both are system fields that cannot be created
    via POST /ticket_fields, so they must be skipped here — otherwise the
    importer tries to create them and logs a spurious failure.
    """
    if item.get("type") in SYSTEM_TYPES:
        return None
    return item


def prepare_ticket_field(item: Dict, _id_map: Dict) -> Optional[Dict]:
    """
    Skip system fields and strip nested collections that the create API
    rejects inline. custom_field_options (dropdown/checkbox choices) are
    stashed under the private key `_custom_field_options` so the importer's
    _create_one hook can PUT them after the field is created — Zendesk
    ignores options on the initial POST but accepts them on a subsequent PUT.

    system_field_options (e.g. tagger/multiselect field-choice definitions)
    and custom_statuses are also stashed under `_system_field_options` /
    `_custom_statuses` so they can be restored after create — they have the
    same POST-ignores, PUT-accepts limitation as custom_field_options.

    Source-specific `id` values in each option are stripped because they
    reference option IDs that do not exist on the target. Sending them
    causes Zendesk to reject the entire options update (422), leaving the
    field with no options and breaking conditional form rules
    (agent_conditions / end_user_conditions) that reference option values.
    Read-only fields `raw_name` and `raw_value` are also stripped.
    """
    item = skip_system_ticket_field(item, _id_map)
    if item is None:
        return None
    item = dict(item)
    options = item.pop("custom_field_options", None)
    system_options = item.pop("system_field_options", None)
    custom_statuses = item.pop("custom_statuses", None)

    if options:
        cleaned = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            cleaned_opt = {
                k: v for k, v in opt.items()
                if k not in ("id", "raw_name", "raw_value")
            }
            cleaned.append(cleaned_opt)
        item["_custom_field_options"] = cleaned

    if system_options:
        cleaned = []
        for opt in system_options:
            if not isinstance(opt, dict):
                continue
            cleaned_opt = {
                k: v for k, v in opt.items()
                if k not in ("id", "raw_name", "raw_value")
            }
            cleaned.append(cleaned_opt)
        item["_system_field_options"] = cleaned

    if custom_statuses:
        cleaned = []
        for cs in custom_statuses:
            if not isinstance(cs, dict):
                continue
            cleaned_cs = {
                k: v for k, v in cs.items()
                if k not in ("id", "url", "created_at", "updated_at")
            }
            cleaned.append(cleaned_cs)
        item["_custom_statuses"] = cleaned

    return item


def prepare_user_field(item: Dict, _id_map: Dict) -> Optional[Dict]:
    """
    Pre-process user/org fields that use dropdown/checkbox/tagger types.

    Zendesk ignores `custom_field_options` and `system_field_options` on the
    initial field creation POST but accepts them on a subsequent PUT. We pop
    them from the payload and stash them under `_custom_field_options` /
    `_system_field_options` so the importer's _create_one hook can apply them
    after the field exists on the target.

    Source-specific `id` values in each option are stripped (they reference
    option IDs that don't exist on the target). Sending them would cause
    Zendesk to reject the entire options update (422), leaving the field
    with no options.
    """
    item = dict(item)

    options = item.pop("custom_field_options", None)
    if options:
        cleaned = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            cleaned_opt = {
                k: v for k, v in opt.items()
                if k not in ("id", "raw_name", "raw_value")
            }
            cleaned.append(cleaned_opt)
        item["_custom_field_options"] = cleaned

    system_options = item.pop("system_field_options", None)
    if system_options:
        cleaned = []
        for opt in system_options:
            if not isinstance(opt, dict):
                continue
            cleaned_opt = {
                k: v for k, v in opt.items()
                if k not in ("id", "raw_name", "raw_value")
            }
            cleaned.append(cleaned_opt)
        item["_system_field_options"] = cleaned

    return item


def run(source: ZendeskClient, target: ZendeskClient,
        exports: Dict[str, List[Dict]]) -> Dict:
    id_map = load_id_map()

    # ---- 2.1  Groups ------------------------------------------------- #
    logger.section("2.1  Groups")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("groups", []),
        resource_key="groups",
        list_path="groups", list_rkey="groups",
        create_path="groups", create_rkey="group",
        create_response_rkey="group",
        delete_path_fn=lambda tid: f"groups/{tid}",
    )

    # ---- 2.2  Brands ------------------------------------------------- #
    logger.section("2.2  Brands")

    def _make_brand_preprocess(target_subdomain: str):
        def _prepare_brand(item: Dict, _id_map: Dict) -> Dict:
            """
            Brands require globally unique subdomains at create time.
            Strip source subdomain and generate a new unique one from
            the brand name + target account subdomain.
            """
            item = dict(item)
            item.pop("subdomain", None)
            item.pop("host_mapping", None)
            item.pop("help_center_state", None)
            item.pop("has_help_center", None)
            item.pop("ticket_form_ids", None)
            item.pop("signature_template", None)
            brand_name = item.get("name", "brand")
            slug = re.sub(r"[^a-z0-9-]", "", brand_name.lower().replace(" ", "-"))
            slug = slug[:40].strip("-")
            if not slug:
                slug = "brand"
            item["subdomain"] = f"{target_subdomain}-{slug}"
            return item
        return _prepare_brand

    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("brands", []),
        resource_key="brands",
        list_path="brands", list_rkey="brands",
        create_path="brands", create_rkey="brand",
        create_response_rkey="brand",
        delete_path_fn=lambda tid: f"brands/{tid}",
        pre_process_fn=_make_brand_preprocess(target.subdomain),
    )

    # ---- 2.3  Ticket Fields ------------------------------------------ #
    logger.section("2.3  Ticket Fields (custom only)")

    # Pre-reconciliation: before running the create loop, scan ALL existing
    # target ticket fields and reconcile source→target mappings by title.
    # This handles re-runs where the target already has fields from a previous
    # (possibly partial) run whose id_map was lost or incomplete.
    #
    # When the target has DUPLICATE fields with the same title (from multiple
    # failed runs), we pick the one with the HIGHEST target ID — Zendesk
    # assigns IDs sequentially, so the highest ID is the most recently created
    # field, which is the one most likely to have the correct options/config.
    #
    # This pre-reconciliation runs BEFORE the create loop so that the
    # conflict_mode="skip" check correctly finds and skips already-existing
    # fields, and the id_map has the right target IDs for form conditions.
    try:
        _existing_target_fields = target.list_resource("ticket_fields", "ticket_fields")
        # Build title→highest_id map for non-system target fields
        _target_by_title: Dict[str, int] = {}
        for tf in _existing_target_fields:
            if not isinstance(tf, dict):
                continue
            if tf.get("type") in SYSTEM_TYPES:
                continue
            title = tf.get("title", "")
            tid = tf.get("id")
            if not title or not tid:
                continue
            # Keep the highest ID (most recently created) when duplicates exist
            if title not in _target_by_title or tid > _target_by_title[title]:
                _target_by_title[title] = tid

        _pre_reconciled = 0
        for src_field in exports.get("ticket_fields", []):
            if not isinstance(src_field, dict):
                continue
            if src_field.get("type") in SYSTEM_TYPES:
                continue
            src_id = src_field.get("id")
            src_title = src_field.get("title", "")
            if not src_id or not src_title:
                continue
            if str(src_id) in id_map.get("ticket_fields", {}):
                continue  # already mapped
            tgt_id = _target_by_title.get(src_title)
            if tgt_id:
                _record_mapping(id_map, "ticket_fields", src_id, tgt_id)
                _pre_reconciled += 1

        if _pre_reconciled:
            logger.info(
                f"  Pre-reconciled {_pre_reconciled} ticket field(s) by title "
                "from existing target fields (re-run recovery)."
            )
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(
            f"Could not pre-reconcile ticket fields from target: {exc}. "
            "Proceeding — conflict_mode='skip' will handle collisions."
        )

    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("ticket_fields", []),
        resource_key="ticket_fields",
        list_path="ticket_fields", list_rkey="ticket_fields",
        create_path="ticket_fields", create_rkey="ticket_field",
        create_response_rkey="ticket_field",
        delete_path_fn=lambda tid: f"ticket_fields/{tid}",
        update_path_fn=lambda tid: f"ticket_fields/{tid}",
        # Ticket fields are identified by `title`, not `name` — without this the
        # collision check never matches, so re-runs duplicate every custom field
        # (and the form-field map drifts to the newest duplicate).
        name_field="title",
        pre_process_fn=prepare_ticket_field,
        skip_system=False,  # handled by pre_process_fn above
        conflict_mode="skip",
    )
    # Zendesk ignores `position` on create — restore the source field order.
    restore_positions(
        target, id_map, exports.get("ticket_fields", []),
        id_map_key="ticket_fields",
        update_path_fn=lambda tid: f"ticket_fields/{tid}",
        wrap_key="ticket_field",
    )

    # ---- 2.4  User Fields -------------------------------------------- #
    logger.section("2.4  User Fields")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("user_fields", []),
        resource_key="user_fields",
        list_path="user_fields", list_rkey="user_fields",
        create_path="user_fields", create_rkey="user_field",
        create_response_rkey="user_field",
        delete_path_fn=lambda tid: f"user_fields/{tid}",
        update_path_fn=lambda tid: f"user_fields/{tid}",
        name_field="key",
        pre_process_fn=prepare_user_field,
        conflict_mode="skip",
    )
    restore_positions(
        target, id_map, exports.get("user_fields", []),
        id_map_key="user_fields",
        update_path_fn=lambda tid: f"user_fields/{tid}",
        wrap_key="user_field",
    )

    # ---- 2.5  Organization Fields ------------------------------------ #
    logger.section("2.5  Organization Fields")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("organization_fields", []),
        resource_key="organization_fields",
        list_path="organization_fields", list_rkey="organization_fields",
        create_path="organization_fields", create_rkey="organization_field",
        create_response_rkey="organization_field",
        delete_path_fn=lambda tid: f"organization_fields/{tid}",
        update_path_fn=lambda tid: f"organization_fields/{tid}",
        name_field="key",
        pre_process_fn=prepare_user_field,
        conflict_mode="skip",
    )
    restore_positions(
        target, id_map, exports.get("organization_fields", []),
        id_map_key="organization_fields",
        update_path_fn=lambda tid: f"organization_fields/{tid}",
        wrap_key="organization_field",
    )

    # ---- 2.6  Custom Roles (Enterprise only) ------------------------- #
    logger.section("2.6  Custom Roles")
    if exports.get("custom_roles"):
        import_resource(
            client=target, id_map=id_map,
            source_items=exports.get("custom_roles", []),
            resource_key="custom_roles",
            list_path="custom_roles", list_rkey="custom_roles",
            create_path="custom_roles", create_rkey="custom_role",
            create_response_rkey="custom_role",
            delete_path_fn=lambda tid: f"custom_roles/{tid}",
            conflict_mode="replace",
        )
    else:
        logger.log_skipped(
            "custom_roles", "N/A",
            "No custom roles in source or not on Enterprise plan"
        )

    # ---- 2.7  Ticket Forms ------------------------------------------- #
    logger.section("2.7  Ticket Forms (base metadata)")

    def _strip_form_field_ids(item: Dict, _id_map: Dict) -> Dict:
        """Import form metadata without ticket_field_ids; Phase 2 will assign fields."""
        item = dict(item)
        item.pop("ticket_field_ids", None)
        item.pop("agent_conditions", None)
        item.pop("end_user_conditions", None)
        item.pop("restricted_brand_ids", None)
        return item

    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("ticket_forms", []),
        resource_key="ticket_forms",
        list_path="ticket_forms", list_rkey="ticket_forms",
        create_path="ticket_forms", create_rkey="ticket_form",
        create_response_rkey="ticket_form",
        delete_path_fn=lambda tid: f"ticket_forms/{tid}",
        pre_process_fn=_strip_form_field_ids,
        conflict_mode="skip",
    )
    restore_positions(
        target, id_map, exports.get("ticket_forms", []),
        id_map_key="ticket_forms",
        update_path_fn=lambda tid: f"ticket_forms/{tid}",
        wrap_key="ticket_form",
    )

    # ---- 2.8  Organizations (batched) -------------------------------- #
    logger.section("2.8  Organizations")
    _import_organizations_batched(
        target, id_map, exports.get("organizations", [])
    )

    from src.importer import flush_id_map
    flush_id_map(id_map)
    logger.success("Phase 2 — Foundation Layer complete.")
    return id_map


# ------------------------------------------------------------------ #
#  Organizations: bulk path via /create_many                          #
# ------------------------------------------------------------------ #
#
# Source accounts often have hundreds of orgs (this codebase has seen 563).
# Posting them one at a time spends 563 tokens from the rate limiter — at
# Suite Team's 200 rpm that alone is ~3 minutes. The /create_many endpoint
# takes up to 100 items per call and returns a job_status to poll; net cost
# drops to ~6 POSTs + ~6 GETs for polling.
#
# We preserve the legacy import_resource() semantics:
#   - conflict_mode="skip": don't replace orgs that already exist by name
#   - strip_source_fields + remap_payload before send
#   - log CREATED / SKIPPED / FAILED via the same logger calls so the
#     migration report keeps the same shape

ORG_BATCH_SIZE = 100  # Zendesk documented cap


def _import_organizations_batched(
    target: ZendeskClient,
    id_map: Dict,
    source_items: List[Dict],
) -> None:
    if not source_items:
        return

    # 1) Snapshot existing orgs in target so we can honour conflict_mode=skip
    try:
        existing = target.list_resource("organizations", "organizations")
    except (ZendeskAPIError, ZendeskNetworkError) as exc:
        logger.warn(
            f"Could not list existing organizations: {exc}. Proceeding without conflict check."
        )
        existing = []
    existing_by_name = {
        o["name"]: o for o in existing if isinstance(o, dict) and o.get("name")
    }

    # 2) Prepare payloads + parallel source-id list (same index as payload).
    prepared: List[Dict] = []
    sources: List[Dict] = []  # (source_id, name) per prepared entry
    for item in source_items:
        if not isinstance(item, dict):
            continue
        source_id = item.get("id")
        name = item.get("name") or f"<unnamed id={source_id}>"

        if name in existing_by_name:
            # The target already has an org with this name — typically from
            # a prior run. Reconcile the id_map so dependent phases (views/
            # triggers/automations) can resolve `organization_id` references
            # without seeing a 'Missing ID map' warning.
            tid = existing_by_name[name].get("id")
            if tid and source_id is not None:
                _record_mapping(id_map, "organizations", source_id, tid)
            logger.log_skipped(
                "organizations", source_id,
                f"Target already has a organizations named '{name}'"
            )
            continue

        try:
            payload = strip_source_fields(item, strip_custom_role=True)
            # Organization custom-field VALUES are keyed by the field's stable
            # `key` string, and the org field definitions were migrated in
            # Phase 1 (section 2.5) with name_field="key", so the keys line up
            # on the target. Sanitize (drop nulls / bad keys) and retain them;
            # omit entirely when empty.
            sanitized_org_fields = sanitize_custom_field_values(
                payload.get("organization_fields")
            )
            payload.pop("organization_fields", None)
            payload = remap_payload(
                payload, id_map, context=f"organizations:{name}"
            )
            if sanitized_org_fields is not None:
                payload["organization_fields"] = sanitized_org_fields
        except RemapError as exc:
            logger.log_failed("organizations", source_id, str(exc), name)
            continue
        except Exception as exc:
            logger.log_failed(
                "organizations", source_id,
                f"Error preparing payload: {type(exc).__name__}: {exc}", name,
            )
            continue

        prepared.append(payload)
        sources.append({"id": source_id, "name": name})

    # 3) Submit in 100-item chunks. Each chunk = 1 POST + N polls.
    for chunk_start in range(0, len(prepared), ORG_BATCH_SIZE):
        chunk_payloads = prepared[chunk_start:chunk_start + ORG_BATCH_SIZE]
        chunk_sources = sources[chunk_start:chunk_start + ORG_BATCH_SIZE]
        try:
            results = target.create_many(
                "organizations/create_many", "organizations", chunk_payloads
            )
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            # Whole batch failed — log every item in the chunk as failed
            for s in chunk_sources:
                logger.log_failed(
                    "organizations", s["id"],
                    f"create_many batch failed: {exc}", s["name"],
                )
            continue

        # 4) Decode results. Zendesk returns the `results` array in the
        #    same order as the submitted items; the documented `index`
        #    field is sometimes absent in practice, so we trust array
        #    position. We still consult `index` as a tie-breaker when
        #    present so out-of-order responses (if Zendesk changes that)
        #    don't mis-pair entries.
        for pos, entry in enumerate(results):
            if not isinstance(entry, dict):
                continue
            raw_idx = entry.get("index")
            if isinstance(raw_idx, int) and 0 <= raw_idx < len(chunk_sources):
                idx = raw_idx
            elif 0 <= pos < len(chunk_sources):
                idx = pos
            else:
                continue
            s = chunk_sources[idx]
            target_id = entry.get("id")
            if target_id:
                _record_mapping(id_map, "organizations", s["id"], target_id)
                logger.log_created(
                    "organizations", s["id"], target_id, s["name"]
                )
            else:
                err = (
                    entry.get("details") or entry.get("error") or
                    entry.get("status") or "Unknown batch failure"
                )
                if isinstance(err, (dict, list)):
                    import json as _json
                    err = _json.dumps(err)[:500]
                logger.log_failed(
                    "organizations", s["id"], str(err)[:500], s["name"]
                )
