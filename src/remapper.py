"""
Remapper — recursively walks a Zendesk API payload and replaces
all source-account IDs with their target-account equivalents.

Bug fixes vs v1:
  - remap_payload now has a recursion depth guard to prevent stack overflow
    on pathologically nested payloads.
  - _lookup return value validated before int() conversion — avoids
    TypeError/ValueError on corrupt map entries.
  - strip_source_fields now also strips positional/internal Zendesk fields
    that cause 422 errors when POSTed verbatim.
"""

import re
from typing import Any, Dict, List, Optional

from src.utils import logger

# Maps known ID field names → the id_map category that holds their mapping
#
# NOTE: 'category_id' here refers to HC categories (used by HC sections during
# create). Triggers ALSO have a top-level 'category_id' that points at a
# trigger category (a totally separate resource we don't migrate) — that one
# is stripped upstream by the trigger pre-process hook in phase2, so it never
# reaches this remapper.
FIELD_REMAP_MAP: Dict[str, str] = {
    "group_id":            "groups",
    "brand_id":            "brands",
    "ticket_field_id":     "ticket_fields",
    "form_id":             "ticket_forms",
    "organization_id":     "organizations",
    "ticket_form_id":      "ticket_forms",
    "user_segment_id":     "hc_user_segments",
    "category_id":         "hc_categories",
    "section_id":          "hc_sections",
}

# Regex to detect custom field keys embedded as condition field names
# e.g. "custom_fields_360012345678"
CUSTOM_FIELD_PATTERN = re.compile(r"^custom_fields_(\d+)$")

# Maps view/trigger/automation condition field names to id_map categories.
# When a condition/action item has {"field": "group_id", "value": "12345"},
# the "value" is remapped using the "groups" category from id_map.
CONDITION_VALUE_MAP: Dict[str, str] = {
    "group_id":            "groups",
    "organization_id":     "organizations",
    "assignee_id":         "users",   # users are now migrated — remap
    "requester_id":        "users",   # users are now migrated — remap
    "current_user_id":     "users",
    "brand_id":            "brands",
    "ticket_form_id":      "ticket_forms",
    "custom_role_id":      "custom_roles",
    "category_id":         "hc_categories",
    "section_id":          "hc_sections",
    "user_field_*":        "user_fields",
    "ticket_type_id":      None,      # system field — no remapping needed
}

# Maximum recursion depth for payload walking (guard against deeply nested payloads)
MAX_DEPTH = 30

# Fields that must be stripped before POSTing to target
# Expanded to include all account-scoped, read-only, and server-managed fields
STRIP_FIELDS = frozenset({
    "id", "url", "created_at", "updated_at",
    "raw_subject", "raw_title", "raw_description", "raw_body",
    "html_url",          # account-scoped URL
    "source_url",        # account-scoped
    "subdomain",         # brand-specific
    "host_mapping",      # brand-specific
    "zendesk_support_address",  # email address tied to source domain
    "owner_id",          # users not migrated — view becomes shared
    # HC permission / segment IDs are account-scoped and not migrated as their
    # own resources today. Stripping causes target to assign defaults — safer
    # than posting source IDs that 400 the request. See remapper docstring.
    "permission_group_id",
    "user_segment_id",
    # User-specific read-only / server-managed fields
    "last_login_at",
    "two_factor_auth_enabled",
    "shared_phone_number",
    "shared_agent",
    "photo",
    "password",
    "phone",
    "identities",
    "custom_role_id",
    "moderator",
    "only_private_comments",
    "restricted_agent",
    "suspended",
    "role_type",
    "confirmed",
})


class RemapError(Exception):
    """Raised when a required ID mapping is not found."""


def remap_payload(obj: Any, id_map: Dict[str, Dict[str, str]],
                  context: str = "", _depth: int = 0) -> Any:
    """
    Recursively walk obj (dict or list) and remap all known ID fields.
    Raises RemapError if a required ID is missing from id_map.
    Respects MAX_DEPTH to prevent stack overflow on adversarial payloads.
    """
    if _depth > MAX_DEPTH:
        logger.warn(
            f"remap_payload: max recursion depth ({MAX_DEPTH}) exceeded "
            f"at context='{context}'. Returning value unchanged."
        )
        return obj

    if isinstance(obj, dict):
        # Detect condition/action items: {"field": "group_id", "value": "12345"}
        if _is_condition_item(obj):
            return _remap_condition_item(obj, id_map, context, _depth)
        # Keys prefixed with "_" are internal stashes set by phase pre-process
        # hooks (e.g. "_trigger_category_id", "_macro_actions"). They hold
        # values that must NOT be walked/remapped here — the owning
        # post-process hook handles them. Pass them through untouched.
        return {
            k: (v if isinstance(k, str) and k.startswith("_")
                else _remap_value(k, v, id_map, context, _depth))
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        result = [
            remap_payload(item, id_map, context, _depth + 1)
            for item in obj
        ]
        return [r for r in result if r is not None]
    return obj


def _is_condition_item(obj: Dict) -> bool:
    """Check if a dict looks like a Zendesk condition/action item."""
    return bool(
        isinstance(obj, dict)
        and isinstance(obj.get("field"), str)
        and "value" in obj
    )


def _remap_condition_item(obj: Dict, id_map: Dict,
                           context: str, depth: int) -> Any:
    """
    Remap a condition/action item like {"field": "group_id", "value": "12345"}.
    The 'value' is remapped based on the 'field' name.

    Also handles custom_fields_<id> in the 'field' key — the embedded ticket
    field ID must be remapped from the source ID to the target ID, otherwise
    the condition/action references a non-existent field on the target and is
    silently ignored by Zendesk (causing triggers to fire incorrectly).

    Returns the remapped dict, or None if the condition must be removed
    (e.g. user references that can't be migrated).
    """
    field = obj.get("field", "")
    value = obj.get("value")

    # --- Handle custom_fields_<id> in the field key (ticket field ID remap) --- #
    # Conditions/actions like {"field": "custom_fields_360012345678", ...}
    # embed the ticket field ID in the field name itself. This ID must be
    # remapped to the target's equivalent, otherwise the whole condition
    # silently fails and the trigger fires incorrectly.
    m = CUSTOM_FIELD_PATTERN.match(field)
    if m:
        source_field_id = m.group(1)
        mapped_id = _lookup(
            "ticket_fields", source_field_id, id_map,
            context, "custom_field_condition_key",
            raise_on_miss=False,
        )
        if mapped_id is not None:
            if isinstance(mapped_id, str) and mapped_id.isdigit():
                obj = dict(obj)
                obj["field"] = f"custom_fields_{mapped_id}"
            else:
                logger.warn(
                    f"Mapped ticket_field ID '{mapped_id}' is not a digit string "
                    f"— keeping original field '{field}' (context: {context})"
                )
        else:
            # Target has no matching ticket field — this condition/action
            # cannot work. Strip it so the rule doesn't silently malfunction.
            logger.warn(
                f"Stripping condition/action referencing non-migrated "
                f"ticket field (field='{field}', context='{context}')"
            )
            return None
        # custom_field values are option texts/tags, not IDs — no value remap
        return obj

    category = CONDITION_VALUE_MAP.get(field)

    if category is None and field.startswith("user_field_"):
        category = "user_fields"

    if category is None:
        return obj

    if not value:
        return obj

    # Special cases — these values don't need remapping
    if field == "current_user_id" and str(value) in ("1", "current_user"):
        return obj  # literal sentinel, not a real user ID

    # Attempt the remap
    mapped = _lookup(category, str(value), id_map, context, field,
                     raise_on_miss=False)
    if mapped is not None:
        obj = dict(obj)
        # Views/Triggers/Automations require condition `value` to be a STRING.
        # Posting an int produces: "value must be a string from
        # api/v2/rules/views/create". Always coerce to str here — top-level
        # scalar ID fields (group_id, form_id, etc.) are handled in
        # _remap_value where int conversion IS correct.
        obj["value"] = str(mapped)
    elif category == "users":
        # If the users category doesn't exist in id_map at all (users haven't
        # been migrated yet — they're Phase 5, runs after business logic),
        # leave the condition/action as-is. The source user ID won't match
        # anyone on target, so the condition is inert but structurally preserved.
        if "users" not in id_map:
            # Only warn once — the first time we skip a user remap
            logger.warn(
                f"Skipping user ID remap — 'users' category absent from id_map "
                f"(field='{field}', context='{context}'). "
                f"Value '{value}' left as-is (inert on target until user migration)."
            )
            return obj
        # User ID not found in id_map — remove this condition
        logger.warn(
            f"Stripping condition/action referencing non-migrated user "
            f"(field='{field}', value='{value}', context='{context}')"
        )
        return None
    else:
        # Mapping miss for a non-user reference (e.g. a deleted group or org
        # that no longer exists in source). Posting the orphaned source ID
        # causes a 422 on the target. Drop the entire condition rather than
        # poisoning the whole rule.
        logger.warn(
            f"Stripping condition/action referencing missing "
            f"{category}[{value}] (field='{field}', context='{context}')"
        )
        return None

    return obj


def _remap_value(key: str, value: Any, id_map: Dict,
                 context: str, depth: int) -> Any:
    # Direct field name match
    if key in FIELD_REMAP_MAP and value is not None:
        category = FIELD_REMAP_MAP[key]
        mapped = _lookup(category, str(value), id_map, context, key)
        if mapped is None:
            return value  # _lookup raises on miss; None means raise_on_miss=False
        # Safe int conversion — validate mapped is a digit string before int()
        if isinstance(mapped, str) and mapped.isdigit():
            return int(mapped)
        return mapped  # return as-is if format unexpected (logged by _lookup)

    # Condition/action arrays — remap 'field' keys that are custom_fields_<id>
    if key == "field" and isinstance(value, str):
        m = CUSTOM_FIELD_PATTERN.match(value)
        if m:
            source_field_id = m.group(1)
            mapped_id = _lookup(
                "ticket_fields", source_field_id, id_map,
                context, "custom_field_condition_key",
                raise_on_miss=False,
            )
            if mapped_id is not None:
                # Validate mapped_id is digits before embedding in key name
                if isinstance(mapped_id, str) and mapped_id.isdigit():
                    return f"custom_fields_{mapped_id}"
                logger.warn(
                    f"Mapped ticket_field ID '{mapped_id}' is not a digit string "
                    f"— keeping original key '{value}' (context: {context})"
                )

    # View/trigger restriction: {"type": "Group", "id": "12345"}
    if key == "restriction" and isinstance(value, dict):
        restriction_id = value.get("id")
        restriction_type = value.get("type", "")
        if restriction_id and restriction_type in ("Group", "User"):
            cat = "groups" if restriction_type == "Group" else "users"
            mapped = _lookup(cat, str(restriction_id), id_map,
                             context, "restriction.id", raise_on_miss=False)
            if mapped is not None:
                value = dict(value)
                if isinstance(mapped, str) and mapped.isdigit():
                    value["id"] = int(mapped)
                else:
                    value["id"] = mapped
                return value
            else:
                logger.warn(
                    f"Stripping view restriction that references "
                    f"non-existent {cat[:-1]} {restriction_id} "
                    f"(context: {context})"
                )
                return None

    # Recurse into nested dicts/lists
    if isinstance(value, (dict, list)):
        return remap_payload(value, id_map, context, depth + 1)

    return value


def _lookup(category: str, source_id: str, id_map: Dict,
            context: str, field: str,
            raise_on_miss: bool = True) -> Optional[str]:
    """
    Look up a source ID in the mapping table.
    Returns the target ID string, or None if not found (and raise_on_miss=False).
    """
    if not isinstance(id_map, dict):
        raise RemapError(f"id_map is not a dict (got {type(id_map).__name__})")

    mapping = id_map.get(category)
    if not isinstance(mapping, dict):
        msg = (
            f"No mapping category '{category}' in id_map "
            f"(field='{field}', context='{context}')"
        )
        if raise_on_miss:
            raise RemapError(msg)
        logger.warn(msg)
        return None

    result = mapping.get(str(source_id))
    if result is None:
        msg = (
            f"Missing ID map for {category}[{source_id}] "
            f"(field='{field}', context='{context}')"
        )
        if raise_on_miss:
            raise RemapError(msg)
        logger.warn(msg)
    return result


def remap_form_conditions(
    conditions: Any,
    id_map: Dict,
    context: str = "",
) -> List[Dict]:
    """
    Remap a ticket form's conditional-field rules (agent_conditions /
    end_user_conditions) from source IDs to target IDs.

    Each condition has the shape:
        {
          "parent_field_id": <source ticket field id>,
          "value": "<option value/tag>",   # the selected drop-down value
          "child_fields": [
            {"id": <source ticket field id>, "is_required": bool,
             "required_on_statuses": {...}},
            ...
          ]
        }

    `parent_field_id` and every `child_fields[].id` are ticket field IDs that
    must be translated through id_map["ticket_fields"]. The `value` is an
    option tag/text (not an ID) and is preserved verbatim.

    Behavior on a mapping miss:
      - If the parent field has no target equivalent, the whole condition is
        dropped (it can't anchor to a non-existent field).
      - Individual child fields with no target equivalent are dropped from the
        child list; remaining children are kept.

    Returns a new list of remapped conditions. Non-list / malformed input
    yields an empty list.
    """
    if not isinstance(conditions, list):
        return []

    field_map = id_map.get("ticket_fields")
    if not isinstance(field_map, dict):
        logger.warn(
            f"remap_form_conditions: 'ticket_fields' map missing/corrupt "
            f"(context='{context}') — dropping all conditions."
        )
        return []

    def _map_field_id(raw_id: Any) -> Optional[int]:
        if raw_id is None:
            return None
        mapped = field_map.get(str(raw_id))
        if isinstance(mapped, str) and mapped.isdigit():
            return int(mapped)
        if isinstance(mapped, int):
            return mapped
        return None

    remapped: List[Dict] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue

        parent_target = _map_field_id(cond.get("parent_field_id"))
        if parent_target is None:
            logger.warn(
                f"Stripping form condition referencing non-migrated parent "
                f"field {cond.get('parent_field_id')!r} (context='{context}')"
            )
            continue

        new_children: List[Dict] = []
        for child in cond.get("child_fields") or []:
            if not isinstance(child, dict):
                continue
            child_target = _map_field_id(child.get("id"))
            if child_target is None:
                logger.warn(
                    f"Dropping conditional child field {child.get('id')!r} "
                    f"with no target equivalent (context='{context}')"
                )
                continue
            new_child = dict(child)
            new_child["id"] = child_target
            new_children.append(new_child)

        new_cond = dict(cond)
        new_cond["parent_field_id"] = parent_target
        new_cond["child_fields"] = new_children
        remapped.append(new_cond)

    return remapped


# ------------------------------------------------------------------ #
#  System ticket field mapping                                        #
# ------------------------------------------------------------------ #
#
# Phase 1 never *creates* system ticket fields (Subject, Status, Priority,
# Type, Group, Assignee, Ticket status, ...) — every Zendesk account already
# has exactly one of each. But they carry account-specific IDs, so a form's
# `ticket_field_ids` and its conditional rules can reference a source system
# field ID that has a different ID on the target.
#
# Because there is exactly one field of each system `type` per account, they
# can be matched across accounts by `type`. This is what lets a condition
# anchored on the system "Type"/"Priority" field — or one that makes a system
# field a conditional child — survive migration instead of being dropped.
#
# `type` values are exactly what the Zendesk API returns: the "Type" field is
# `tickettype` and the "Ticket status" field is `custom_status`.
SYSTEM_TICKET_FIELD_TYPES = frozenset({
    "subject", "description", "status", "custom_status",
    "tickettype", "type", "priority", "group", "assignee",
    "requester", "organization", "tags", "ticket_form_id",
    "brand_id", "due_at",
})


def build_system_field_map(
    source_fields: Any, target_fields: Any,
) -> Dict[str, str]:
    """
    Build a ``{source_id: target_id}`` map for SYSTEM ticket fields by matching
    on their ``type``.

    Inputs are the raw ``ticket_fields`` lists from each account (as returned
    by ``GET /api/v2/ticket_fields``). Custom fields are ignored here — those
    are mapped by Phase 1 into ``id_map["ticket_fields"]``. Returns string keys
    and values to match the id_map convention; malformed entries are skipped.
    """
    target_by_type: Dict[str, Any] = {}
    if isinstance(target_fields, list):
        for f in target_fields:
            if not isinstance(f, dict):
                continue
            ftype = f.get("type")
            fid = f.get("id")
            if ftype in SYSTEM_TICKET_FIELD_TYPES and fid is not None:
                # Exactly one system field per type — first one wins.
                target_by_type.setdefault(ftype, fid)

    mapping: Dict[str, str] = {}
    if isinstance(source_fields, list):
        for f in source_fields:
            if not isinstance(f, dict):
                continue
            ftype = f.get("type")
            sid = f.get("id")
            if ftype in SYSTEM_TICKET_FIELD_TYPES and sid is not None:
                tid = target_by_type.get(ftype)
                if tid is not None:
                    mapping[str(sid)] = str(tid)
    return mapping


# Macro action fields whose `value` is a single ID that must be remapped,
# mapped to the id_map category that resolves them.
_MACRO_SCALAR_ID_ACTIONS: Dict[str, str] = {
    "group_id": "groups",
    "brand_id": "brands",
    "ticket_form_id": "ticket_forms",
}


def remap_macro_actions(actions: Any, id_map: Dict, context: str = "") -> List[Dict]:
    """
    Remap the `actions` array of a macro from source IDs to target IDs.

    Macro actions are `{"field": <name>, "value": <value>}` items. Most fields
    (set_tags, comment_value, subject, status, priority, ...) carry no IDs and
    pass through untouched. The ID-bearing fields handled here:

      - group_id / brand_id / ticket_form_id:
            `value` is a single source ID → remapped via id_map.
      - assignee_id:
            Zendesk stores this as the composite string "<group_id>/<user_id>"
            (either side may be empty, e.g. "123/" or "/456"). Each present
            half is remapped independently through groups / users.
      - custom_fields_<id> (the field name embeds a ticket field ID):
            the embedded ID is remapped via ticket_fields; the value (an option
            tag/text) is preserved.

    Drop semantics (mirrors remap_form_conditions / _remap_condition_item):
      - An action whose required ID can't be resolved is dropped, so the macro
        never references a non-existent resource on the target.
      - For the composite assignee_id, if neither half resolves the action is
        dropped; if one half resolves it is kept with the unresolved half blank.

    Returns a new list of remapped actions. Non-list input yields [].
    """
    if not isinstance(actions, list):
        return []

    out: List[Dict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        field = action.get("field")
        if not isinstance(field, str):
            out.append(action)
            continue

        # --- custom_fields_<id>: remap the embedded ticket field ID --- #
        m = CUSTOM_FIELD_PATTERN.match(field)
        if m:
            mapped_id = _lookup(
                "ticket_fields", m.group(1), id_map, context,
                "macro_custom_field_action", raise_on_miss=False,
            )
            if isinstance(mapped_id, str) and mapped_id.isdigit():
                new_action = dict(action)
                new_action["field"] = f"custom_fields_{mapped_id}"
                out.append(new_action)
            else:
                logger.warn(
                    f"Dropping macro action referencing non-migrated ticket "
                    f"field (field='{field}', context='{context}')"
                )
            continue

        # --- assignee_id composite "<group_id>/<user_id>" --- #
        if field == "assignee_id":
            remapped_val = _remap_assignee_composite(
                action.get("value"), id_map, context
            )
            if remapped_val is None:
                logger.warn(
                    f"Dropping macro 'assignee_id' action — neither group nor "
                    f"user half resolved (value={action.get('value')!r}, "
                    f"context='{context}')"
                )
                continue
            new_action = dict(action)
            new_action["value"] = remapped_val
            out.append(new_action)
            continue

        # --- scalar single-ID fields --- #
        category = _MACRO_SCALAR_ID_ACTIONS.get(field)
        if category is not None:
            value = action.get("value")
            if value in (None, "", "current_groups", "current_user"):
                # Sentinels / blanks — leave as-is.
                out.append(action)
                continue
            mapped = _lookup(category, str(value), id_map, context, field,
                             raise_on_miss=False)
            if mapped is None:
                logger.warn(
                    f"Dropping macro action referencing missing "
                    f"{category}[{value}] (field='{field}', context='{context}')"
                )
                continue
            new_action = dict(action)
            new_action["value"] = str(mapped)
            out.append(new_action)
            continue

        # Non-ID action — pass through unchanged.
        out.append(action)

    return out


def _remap_assignee_composite(value: Any, id_map: Dict,
                              context: str) -> Optional[str]:
    """
    Remap a macro assignee_id value of the form "<group_id>/<user_id>".

    Returns the remapped "<g>/<u>" string, or None if the value is malformed
    or no half could be resolved. Either half may be empty in the source
    (e.g. "123/" sets only the group); empty halves stay empty.
    """
    if not isinstance(value, str) or "/" not in value:
        return None

    group_part, _, user_part = value.partition("/")
    group_part = group_part.strip()
    user_part = user_part.strip()

    new_group = ""
    new_user = ""
    resolved_any = False

    if group_part:
        mapped = _lookup("groups", group_part, id_map, context,
                         "assignee_id.group", raise_on_miss=False)
        if mapped is not None:
            new_group = str(mapped)
            resolved_any = True
    if user_part:
        mapped = _lookup("users", user_part, id_map, context,
                         "assignee_id.user", raise_on_miss=False)
        if mapped is not None:
            new_user = str(mapped)
            resolved_any = True

    if not resolved_any:
        return None
    return f"{new_group}/{new_user}"


def find_subdomain_references(obj: Any, source_subdomain: str,
                              _depth: int = 0) -> bool:
    """
    Recursively scan a payload for any string containing the source account's
    Zendesk subdomain host (e.g. "acme.zendesk.com"). Returns True as soon as
    one is found.

    Used to surface (not auto-rewrite) hard-coded source-portal URLs in macros,
    dynamic content, and articles, which break after migration because the
    target brand gets a freshly-generated subdomain. Auto-rewriting is unsafe
    (it could corrupt legitimate references), so callers report a MANUAL item.
    """
    if not source_subdomain or _depth > MAX_DEPTH:
        return False
    needle = f"{source_subdomain}.zendesk.com".lower()

    if isinstance(obj, str):
        return needle in obj.lower()
    if isinstance(obj, dict):
        return any(
            find_subdomain_references(v, source_subdomain, _depth + 1)
            for v in obj.values()
        )
    if isinstance(obj, list):
        return any(
            find_subdomain_references(v, source_subdomain, _depth + 1)
            for v in obj
        )
    return False


def sanitize_custom_field_values(values: Any) -> Optional[Dict]:
    """
    Sanitize a user/organization `custom_fields` (a.k.a. `user_fields` /
    `organization_fields`) value map for safe re-send to the target.

    These values are keyed by the field *key* (a stable string the field
    definition carries across accounts), NOT by numeric field ID — so as long
    as the field definitions migrate with the same key (Phase 1 imports them
    with name_field="key"), the values transfer without ID remapping.

    Sanitization:
      - Returns a plain dict of {key: value} (a shallow copy).
      - Drops entries whose value is None (Zendesk treats absent == unset; a
        null can clobber a default or 400 on strict fields).
      - Drops non-string keys defensively.
      - Returns None when there is nothing to send, so callers can omit the
        field entirely rather than posting an empty object.
    """
    if not isinstance(values, dict):
        return None
    cleaned = {
        k: v
        for k, v in values.items()
        if isinstance(k, str) and v is not None
    }
    return cleaned or None


def strip_source_fields(payload: Dict) -> Dict:
    """
    Remove all fields that must not be sent to the target account.
    Also strips any key whose value is exactly the string 'None' —
    which would indicate a corrupt mapping was previously recorded.
    """
    if not isinstance(payload, dict):
        return payload

    cleaned = {}
    for k, v in payload.items():
        if k in STRIP_FIELDS:
            continue
        # Guard against stringified None values from a corrupt id_map
        if v == "None" and k.endswith("_id"):
            logger.warn(
                f"strip_source_fields: field '{k}' has value 'None' "
                "(possible corrupt id_map entry) — stripping field."
            )
            continue
        cleaned[k] = v
    return cleaned
