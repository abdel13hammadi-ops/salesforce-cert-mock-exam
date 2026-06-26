"""Focused tests for Dashboard Daily Sprint domain resolution."""

from __future__ import annotations

import ast
import os
import sys
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DASHBOARD_PAGE = os.path.join(ROOT, "pages", "Dashboard.py")

HELPER_NAMES = {
    "DAILY_SPRINT_QUESTION_COUNT",
    "safe_str",
    "safe_int",
    "domain_has_sprint_capacity",
    "select_daily_sprint_fallback_domain",
    "resolve_daily_sprint_domain",
    "build_daily_sprint_href",
}


def _load_dashboard_helpers():
    with open(DASHBOARD_PAGE, encoding="utf-8") as handle:
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
    namespace = {
        "Any": __import__("typing").Any,
        "Dict": __import__("typing").Dict,
        "Optional": __import__("typing").Optional,
        "List": __import__("typing").List,
        "pd": pd,
        "quote": quote,
        "st": type("St", (), {"query_params": {}})(),
    }
    exec(compile(module_source, DASHBOARD_PAGE, "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def helpers():
    return _load_dashboard_helpers()


def test_new_premium_user_receives_valid_sprint_domain(helpers):
    domain = helpers["resolve_daily_sprint_domain"](
        {},
        pd.DataFrame(),
        {"Configuration and Setup": 12, "Object Manager": 8},
    )

    assert domain == "Configuration and Setup"
    assert helpers["domain_has_sprint_capacity"](domain, {"Configuration and Setup": 12})


def test_existing_weak_domain_targeting_still_wins(helpers):
    readiness = {"weak_domains": ["Security and Access", "Configuration and Setup"]}
    domain_df = pd.DataFrame(
        [{"Domain": "Workflow Automation", "Accuracy %": 25.0, "Correct": 1, "Total": 4}]
    )
    counts = {
        "Security and Access": 15,
        "Configuration and Setup": 20,
        "Workflow Automation": 12,
    }

    domain = helpers["resolve_daily_sprint_domain"](readiness, domain_df, counts)

    assert domain == "Security and Access"


def test_domains_with_fewer_than_ten_eligible_questions_are_skipped(helpers):
    readiness = {"weak_domains": ["Too Small"]}
    domain_df = pd.DataFrame(
        [
            {"Domain": "Too Small", "Accuracy %": 10.0, "Correct": 1, "Total": 10},
            {"Domain": "Big Enough", "Accuracy %": 80.0, "Correct": 8, "Total": 10},
        ]
    )
    counts = {"Too Small": 5, "Big Enough": 12, "Another Domain": 15}

    domain = helpers["resolve_daily_sprint_domain"](readiness, domain_df, counts)

    assert domain == "Big Enough"


def test_no_valid_domain_hides_card_safely(helpers):
    domain = helpers["resolve_daily_sprint_domain"](
        {"weak_domains": ["Tiny"]},
        pd.DataFrame([{"Domain": "Tiny", "Accuracy %": 20.0, "Correct": 1, "Total": 5}]),
        {"Tiny": 4, "Also Tiny": 9},
    )

    assert domain == ""


def test_daily_sprint_href_uses_expected_query_params(helpers):
    href = helpers["build_daily_sprint_href"](
        "pages/Practice_By_Category.py",
        "Salesforce Certified Platform Administrator",
        "Configuration and Setup",
        10,
    )
    parsed = urlparse(href)
    params = parse_qs(parsed.query)

    assert parsed.path.endswith("Practice_By_Category")
    assert params["daily_sprint"] == ["1"]
    assert params["exam_name"] == ["Salesforce Certified Platform Administrator"]
    assert params["category"] == ["Configuration and Setup"]
    assert params["count"] == ["10"]
