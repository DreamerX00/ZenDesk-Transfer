"""
Tests for phase3._topo_sort_sections — parents must precede children so nested
HC subsections aren't dropped when their parent isn't mapped yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.phases.phase3_content import _topo_sort_sections


def _order(sections):
    return [s.get("id") for s in _topo_sort_sections(sections)]


def test_child_after_parent_even_if_listed_first():
    sections = [
        {"id": 2, "parent_section_id": 1},   # child listed before parent
        {"id": 1, "parent_section_id": None},
    ]
    order = _order(sections)
    assert order.index(1) < order.index(2)


def test_deep_nesting_ordered():
    sections = [
        {"id": 3, "parent_section_id": 2},
        {"id": 2, "parent_section_id": 1},
        {"id": 1, "parent_section_id": None},
    ]
    assert _order(sections) == [1, 2, 3]


def test_category_parent_is_root():
    # parent_section_id points at a category (id not among sections) → root.
    sections = [{"id": 5, "parent_section_id": 999}]
    assert _order(sections) == [5]


def test_cycle_does_not_hang():
    sections = [
        {"id": 1, "parent_section_id": 2},
        {"id": 2, "parent_section_id": 1},
    ]
    order = _order(sections)
    assert sorted(order) == [1, 2]  # both emitted exactly once, no infinite loop


def test_all_sections_preserved():
    sections = [{"id": i, "parent_section_id": None} for i in range(5)]
    assert sorted(_order(sections)) == [0, 1, 2, 3, 4]
