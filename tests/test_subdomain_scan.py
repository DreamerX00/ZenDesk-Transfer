"""
Unit tests for source-subdomain reference detection (gap #8) in
src.remapper.find_subdomain_references.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_detects_in_nested_string() -> None:
    from src.remapper import find_subdomain_references

    item = {"actions": [{"field": "comment_value",
                         "value": "See https://acme.zendesk.com/hc/1"}]}
    assert find_subdomain_references(item, "acme") is True


def test_case_insensitive() -> None:
    from src.remapper import find_subdomain_references

    item = {"x": "HTTPS://ACME.ZENDESK.COM/page"}
    assert find_subdomain_references(item, "acme") is True


def test_no_match() -> None:
    from src.remapper import find_subdomain_references

    item = {"actions": [{"field": "status", "value": "solved"}],
            "title": "No URLs here"}
    assert find_subdomain_references(item, "acme") is False


def test_different_subdomain_not_matched() -> None:
    from src.remapper import find_subdomain_references

    item = {"value": "https://other.zendesk.com/x"}
    assert find_subdomain_references(item, "acme") is False


def test_empty_subdomain_returns_false() -> None:
    from src.remapper import find_subdomain_references

    assert find_subdomain_references({"v": "acme.zendesk.com"}, "") is False


def test_handles_lists_and_scalars() -> None:
    from src.remapper import find_subdomain_references

    assert find_subdomain_references(["a", "b", "acme.zendesk.com"], "acme") is True
    assert find_subdomain_references(123, "acme") is False
    assert find_subdomain_references(None, "acme") is False


def _run_all() -> int:
    import inspect
    mod = sys.modules[__name__]
    tests = [v for k, v in vars(mod).items()
             if k.startswith("test_") and inspect.isfunction(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
