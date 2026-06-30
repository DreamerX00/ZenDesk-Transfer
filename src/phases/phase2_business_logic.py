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
)
from src.utils import logger


def run(source: ZendeskClient, target: ZendeskClient,
        exports: Dict[str, List[Dict]]) -> Dict:
    id_map = load_id_map()

    # ---- 3.1  Ticket Form → Field Assignments ------------------------ #
    logger.section("3.1  Ticket Form Field Assignments")
    _assign_form_fields(target, id_map, exports.get("ticket_forms", []))

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

    # ---- 3.5  Automations -------------------------------------------- #
    logger.section("3.4  Automations")
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
                        source_forms: List[Dict]) -> None:
    """
    Phase 3.1 — Now that ticket fields exist in target, push the full
    ticket_field_ids assignment to each already-created form, along with the
    conditional-field rules (agent_conditions / end_user_conditions) remapped
    from source field IDs to target field IDs.

    Conditions are stripped in Phase 1 (the referenced fields don't exist on
    the target yet) and restored here, where both the form and all custom
    ticket fields are present in id_map. Without this step, dynamic forms
    (fields that show/hide or become required based on a selected option) lose
    their behavior after migration.

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
            tfid = field_map.get(str(sfid))
            if not tfid:
                continue
            # Bug fix: guard against non-digit strings (corrupt map values)
            if isinstance(tfid, str) and tfid.isdigit():
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
            form.get("agent_conditions"), id_map, context=form_ctx
        )
        end_user_conditions = remap_form_conditions(
            form.get("end_user_conditions"), id_map, context=form_ctx
        )

        ticket_form_payload = {
            "ticket_field_ids": target_field_ids,
        }
        # Only include condition keys when present, so forms without dynamic
        # rules aren't sent empty arrays unnecessarily.
        if agent_conditions:
            ticket_form_payload["agent_conditions"] = agent_conditions
        if end_user_conditions:
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
