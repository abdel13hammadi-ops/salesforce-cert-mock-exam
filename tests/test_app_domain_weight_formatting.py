"""Focused tests for app.py domain-weight decimal handling (SCC-EXP-03A).

app.py is a top-level Streamlit script (it calls st.set_page_config() and
reads live query params at import time), so it cannot be imported directly
in a test process. Following the same convention as
tests/test_daily_sprint_dashboard.py, this file extracts only the specific
helper function/constants under test via AST and executes that extracted
source in an isolated namespace -- no Streamlit runtime is required.
"""

from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

APP_PATH = os.path.join(ROOT, "app.py")

HELPER_NAMES = {
    "FALLBACK_CATEGORY_WEIGHTS",
    "format_domain_weight",
}


def _load_app_helpers():
    with open(APP_PATH, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    selected_nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {getattr(t, "id", None) for t in node.targets}
            if targets & HELPER_NAMES:
                selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES:
            selected_nodes.append(node)
    module_source = ast.unparse(ast.Module(body=selected_nodes, type_ignores=[]))
    namespace: dict = {}
    exec(compile(module_source, APP_PATH, "exec"), namespace)
    return namespace


HELPERS = _load_app_helpers()
format_domain_weight = HELPERS["format_domain_weight"]


def test_integer_weight_displays_without_trailing_zero():
    assert format_domain_weight(23) == "23"
    assert format_domain_weight(23.0) == "23"
    assert format_domain_weight(100) == "100"


def test_decimal_weight_is_preserved_exactly():
    assert format_domain_weight(23.3) == "23.3"
    assert format_domain_weight(18.3) == "18.3"
    assert format_domain_weight(13.3) == "13.3"
    assert format_domain_weight(20.0) == "20"
    assert format_domain_weight(25.0) == "25"


def test_decimal_weight_surviving_string_round_trip_from_supabase():
    # Supabase's Python client can surface numeric columns as strings; the
    # formatter must not truncate those either.
    assert format_domain_weight("23.3") == "23.3"
    assert format_domain_weight("18.3") == "18.3"
    assert format_domain_weight("15") == "15"


def test_zero_and_missing_weight_fall_back_safely():
    assert format_domain_weight(0) == "0"
    assert format_domain_weight(None) == "0"
    assert format_domain_weight("") == "0"


def test_non_numeric_weight_does_not_raise():
    assert format_domain_weight("not-a-number") == "0"


def test_no_remaining_int_cast_on_supabase_weight_column():
    with open(APP_PATH, encoding="utf-8") as handle:
        source = handle.read()
    assert 'int(d.get("weight")' not in source
    assert 'int(row.get("weight")' not in source
    assert 'd["domain_name"]: float(d.get("weight") or 0)' in source


def test_fallback_category_weights_are_all_integral_and_unchanged():
    # Administrator's hardcoded fallback (used only when Supabase is
    # unreachable) must remain byte-for-byte the same integer percentages
    # after the numeric(5,1) widening -- this dict is Python-only and is
    # never read from the widened database column.
    weights = HELPERS["FALLBACK_CATEGORY_WEIGHTS"]
    assert sum(weights.values()) == 100
    for domain, weight in weights.items():
        assert weight == int(weight), f"{domain} fallback weight must stay integral"
