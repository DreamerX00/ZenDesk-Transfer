"""
Regression test: dropdown/tagger custom fields must carry their options in the
CREATE payload (Zendesk rejects an option-less create with "Field options:
must contain at least one option"). Options are also stashed for the
post-create PUT fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.phases.phase1_foundation import prepare_ticket_field, prepare_user_field


def test_ticket_field_options_present_at_create_without_ids():
    item = {
        "id": 1, "title": "Region", "type": "tagger",
        "custom_field_options": [
            {"id": 111, "name": "APAC", "value": "apac", "raw_name": "APAC"},
            {"id": 222, "name": "EMEA", "value": "emea"},
        ],
    }
    out = prepare_ticket_field(item, {})
    # Options included at create...
    assert "custom_field_options" in out
    assert [o["value"] for o in out["custom_field_options"]] == ["apac", "emea"]
    # ...with source ids / raw_* stripped...
    assert all("id" not in o and "raw_name" not in o for o in out["custom_field_options"])
    # ...and stashed for the PUT fallback.
    assert out["_custom_field_options"] == out["custom_field_options"]


def test_user_field_options_present_at_create():
    item = {
        "id": 2, "key": "tier", "type": "dropdown",
        "custom_field_options": [{"id": 9, "name": "Gold", "value": "gold"}],
    }
    out = prepare_user_field(item, {})
    assert out["custom_field_options"] == [{"name": "Gold", "value": "gold"}]
    assert out["_custom_field_options"] == out["custom_field_options"]


def test_text_field_has_no_options_keys():
    item = {"id": 3, "title": "Notes", "type": "text"}
    out = prepare_ticket_field(item, {})
    assert "custom_field_options" not in out
    assert "_custom_field_options" not in out
