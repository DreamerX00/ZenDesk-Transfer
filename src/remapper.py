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
    "active",
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
        return {
            k: _remap_value(k, v, id_map, context, _depth)
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
    Returns the remapped dict, or None if the condition must be removed
    (e.g. user references that can't be migrated).
    """
    field = obj.get("field", "")
    value = obj.get("value")

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
