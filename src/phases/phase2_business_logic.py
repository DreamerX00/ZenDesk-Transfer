"""
Phase 2 — Business Logic Migration.

Migrates (in dependency order after foundation):
  Ticket Form Field Assignments → Views → Triggers → Automations →
  Macros → SLA Policies → Group SLAs → Schedules →
  Routing Attributes → Dynamic Content → Webhooks

Bug fixes vs v1:
  - _assign_form_fields now imports and catches ZendeskNetworkError.
  - form.get("id") None guard — str(None) produced "None" key mismatch.
  - int(tfid) guarded with .isdigit() to prevent ValueError on corrupt map.
  - dry-run return value from target.put() now checked.
"""

from typing import Dict, List, Optional

from src.client import ZendeskClient, ZendeskAPIError, ZendeskNetworkError
from src.importer import import_resource, load_id_map
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

    # ---- 3.3  Triggers ----------------------------------------------- #
    logger.section("3.3  Triggers")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("triggers", []),
        resource_key="triggers",
        list_path="triggers", list_rkey="triggers",
        create_path="triggers", create_rkey="trigger",
        create_response_rkey="trigger",
        delete_path_fn=lambda tid: f"triggers/{tid}",
        pre_process_fn=_prepare_trigger,
        conflict_mode="replace",
    )

    # ---- 3.4  Automations -------------------------------------------- #
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

    # ---- 3.5  Macros ------------------------------------------------- #
    logger.section("3.5  Macros")
    import_resource(
        client=target, id_map=id_map,
        source_items=exports.get("macros", []),
        resource_key="macros",
        list_path="macros", list_rkey="macros",
        create_path="macros", create_rkey="macro",
        create_response_rkey="macro",
        delete_path_fn=lambda tid: f"macros/{tid}",
        conflict_mode="replace",
    )

    # ---- 3.6  SLA Policies ------------------------------------------- #
    logger.section("3.6  SLA Policies")
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

    # ---- 3.7  Group SLA Policies (Enterprise) ------------------------ #
    logger.section("3.7  Group SLA Policies")
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
        logger.log_skipped(
            "group_sla_policies", "N/A", "Not available on this plan"
        )

    # ---- 3.8  Business Hours Schedules ------------------------------- #
    logger.section("3.8  Business Hours Schedules")
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

    # ---- 3.9  Routing Attributes ------------------------------------- #
    logger.section("3.9  Routing Attributes")
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
        logger.log_skipped(
            "routing_attributes", "N/A",
            "None in source or not on Enterprise plan"
        )

    # ---- 3.10  Dynamic Content --------------------------------------- #
    logger.section("3.10  Dynamic Content")
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

    # ---- 3.11  Webhooks ---------------------------------------------- #
    logger.section("3.11  Webhooks")
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

    from src.importer import flush_id_map
    flush_id_map(id_map)
    logger.success("Phase 3 — Business Logic complete.")
    return id_map


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

def _assign_form_fields(target: ZendeskClient, id_map: Dict,
                        source_forms: List[Dict]) -> None:
    """
    Phase 3.1 — Now that ticket fields exist in target, push the full
    ticket_field_ids assignment to each already-created form.

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

        payload = {
            "ticket_form": {
                "ticket_field_ids": target_field_ids,
            }
        }
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


def _prepare_trigger(item: Dict, id_map: Dict) -> Optional[Dict]:
    """
    Trigger-specific pre-processing.

    Triggers carry a top-level `category_id` referring to **trigger
    categories** (api/v2/trigger_categories) — a separate resource we don't
    migrate. The remapper's FIELD_REMAP_MAP['category_id'] points at
    `hc_categories`, which is the correct namespace for HC sections but
    wrong here. Stripping the field lets the target place the trigger in
    its default category, which is the safe fallback.
    """
    item = _prepare_rule(item, id_map)
    if item is None:
        return None
    item = dict(item)
    item.pop("category_id", None)
    return item


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
