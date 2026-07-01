"""
Phase 2 — Business Logic Migration.

Migrates (in dependency order after foundation):
  Ticket Form Field Assignments → Trigger Categories → Views → Triggers →
  Automations → Macros → SLA Policies → Group SLAs → Schedules →
  Routing Attributes → Dynamic Content → Webhooks

Bug fixes vs v1:
  - _assign_form_fields now imports and catches ZendeskNetworkError.
  - form.get("id") None guard — str(None) produced "None" key mismatch.
  - int(tfid) guarded with .isdigit() to prevent ValueError on corrupt map.
  - dry-run return value from target.put() now checked.
  - trigger_categories are now migrated (3.3) so category_id on triggers
    can be remapped to target trigger_category IDs instead of being stripped.
"""

from typing import Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.importer import import_resource, load_id_map
from src.remapper import (
    remap_form_conditions,
    remap_macro_actions,
    find_subdomain_references,
    build_system_field_map,
)
from src.utils import logger


def run(source: ZendeskClient, target: ZendeskClient,
        exports: Dict[str, List[Dict]]) -> Dict:
    id_map = load_id_map()

    # ---- 3.1  Ticket Form → Field Assignments ------------------------ #
    logger.section("3.1  Ticket Form Field Assignments")
    _assign_form_fields(
        target, id_map,
        exports.get("ticket_forms", []),
        exports.get("ticket_fields", []),
    )

    # ---- 3.2  Views -------------------------------------------------- #
    logger.section("3.2  Views")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("views", []),
        resource_key="views",
        list_path="views", list_rkey="views",
        create_path="views", create_rkey="view",
        create_response_rkey="view",
        delete_path_fn=lambda tid: f"views/{tid}",
        pre_process_fn=_prepare_rule,
        conflict_mode="replace",
    )
    logger.section("3.2c  View Ordering")
    _restore_rule_positions(
        target, id_map, exports.get("views", []),
        resource_key="views", id_map_key="views",
        update_path_fn=lambda tid: f"views/{tid}",
        wrap_key="view",
    )

    # ---- 3.2b  Custom Ticket Statuses -------------------------------- #
    # Custom statuses are referenced by trigger/automation conditions via
    # the `custom_status_id` field. They must exist on the target before
    # triggers are migrated so the condition values can be remapped.
    logger.section("3.2b  Custom Ticket Statuses")
    if exports.get("custom_statuses"):
        import_resource(
            client=target, id_map=id_map,
            source_items=exports.get("custom_statuses", []),
            resource_key="custom_statuses",
            list_path="custom_statuses", list_rkey="custom_statuses",
            create_path="custom_statuses", create_rkey="custom_status",
            create_response_rkey="custom_status",
            delete_path_fn=lambda tid: f"custom_statuses/{tid}",
            name_field="agent_label",
            conflict_mode="skip",
        )
    else:
        logger.info(
            "  No custom ticket statuses in export — skipping. "
            "(Expected if the source account does not use custom statuses.)"
        )

    # ---- 3.3  Trigger Categories ------------------------------------- #
    logger.section("3.3  Trigger Categories")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("trigger_categories", []),
        resource_key="trigger_categories",
        list_path="trigger_categories", list_rkey="trigger_categories",
        create_path="trigger_categories", create_rkey="trigger_category",
        create_response_rkey="trigger_category",
        delete_path_fn=lambda tid: f"trigger_categories/{tid}",
        conflict_mode="replace",
    )

    # ---- 3.4  Triggers ----------------------------------------------- #
    logger.section("3.4  Triggers")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("triggers", []),
        resource_key="triggers",
        list_path="triggers", list_rkey="triggers",
        create_path="triggers", create_rkey="trigger",
        create_response_rkey="trigger",
        delete_path_fn=lambda tid: f"triggers/{tid}",
        pre_process_fn=_prepare_trigger,
        post_process_fn=_assign_trigger_category,
        conflict_mode="replace",
    )
    logger.section("3.4b  Trigger Ordering")
    _restore_rule_positions(
        target, id_map, exports.get("triggers", []),
        resource_key="triggers", id_map_key="triggers",
        update_path_fn=lambda tid: f"triggers/{tid}",
        wrap_key="trigger",
    )

    # ---- 3.5  Automations -------------------------------------------- #
    logger.section("3.5  Automations")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("automations", []),
        resource_key="automations",
        list_path="automations", list_rkey="automations",
        create_path="automations", create_rkey="automation",
        create_response_rkey="automation",
        delete_path_fn=lambda tid: f"automations/{tid}",
        pre_process_fn=_prepare_rule,
        conflict_mode="replace",
    )
    logger.section("3.5b  Automation Ordering")
    _restore_rule_positions(
        target, id_map, exports.get("automations", []),
        resource_key="automations", id_map_key="automations",
        update_path_fn=lambda tid: f"automations/{tid}",
        wrap_key="automation",
    )

    # ---- 3.6  Macros ------------------------------------------------- #
    logger.section("3.6  Macros")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("macros", []),
        resource_key="macros",
        list_path="macros", list_rkey="macros",
        create_path="macros", create_rkey="macro",
        create_response_rkey="macro",
        delete_path_fn=lambda tid: f"macros/{tid}",
        pre_process_fn=_prepare_macro,
        post_process_fn=_assign_macro_actions,
        conflict_mode="replace",
    )

    # ---- 3.7  SLA Policies ------------------------------------------- #
    logger.section("3.7  SLA Policies")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("sla_policies", []),
        resource_key="sla_policies",
        list_path="slas/policies", list_rkey="sla_policies",
        create_path="slas/policies", create_rkey="sla_policy",
        create_response_rkey="sla_policy",
        delete_path_fn=lambda tid: f"slas/policies/{tid}",
        conflict_mode="replace",
    )

    # ---- 3.8  Group SLA Policies (Enterprise) ------------------------ #
    logger.section("3.8  Group SLA Policies")
    if exports.get("group_sla_policies"):
        import_resource(
            client=target, id_map=id_map,
            source_items=exports.get("group_sla_policies", []),
            resource_key="group_sla_policies",
            list_path="group_slas/policies", list_rkey="group_sla_policies",
            create_path="group_slas/policies", create_rkey="group_sla_policy",
            create_response_rkey="group_sla_policy",
            delete_path_fn=lambda tid: f"group_slas/policies/{tid}",
            conflict_mode="replace",
        )
    else:
        logger.log_manual(
            "group_sla_policies",
            "No Group SLA policies were migrated. This is expected if the "
            "source isn't Enterprise or has none. If the source DOES have "
            "Group SLA policies, verify the export step succeeded — an empty "
            "export and a plan restriction look identical here.",
        )

    # ---- 3.9  Business Hours Schedules ------------------------------- #
    logger.section("3.9  Business Hours Schedules")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("schedules", []),
        resource_key="schedules",
        list_path="business_hours/schedules", list_rkey="schedules",
        create_path="business_hours/schedules", create_rkey="schedule",
        create_response_rkey="schedule",
        delete_path_fn=lambda tid: f"business_hours/schedules/{tid}",
        conflict_mode="replace",
    )

    # ---- 3.10  Routing Attributes ------------------------------------ #
    logger.section("3.10  Routing Attributes")
    if exports.get("routing_attributes"):
        import_resource(
            client=target, id_map=id_map,
            source_items=exports.get("routing_attributes", []),
            resource_key="routing_attributes",
            list_path="routing/attributes", list_rkey="attributes",
            create_path="routing/attributes", create_rkey="attribute",
            create_response_rkey="attribute",
            delete_path_fn=lambda tid: f"routing/attributes/{tid}",
            conflict_mode="replace",
        )

        # ---- 3.10b  Routing Attribute Values (skills) ---------------- #
        # Skill option values are nested under each attribute and must be
        # migrated after the attribute definitions exist on the target.
        logger.section("3.10b  Routing Attribute Values (skills)")
        _migrate_routing_attribute_values(
            target, id_map, exports.get("routing_attribute_values", [])
        )
    else:
        logger.log_manual(
            "routing_attributes",
            "No routing attributes were migrated. Expected if the source "
            "isn't Enterprise or has none. If the source DOES use skills-based "
            "routing, verify the export step succeeded — an empty export and a "
            "plan restriction look identical here.",
        )

    # ---- 3.11  Dynamic Content --------------------------------------- #
    logger.section("3.11  Dynamic Content")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("dynamic_content_items", []),
        resource_key="dynamic_content_items",
        list_path="dynamic_content/items", list_rkey="items",
        create_path="dynamic_content/items", create_rkey="item",
        create_response_rkey="item",
        delete_path_fn=lambda tid: f"dynamic_content/items/{tid}",
        conflict_mode="replace",
    )

    # ---- 3.11b  Dynamic Content Locale Variants ---------------------- #
    # Non-default locale variants are nested under each item and must be
    # migrated after the item definitions exist on the target.
    logger.section("3.11b  Dynamic Content Locale Variants")
    _migrate_dynamic_content_variants(
        target, id_map, exports.get("dynamic_content_variants", [])
    )

    # ---- 3.12  Webhooks ---------------------------------------------- #
    logger.section("3.12  Webhooks")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("webhooks", []),
        resource_key="webhooks",
        list_path="webhooks", list_rkey="webhooks",
        create_path="webhooks", create_rkey="webhook",
        create_response_rkey="webhook",
        delete_path_fn=lambda tid: f"webhooks/{tid}",
        pre_process_fn=_scrub_webhook_secret,
        post_process_fn=_remap_webhook_subscriptions,
        conflict_mode="replace",
    )

    # ---- 3.13  Source-subdomain reference scan ----------------------- #
    # The target brand gets a freshly-generated subdomain, so any hard-coded
    # "<source>.zendesk.com" URL embedded in macro comments, dynamic content,
    # or triggers will break. We don't auto-rewrite (risk of corrupting
    # legitimate references) — instead we flag the affected items as MANUAL.
    _scan_subdomain_references(source, exports)

    from src.importer import flush_id_map
    flush_id_map(id_map)
    logger.success("Phase 3 — Business Logic complete.")
    return id_map


def _restore_rule_positions(
    target: ZendeskClient,
    id_map: Dict,
    source_items: List[Dict],
    *,
    resource_key: str,
    id_map_key: str,
    update_path_fn,
    wrap_key: str,
) -> None:
    """
    Restore `position` ordering for views, triggers, and automations.

    Zendesk assigns positions by insertion order on create. After all items
    of a type exist on the target, we PUT each item's position so the
    execution/display order matches the source exactly.

    Only items with an explicit `position` value that were successfully
    migrated (present in id_map) are updated.
    """
    resource_map = id_map.get(id_map_key, {})
    if not isinstance(resource_map, dict) or not source_items:
        return

    updated = skipped = failed = 0
    for item in source_items:
        if not isinstance(item, dict):
            continue
        src_id = item.get("id")
        position = item.get("position")
        if position is None:
            continue
        tgt_id = resource_map.get(str(src_id))
        if not tgt_id:
            skipped += 1
            continue
        try:
            resp = target.put(
                update_path_fn(tgt_id),
                {wrap_key: {"position": int(position)}},
            )
            if isinstance(resp, dict) and resp.get("dry_run"):
                skipped += 1
                continue
            updated += 1
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed(
                f"{resource_key}_position", src_id,
                f"Failed to set position={position}: {exc}",
                item.get("title") or item.get("name", ""),
            )
            failed += 1
        except Exception as exc:
            logger.log_failed(
                f"{resource_key}_position", src_id,
                f"Unexpected error: {type(exc).__name__}: {exc}",
                item.get("title") or item.get("name", ""),
            )
            failed += 1

    if updated or failed:
        logger.success(
            f"{resource_key} positions: {updated} updated, "
            f"{failed} failed, {skipped} skipped."
        )
    else:
        logger.info(f"  No {resource_key} positions to restore.")


def _scan_subdomain_references(
    source: ZendeskClient, exports: Dict[str, List[Dict]],
) -> None:
    """Report resources that embed the source account's subdomain host."""
    src_sub = getattr(source, "subdomain", "") or ""
    if not src_sub:
        return

    # (export_key, name_field) for the content-bearing resources most likely
    # to contain portal URLs.
    scan_targets = [
        ("macros", "title"),
        ("dynamic_content_items", "name"),
        ("triggers", "title"),
        ("automations", "title"),
    ]
    flagged = 0
    for export_key, name_field in scan_targets:
        for item in exports.get(export_key, []) or []:
            if not isinstance(item, dict):
                continue
            if find_subdomain_references(item, src_sub):
                flagged += 1
                logger.log_manual(
                    export_key,
                    f"'{item.get(name_field, item.get('id'))}' contains a "
                    f"hard-coded '{src_sub}.zendesk.com' URL. Update it to the "
                    "target brand's subdomain — it was migrated verbatim.",
                )
    if flagged:
        logger.warn(
            f"Found {flagged} resource(s) embedding the source subdomain "
            f"'{src_sub}.zendesk.com'. See MANUAL entries in the report."
        )


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

def _assign_form_fields(target: ZendeskClient, id_map: Dict,
                        source_forms: List[Dict],
                        source_fields: Optional[List[Dict]] = None) -> None:
    """
    Phase 3.1 — Now that ticket fields exist in target, push the full
    ticket_field_ids assignment to each already-created form, along with the
    conditional-field rules (agent_conditions / end_user_conditions) remapped
    from source field IDs to target field IDs.

    Conditions are stripped in Phase 1 (the referenced fields don't exist on
    the target yet) and restored here, where both the form and the ticket
    fields are resolvable. Without this step, dynamic forms (fields that
    show/hide or become required based on a selected option) lose their
    behavior after migration.

    Field-ID resolution uses BOTH:
      - id_map["ticket_fields"] — custom fields created/reconciled in Phase 1.
      - a system-field map matched by `type` — built here from the source field
        export and the target's live field list. System fields (Type, Priority,
        Status, ...) are never created, so they aren't in id_map; conditions
        anchored on them (or using them as children) would otherwise be dropped.

    Both the form's `ticket_field_ids` order and the conditional rules are
    resolved through this combined map, so system fields keep their place on
    the form and system-field-driven dependencies survive.

    Bug fixes:
      - Guards form.get("id") is not None before str() conversion.
      - Guards int(tfid) with .isdigit() check.
      - Checks dry_run response from target.put().
      - Catches ZendeskNetworkError in addition to ZendeskAPIError.
    """
    form_map = id_map.get("ticket_forms", {})
    field_map = id_map.get("ticket_fields", {})

    if not isinstance(form_map, dict):
        logger.warn("_assign_form_fields: ticket_forms map is missing or corrupt.")
        return
    if not isinstance(field_map, dict):
        logger.warn("_assign_form_fields: ticket_fields map is missing or corrupt.")
        return

    # Build a combined custom + system field map. System fields are matched by
    # `type` against the target's live field list (one of each per account).
    system_field_map: Dict[str, str] = {}
    if source_fields:
        try:
            target_fields = target.list_resource("ticket_fields", "ticket_fields")
            system_field_map = build_system_field_map(source_fields, target_fields)
            if system_field_map:
                logger.info(
                    f"  Mapped {len(system_field_map)} system ticket field(s) "
                    "by type for form layout/conditions."
                )
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(
                "_assign_form_fields: could not list target ticket fields to "
                f"map system fields ({exc}). Conditions or field order that "
                "reference system fields (Type/Priority/...) may be lost."
            )

    # full_field_map resolves any source ticket field id (custom or system) to
    # its target id. Custom mappings take precedence over system on the (never
    # expected) chance of an id collision across the two maps.
    full_field_map: Dict[str, str] = dict(system_field_map)
    full_field_map.update({str(k): v for k, v in field_map.items()})

    # remap_form_conditions resolves field ids via id_map["ticket_fields"], so
    # hand it an id_map view whose ticket_fields is the combined map.
    cond_id_map: Dict = dict(id_map)
    cond_id_map["ticket_fields"] = full_field_map

    for form in source_forms:
        if not isinstance(form, dict):
            continue

        raw_form_id = form.get("id")
        # Bug fix: guard against None id before str() conversion
        if raw_form_id is None:
            logger.log_skipped(
                "ticket_form_fields", "unknown",
                "Source form has no 'id' field — skipping."
            )
            continue

        source_form_id = str(raw_form_id)
        target_form_id = form_map.get(source_form_id)
        if not target_form_id:
            logger.log_skipped(
                "ticket_form_fields", source_form_id,
                "No target form ID found (form may have failed earlier)"
            )
            continue

        # Remap all ticket_field_ids
        source_field_ids = form.get("ticket_field_ids") or []
        if not isinstance(source_field_ids, list):
            source_field_ids = []

        target_field_ids = []
        for sfid in source_field_ids:
            if sfid is None:
                continue
            tfid = full_field_map.get(str(sfid))
            if not tfid:
                continue
            # Bug fix: guard against non-digit strings (corrupt map values)
            if isinstance(tfid, int):
                target_field_ids.append(tfid)
            elif isinstance(tfid, str) and tfid.isdigit():
                target_field_ids.append(int(tfid))
            else:
                logger.warn(
                    f"ticket_form_fields: skipping invalid mapped field id "
                    f"{tfid!r} for source field {sfid}"
                )

        # Restore conditional-field rules (dynamic forms). Field IDs embedded
        # in the conditions are remapped to the target; conditions anchored to
        # fields that weren't migrated are dropped by the remapper.
        form_ctx = f"ticket_forms:{form.get('name', source_form_id)}"
        agent_conditions = remap_form_conditions(
            form.get("agent_conditions"), cond_id_map, context=form_ctx
        )
        end_user_conditions = remap_form_conditions(
            form.get("end_user_conditions"), cond_id_map, context=form_ctx
        )

        source_agent_conds = form.get("agent_conditions")
        source_end_user_conds = form.get("end_user_conditions")
        has_conditions = (
            (source_agent_conds is not None and agent_conditions)
            or (source_end_user_conds is not None and end_user_conditions)
        )

        # Fix P1-O: if every source field failed to remap AND there are no
        # conditions to apply, skip the PUT entirely — sending
        # ticket_field_ids=[] would wipe the form's field layout on the target.
        # But if conditions ARE present (e.g. a form that uses only system
        # fields whose IDs are in the system_field_map), we must still PUT so
        # the dynamic rules are applied even when no custom fields remapped.
        if not target_field_ids and not has_conditions:
            logger.warn(
                f"ticket_form_fields: skipping form '{form.get('name', source_form_id)}' "
                f"(target_form_id={target_form_id}) — all source field IDs failed to remap "
                "and no conditions to apply. The form's field layout on the target is unchanged."
            )
            continue

        ticket_form_payload: Dict = {}
        if target_field_ids:
            ticket_form_payload["ticket_field_ids"] = target_field_ids
        # Always include condition keys when the source form had them, even if
        # the remapped list is empty. Sending [] explicitly clears any stale
        # conditions left on the target from a previous run. Omitting the key
        # entirely (when the source had no conditions) avoids an unnecessary
        # API round-trip and leaves the target's default state intact.
        if source_agent_conds is not None:
            ticket_form_payload["agent_conditions"] = agent_conditions
        if source_end_user_conds is not None:
            ticket_form_payload["end_user_conditions"] = end_user_conditions

        payload = {"ticket_form": ticket_form_payload}
        try:
            resp = target.put(f"ticket_forms/{target_form_id}", payload)
            # Bug fix: honour dry_run — put() returns {"dry_run": True} in dry mode
            if isinstance(resp, dict) and resp.get("dry_run"):
                logger.log_skipped(
                    "ticket_form_fields", source_form_id, "dry-run"
                )
                continue
            logger.log_created(
                "ticket_form_fields", source_form_id,
                target_form_id, form.get("name", "")
            )
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.log_failed(
                "ticket_form_fields", source_form_id, str(exc),
                form.get("name", "")
            )
        except Exception as exc:
            logger.log_failed(
                "ticket_form_fields", source_form_id,
                f"Unexpected error: {type(exc).__name__}: {exc}",
                form.get("name", "")
            )


from typing import Dict, Optional


def _prepare_rule(item: Dict, id_map: Dict) -> Optional[Dict]:
    """
    Pre-process a view / trigger / automation before the generic importer
    runs strip_source_fields + remap_payload on it.

    - Drops `restriction` whose target group/user doesn't exist in id_map.
      Posting an orphaned restriction.id 422s ("invalid group ids: ...").
      With no restriction the rule becomes shared, which is the documented
      Zendesk fallback and matches what the API does when restriction is
      omitted on create.
    """
    item = dict(item)

    restriction = item.get("restriction")
    if isinstance(restriction, dict):
        rtype = restriction.get("type", "")
        rid = restriction.get("id")
        if rid is not None and rtype in ("Group", "User"):
            cat = "groups" if rtype == "Group" else "users"
            mapping = id_map.get(cat) or {}
            if not mapping.get(str(rid)):
                logger.warn(
                    f"Stripping restriction on '{item.get('title') or item.get('name') or '<unnamed>'}' "
                    f"— {cat[:-1]} {rid} not present in target."
                )
                item.pop("restriction", None)

    return item


def _prepare_trigger(item: Optional[Dict], id_map: Dict) -> Optional[Dict]:
    """
    Pre-process a trigger before the generic importer.

    Pops `category_id` so the generic remapper (which maps
    FIELD_REMAP_MAP['category_id'] → hc_categories) never sees it.
    The remapped target category_id is stashed in `_trigger_category_id`
    which _assign_trigger_category renames back to `category_id` after
    remap_payload runs.
    """
    if item is None:
        return None
    item = _prepare_rule(item, id_map)
    if item is None:
        return None
    item = dict(item)
    cat_id = item.pop("category_id", None)
    if cat_id is not None:
        mapping = id_map.get("trigger_categories", {})
        target_cat_id = mapping.get(str(cat_id))
        if target_cat_id is not None:
            item["_trigger_category_id"] = target_cat_id
    return item


def _assign_trigger_category(payload: Dict, id_map: Dict) -> Dict:
    """
    Post-process: restore the remapped category_id from the stash
    created by _prepare_trigger, so it appears in the POST payload.
    """
    if "_trigger_category_id" in payload:
        tid = payload.pop("_trigger_category_id")
        payload["category_id"] = int(tid) if str(tid).isdigit() else tid
    return payload


def _prepare_macro(item: Optional[Dict], id_map: Dict) -> Optional[Dict]:
    """
    Pre-process a macro before the generic importer.

    Macro `actions` can contain ID-bearing fields (group_id, brand_id,
    ticket_form_id, the composite assignee_id "<group>/<user>", and
    custom_fields_<id>). The generic remap_payload would walk these as
    condition/action items and, crucially, MIS-handle the composite assignee
    format and re-process already-mapped values.

    To get exactly-once, correct remapping we stash the raw actions under
    `_macro_actions` and pop `actions`, so remap_payload never touches them.
    `_assign_macro_actions` (post_process) does the remap after remap_payload
    has run on the rest of the payload, then restores `actions`.
    """
    if item is None:
        return None
    item = dict(item)
    if "actions" in item:
        item["_macro_actions"] = item.pop("actions")
    return item


def _assign_macro_actions(payload: Dict, id_map: Dict) -> Dict:
    """
    Post-process: remap the stashed macro actions and restore them as
    `actions`. Runs after remap_payload so target IDs aren't re-looked-up.
    """
    if "_macro_actions" in payload:
        raw_actions = payload.pop("_macro_actions")
        payload["actions"] = remap_macro_actions(
            raw_actions, id_map,
            context=f"macros:{payload.get('title', '<unnamed>')}",
        )
    return payload


def _remap_webhook_subscriptions(payload: Dict, id_map: Dict) -> Dict:
    """
    Post-process: remap the `subscriptions` array on a webhook payload.

    Zendesk webhook subscriptions reference trigger or automation IDs via
    the shape:
        {"event_type": "conditional_ticket_events", "resource_type": "trigger",
         "resource_id": <source_trigger_id>}

    After triggers and automations are migrated (steps 3.4 / 3.5), their
    source→target ID mappings are in id_map["triggers"] and
    id_map["automations"]. We remap each subscription's resource_id here.

    Subscriptions whose resource_id has no mapping (e.g. the trigger was
    skipped or failed) are dropped and logged as MANUAL — a dangling
    subscription would cause Zendesk to reject the webhook create with a
    422 referencing a non-existent trigger.
    """
    subs = payload.get("subscriptions")
    if not isinstance(subs, list) or not subs:
        return payload

    webhook_name = payload.get("name", "<unnamed>")
    remapped: list = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        rtype = sub.get("resource_type", "")
        rid = sub.get("resource_id")
        if rid is None:
            remapped.append(sub)
            continue

        # Map resource_type → id_map category
        category_map = {
            "trigger":    "triggers",
            "automation": "automations",
        }
        category = category_map.get(rtype)
        if category is None:
            # Unknown resource type — pass through unchanged
            remapped.append(sub)
            continue

        mapping = id_map.get(category, {})
        target_rid = mapping.get(str(rid))
        if target_rid is None:
            logger.log_manual(
                "webhooks",
                f"Webhook '{webhook_name}': subscription references "
                f"{rtype} id={rid} which was not migrated — subscription dropped. "
                "Re-add it manually in the target account.",
            )
            continue

        new_sub = dict(sub)
        new_sub["resource_id"] = int(target_rid) if str(target_rid).isdigit() else target_rid
        remapped.append(new_sub)

    payload = dict(payload)
    payload["subscriptions"] = remapped
    return payload


def _scrub_webhook_secret(item: Dict, _id_map: Dict) -> Optional[Dict]:
    """
    Webhook signing secrets cannot be read from the API.
    Remove signing_secret so Zendesk generates a new one on creation.
    The new secret will be logged and must be updated on the receiving endpoint.

    Also handles write-only auth credentials (bearer_token, basic_auth, api_key)
    which Zendesk does not return — these webhooks must be migrated manually.
    """
    item = dict(item)
    item.pop("signing_secret", None)
    webhook_name = item.get("name", "<unnamed>")

    # Check authentication type — some credentials are write-only
    auth = item.get("authentication") or {}
    if not isinstance(auth, dict):
        auth = {}
    auth_type = auth.get("type", "signing_secret")
    auth_data = auth.get("data") or {}

    WRITE_ONLY_AUTH_TYPES = ("bearer_token", "basic_auth", "api_key")

    if auth_type in WRITE_ONLY_AUTH_TYPES and not auth_data:
        logger.log_manual(
            "webhook",
            f"Webhook '{webhook_name}' uses '{auth_type}' authentication "
            "whose credentials are write-only and were not returned by the "
            "API. Create this webhook manually in the target account."
        )
        return None  # signal importer to skip this item

    logger.log_manual(
        "webhook",
        f"Webhook '{webhook_name}' will receive a NEW signing secret. "
        "Update your endpoint's validation logic accordingly."
    )
    return item


def _migrate_routing_attribute_values(
    target: ZendeskClient,
    id_map: Dict,
    source_values: List[Dict],
) -> None:
    """
    Migrate routing attribute skill values to the target.

    Each value record was augmented by the extractor with an `attribute_id`
    field pointing to its parent source attribute. We resolve that to the
    target attribute ID via id_map["routing_attributes"], then POST the value
    under the correct target attribute.

    Conflict handling: Zendesk returns 422 if a value with the same name
    already exists under the attribute. We list existing values per attribute
    and skip duplicates by name.
    """
    if not source_values:
        logger.info("  No routing attribute values to migrate.")
        return

    attr_map = id_map.get("routing_attributes", {})
    if not isinstance(attr_map, dict):
        logger.warn(
            "routing_attribute_values: routing_attributes map missing — "
            "cannot migrate skill values."
        )
        return

    # Group source values by their source attribute_id for efficient processing
    from collections import defaultdict as _defaultdict
    by_attr: Dict[str, List[Dict]] = _defaultdict(list)
    for v in source_values:
        if isinstance(v, dict):
            by_attr[str(v.get("attribute_id", ""))].append(v)

    created = skipped = failed = 0
    for src_attr_id, values in by_attr.items():
        tgt_attr_id = attr_map.get(src_attr_id)
        if not tgt_attr_id:
            for v in values:
                logger.log_skipped(
                    "routing_attribute_values", v.get("id"),
                    f"Parent attribute {src_attr_id} was not migrated",
                )
                skipped += 1
            continue

        # Fetch existing values on the target attribute to avoid duplicates
        existing_names: set = set()
        try:
            existing = target.list_resource(
                f"routing/attributes/{tgt_attr_id}/values",
                "attribute_values",
            )
            existing_names = {
                e.get("name", "") for e in existing if isinstance(e, dict)
            }
        except (ZendeskAPIError, ZendeskNetworkError) as exc:
            logger.warn(
                f"Could not list existing values for routing attribute "
                f"{tgt_attr_id}: {exc}. Proceeding without dedup."
            )

        for v in values:
            src_val_id = v.get("id")
            name = v.get("name", f"<unnamed id={src_val_id}>")
            if name in existing_names:
                logger.log_skipped(
                    "routing_attribute_values", src_val_id,
                    f"Value '{name}' already exists under target attribute {tgt_attr_id}",
                )
                skipped += 1
                continue

            payload = {"attribute_value": {"name": name}}
            try:
                resp = target.post(
                    f"routing/attributes/{tgt_attr_id}/values", payload
                )
                if resp.get("dry_run"):
                    skipped += 1
                    continue
                new_val = resp.get("attribute_value") or {}
                new_id = new_val.get("id")
                if new_id:
                    from src.importer import _record_mapping
                    _record_mapping(id_map, "routing_attribute_values", src_val_id, new_id)
                    logger.log_created(
                        "routing_attribute_values", src_val_id, new_id, name
                    )
                    existing_names.add(name)
                    created += 1
                else:
                    logger.log_failed(
                        "routing_attribute_values", src_val_id,
                        "Create response had no 'id' field.", name,
                    )
                    failed += 1
            except (ZendeskAPIError, ZendeskNetworkError) as exc:
                logger.log_failed(
                    "routing_attribute_values", src_val_id, str(exc), name
                )
                failed += 1
            except Exception as exc:
                logger.log_failed(
                    "routing_attribute_values", src_val_id,
                    f"Unexpected error: {type(exc).__name__}: {exc}", name,
                )
                failed += 1

    logger.success(
        f"Routing attribute values: {created} created, "
        f"{failed} failed, {skipped} skipped."
    )


def _migrate_dynamic_content_variants(
    target: ZendeskClient,
    id_map: Dict,
    source_variants: List[Dict],
) -> None:
    """
    Migrate non-default locale variants for dynamic content items.

    Each variant record was augmented by the extractor with `item_id`
    (source). We resolve that to the target item ID via
    id_map["dynamic_content_items"], then POST the variant.

    Conflict handling: if a variant for the locale already exists on the
    target item (422), we PUT (update) it instead.

    Note: locale_id values are Zendesk-global integers (e.g. 1 = English,
    16 = French) and do not need remapping — they are the same across accounts.
    """
    if not source_variants:
        logger.info("  No dynamic content variants to migrate.")
        return

    item_map = id_map.get("dynamic_content_items", {})
    if not isinstance(item_map, dict):
        logger.warn(
            "dynamic_content_variants: dynamic_content_items map missing — "
            "cannot migrate variants."
        )
        return

    STRIP = frozenset({"id", "url", "created_at", "updated_at", "item_id",
                       "outdated", "default"})

    created = updated = skipped = failed = 0
    for v in source_variants:
        if not isinstance(v, dict):
            continue
        src_item_id = v.get("item_id")
        locale_id = v.get("locale_id")       # integer locale ID (e.g. 1 for en-us)
        locale_str = v.get("locale", "")     # locale string (e.g. "en-us") for PUT URL
        tgt_item_id = item_map.get(str(src_item_id)) if src_item_id else None
        if not tgt_item_id:
            skipped += 1
            continue

        payload = {k: val for k, val in v.items() if k not in STRIP}
        if not payload:
            skipped += 1
            continue

        try:
            resp = target.post(
                f"dynamic_content/items/{tgt_item_id}/variants",
                {"variant": payload},
            )
            if resp.get("dry_run"):
                skipped += 1
                continue
            new_v = resp.get("variant") or {}
            if new_v.get("id"):
                logger.log_created(
                    "dynamic_content_variants",
                    v.get("id"),
                    new_v["id"],
                    f"item={tgt_item_id} locale={locale_str or locale_id}",
                )
                created += 1
            else:
                logger.log_failed(
                    "dynamic_content_variants", v.get("id"),
                    "Create response had no 'id' field.",
                    f"item={tgt_item_id} locale={locale_str or locale_id}",
                )
                failed += 1
        except ZendeskAPIError as exc:
            if exc.status_code == 422:
                # Variant already exists — update it.
                # The PUT path requires the locale STRING (e.g. "en-us"), not
                # the integer locale_id. Using the integer causes a 404 because
                # Zendesk routes on the locale code, not the numeric ID.
                put_locale = locale_str or str(locale_id)
                try:
                    resp = target.put(
                        f"dynamic_content/items/{tgt_item_id}/variants/{put_locale}",
                        {"variant": payload},
                    )
                    if resp.get("dry_run"):
                        skipped += 1
                        continue
                    logger.log_created(
                        "dynamic_content_variants",
                        v.get("id"),
                        tgt_item_id,
                        f"item={tgt_item_id} locale={put_locale} (updated)",
                    )
                    updated += 1
                except (ZendeskAPIError, ZendeskNetworkError) as upd_exc:
                    logger.log_failed(
                        "dynamic_content_variants", v.get("id"),
                        f"Update failed: {upd_exc}",
                        f"item={tgt_item_id} locale={put_locale}",
                    )
                    failed += 1
            else:
                logger.log_failed(
                    "dynamic_content_variants", v.get("id"),
                    str(exc),
                    f"item={tgt_item_id} locale={locale_str or locale_id}",
                )
                failed += 1
        except (ZendeskNetworkError, Exception) as exc:
            logger.log_failed(
                "dynamic_content_variants", v.get("id"),
                f"Unexpected error: {type(exc).__name__}: {exc}",
                f"item={tgt_item_id} locale={locale_str or locale_id}",
            )
            failed += 1

    logger.success(
        f"Dynamic content variants: {created} created, {updated} updated, "
        f"{failed} failed, {skipped} skipped."
    )
