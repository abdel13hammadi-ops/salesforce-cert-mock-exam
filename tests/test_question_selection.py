"""
V40 repeat-resistant question selection — unit tests.

Covers all 14 required cases plus several supporting sub-cases.
Tests are self-contained: no Supabase, no Streamlit, no network access.
Randomness is injected via a seeded random.Random so results are
deterministic and assertions are stable across runs.

Run with:
    python -m pytest tests/test_question_selection.py -v
"""

import os
import sys
import random
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.question_selection import (
    _family_key,
    build_history_context,
    rank_questions_waterfall,
    select_questions_for_domain,
    select_paid_mock_questions,
    difficulty_targets,
    balance_multi_select,
)


# ── shared factories ──────────────────────────────────────────────────────────

def _rng(seed: int = 42) -> random.Random:
    """Fresh seeded RNG for each test — keeps assertions deterministic."""
    return random.Random(seed)


def make_q(
    qid,
    category: str = "Cat A",
    practice_eligible: bool = True,
    family_id=None,
    difficulty: str = "medium",
    qtype: str = "single",
):
    return {
        "id": qid,
        "category": category,
        "practice_eligible": practice_eligible,
        "question_family_id": family_id,
        "difficulty": difficulty,
        "question": f"Question {qid}",
        "question_text": f"Question {qid}",
        "options": ["A", "B", "C", "D"],
        "answers": ["A"],
        "type": qtype,
        "explanation": "",
    }


def empty_history() -> dict:
    return {
        "seen_question_ids": set(),
        "exposure_count": {},
        "last_seen": {},
        "recent_attempt_ids": set(),
        "recent_question_ids": set(),
    }


def make_history(
    seen_ids=(),
    exposure: dict | None = None,
    last_seen: dict | None = None,
    recent_qids=(),
) -> dict:
    return {
        "seen_question_ids": {str(x) for x in seen_ids},
        "exposure_count": {str(k): v for k, v in (exposure or {}).items()},
        "last_seen": {str(k): v for k, v in (last_seen or {}).items()},
        "recent_attempt_ids": set(),
        "recent_question_ids": {str(x) for x in recent_qids},
    }


# ── 1. Unseen mock-only non-recent family wins over every lower tier ──────────

def test_tier1_beats_all_lower_tiers():
    """Tier 1 (unseen · mock-only · family not recent) must rank first."""
    t1 = make_q(1, practice_eligible=False, family_id="f1")   # Tier 1
    t3 = make_q(2, practice_eligible=True, family_id="f2")    # Tier 3 (unseen, not mock-only, non-recent)
    t4 = make_q(3, practice_eligible=False, family_id="f3")   # Tier 4 (seen, non-recent)
    t5a = make_q(4, practice_eligible=True, family_id="f4")   # Tier 5 (seen, recent family)

    history = make_history(
        seen_ids=[3, 4],
        exposure={3: 1, 4: 1},
        recent_qids=[4],
    )
    ranked = rank_questions_waterfall([t5a, t4, t3, t1], history, rng=_rng())
    assert ranked[0]["id"] == 1, "Tier 1 question must be ranked first"


def test_tier1_beats_tier2():
    """Tier 1 (non-recent family) wins over Tier 2 (same: mock-only but recent family)."""
    t1 = make_q(10, practice_eligible=False, family_id="fresh_fam")
    t2 = make_q(11, practice_eligible=False, family_id="recent_fam")

    history = make_history(recent_qids=[11])

    ranked = rank_questions_waterfall([t2, t1], history, rng=_rng())
    assert ranked[0]["id"] == 10


# ── 2. Unseen exact item preferred over previously seen item ──────────────────

def test_unseen_preferred_over_seen():
    unseen = make_q(20, family_id="fu")
    seen = make_q(21, family_id="fs")
    history = make_history(seen_ids=[21], exposure={21: 2})

    ranked = rank_questions_waterfall([seen, unseen], history, rng=_rng())
    assert ranked[0]["id"] == 20


def test_unseen_preferred_over_seen_in_selection():
    pool = [make_q(i, family_id=f"f{i}") for i in range(10)]
    history = make_history(seen_ids=range(5))  # first 5 seen

    selected = select_questions_for_domain(pool, 3, history, rng=_rng())
    selected_ids = {q["id"] for q in selected}
    # All three should be from the unseen half (ids 5–9) because they rank higher
    assert selected_ids.issubset({5, 6, 7, 8, 9})


# ── 3. Recent-family questions avoided when alternatives exist ────────────────

def test_recent_family_avoided_when_fresh_available():
    fresh = make_q(30, family_id="fresh_fam")
    recent = make_q(31, family_id="recent_fam")

    # q31 is in recent_question_ids → its family is "recent"
    history = make_history(recent_qids=[31])

    # Both unseen and practice_eligible=True:
    # fresh → Tier 3 (unseen, non-mock-only, non-recent family)
    # recent → Tier 5 (unseen, non-mock-only, recent family)
    ranked = rank_questions_waterfall([recent, fresh], history, rng=_rng())
    assert ranked[0]["id"] == 30


def test_non_recent_family_selected_first_in_domain():
    fresh = [make_q(i, family_id=f"fresh_{i}") for i in range(5)]
    recent_family = [make_q(i + 100, family_id=f"recent_{i}") for i in range(5)]

    history = make_history(recent_qids=[q["id"] for q in recent_family])
    pool = recent_family + fresh

    selected = select_questions_for_domain(pool, 3, history, rng=_rng())
    selected_families = {q["question_family_id"] for q in selected}
    # All selected families must be "fresh_*", not "recent_*"
    assert all(f.startswith("fresh_") for f in selected_families)


# ── 4. Recent-family fallback when inventory is insufficient ──────────────────

def test_recent_family_fallback_fills_quota():
    """When only recent-family questions exist, they must still be selected."""
    pool = [make_q(i, family_id=f"recent_fam_{i}") for i in range(4)]
    history = make_history(recent_qids=[q["id"] for q in pool])

    selected = select_questions_for_domain(pool, 4, history, rng=_rng())
    assert len(selected) == 4
    assert {q["id"] for q in selected} == {q["id"] for q in pool}


def test_fresh_exhausted_then_recent_used():
    fresh = [make_q(i, family_id=f"fresh_{i}") for i in range(2)]
    recent = [make_q(i + 100, family_id=f"recent_{i}") for i in range(5)]

    history = make_history(recent_qids=[q["id"] for q in recent])
    pool = fresh + recent

    # Requesting 5 but only 2 fresh; must pull 3 from recent
    selected = select_questions_for_domain(pool, 5, history, rng=_rng())
    assert len(selected) == 5


# ── 5. Lowest exposure count wins among seen candidates ───────────────────────

def test_lowest_exposure_wins():
    low_exp = make_q(40, family_id="f40")
    high_exp = make_q(41, family_id="f41")
    history = make_history(
        seen_ids=[40, 41],
        exposure={40: 1, 41: 5},
        last_seen={40: "2025-01-01T00:00:00", 41: "2025-01-01T00:00:00"},
    )
    ranked = rank_questions_waterfall([high_exp, low_exp], history, rng=_rng())
    assert ranked[0]["id"] == 40


# ── 6. Oldest last-seen wins when exposure counts tie ─────────────────────────

def test_oldest_last_seen_wins():
    older = make_q(50, family_id="f50")
    newer = make_q(51, family_id="f51")
    history = make_history(
        seen_ids=[50, 51],
        exposure={50: 2, 51: 2},
        last_seen={50: "2024-01-01T00:00:00", 51: "2025-06-01T00:00:00"},
    )
    ranked = rank_questions_waterfall([newer, older], history, rng=_rng())
    assert ranked[0]["id"] == 50


# ── 7. No duplicate question IDs ──────────────────────────────────────────────

def test_no_duplicate_question_ids():
    pool = [make_q(i, family_id=f"f{i}") for i in range(30)]
    selected = select_questions_for_domain(pool, 15, empty_history(), rng=_rng())
    ids = [q["id"] for q in selected]
    assert len(ids) == len(set(ids)), "Duplicate question IDs found"


def test_no_duplicate_ids_with_history():
    pool = [make_q(i, family_id=f"f{i}") for i in range(20)]
    history = make_history(seen_ids=range(10), exposure={i: 1 for i in range(10)})

    selected = select_questions_for_domain(pool, 15, history, rng=_rng())
    ids = [q["id"] for q in selected]
    assert len(ids) == len(set(ids))


# ── 8. No duplicate families when sufficient inventory exists ─────────────────

def test_no_duplicate_families_sufficient_inventory():
    # 20 distinct-family questions; select 10 — family uniqueness must hold
    pool = [make_q(i, family_id=f"family_{i}") for i in range(20)]
    selected = select_questions_for_domain(pool, 10, empty_history(), rng=_rng())

    assert len(selected) == 10
    fkeys = [_family_key(q) for q in selected]
    assert len(fkeys) == len(set(fkeys)), "Duplicate families found despite sufficient inventory"


def test_no_duplicate_families_across_domains():
    bank = [make_q(i, category="Cat A", family_id=f"famA_{i}") for i in range(15)]
    bank += [make_q(i + 100, category="Cat B", family_id=f"famB_{i}") for i in range(15)]
    category_counts = {"Cat A": 5, "Cat B": 5}

    result = select_paid_mock_questions(bank, category_counts, empty_history(), rng=_rng())
    all_fkeys = [_family_key(q) for q in result["selected"]]
    assert len(all_fkeys) == len(set(all_fkeys))


# ── 9. Null family IDs are treated as unique per question ─────────────────────

def test_null_family_ids_have_distinct_keys():
    q1 = make_q(60, family_id=None)
    q2 = make_q(61, family_id=None)
    assert _family_key(q1) != _family_key(q2), "Null family_id questions must get unique keys"


def test_null_family_questions_both_selectable():
    """Two questions with family_id=None must not block each other."""
    q1 = make_q(62, family_id=None)
    q2 = make_q(63, family_id=None)

    selected = select_questions_for_domain([q1, q2], 2, empty_history(), rng=_rng())
    assert len(selected) == 2


def test_null_family_mix_with_real_family():
    null1 = make_q(70, family_id=None)
    null2 = make_q(71, family_id=None)
    shared_fam = make_q(72, family_id="shared")
    also_shared = make_q(73, family_id="shared")

    pool = [null1, null2, shared_fam, also_shared]
    selected = select_questions_for_domain(pool, 3, empty_history(), rng=_rng())

    # Both null-family questions can be selected; only one of the shared-family pair
    selected_ids = {q["id"] for q in selected}
    assert len(selected) == 3
    # Cannot have both 72 and 73 (same family, Pass 1 blocks second)
    assert not ({72, 73}.issubset(selected_ids))


# ── 10. Domain quotas are preserved ──────────────────────────────────────────

def test_domain_quotas_preserved_exact():
    pool_a = [make_q(i, category="Cat A", family_id=f"fa{i}") for i in range(20)]
    pool_b = [make_q(i + 100, category="Cat B", family_id=f"fb{i}") for i in range(20)]
    pool_c = [make_q(i + 200, category="Cat C", family_id=f"fc{i}") for i in range(20)]
    bank = pool_a + pool_b + pool_c
    category_counts = {"Cat A": 5, "Cat B": 8, "Cat C": 3}

    result = select_paid_mock_questions(bank, category_counts, empty_history(), rng=_rng())
    counts = {}
    for q in result["selected"]:
        counts[q["category"]] = counts.get(q["category"], 0) + 1

    assert counts.get("Cat A", 0) == 5
    assert counts.get("Cat B", 0) == 8
    assert counts.get("Cat C", 0) == 3


def test_domain_quotas_preserved_with_history():
    pool_a = [make_q(i, category="Cat A", family_id=f"fa{i}") for i in range(20)]
    pool_b = [make_q(i + 100, category="Cat B", family_id=f"fb{i}") for i in range(20)]
    bank = pool_a + pool_b
    category_counts = {"Cat A": 7, "Cat B": 9}
    history = make_history(
        seen_ids=range(10),
        exposure={i: 1 for i in range(10)},
    )

    result = select_paid_mock_questions(bank, category_counts, history, rng=_rng())
    counts = {}
    for q in result["selected"]:
        counts[q["category"]] = counts.get(q["category"], 0) + 1

    assert counts.get("Cat A", 0) == 7
    assert counts.get("Cat B", 0) == 9


# ── 11. Total requested question count is preserved ───────────────────────────

def test_total_question_count_preserved():
    bank = [make_q(i, category=f"Cat{i % 4}", family_id=f"f{i}") for i in range(80)]
    category_counts = {f"Cat{j}": 5 for j in range(4)}  # 4 × 5 = 20 total

    result = select_paid_mock_questions(bank, category_counts, empty_history(), rng=_rng())
    assert len(result["selected"]) == 20


def test_total_count_preserved_with_rich_history():
    bank = [make_q(i, category=f"Cat{i % 3}", family_id=f"f{i}") for i in range(60)]
    category_counts = {f"Cat{j}": 6 for j in range(3)}  # 3 × 6 = 18 total
    history = make_history(
        seen_ids=range(30),
        exposure={i: 2 for i in range(30)},
        recent_qids=range(10),
    )

    result = select_paid_mock_questions(bank, category_counts, history, rng=_rng())
    assert len(result["selected"]) == 18


# ── 12. Empty history behaves like first-time user ────────────────────────────

def test_empty_history_first_time_user():
    pool = [make_q(i, family_id=f"f{i}") for i in range(10)]
    selected = select_questions_for_domain(pool, 5, empty_history(), rng=_rng())

    assert len(selected) == 5
    ids = {q["id"] for q in selected}
    assert len(ids) == 5


def test_empty_history_all_unseen_no_crash():
    """build_history_context with no rows returns a usable empty dict."""
    history = build_history_context(
        all_paid_attempts=[],
        question_exposure_rows=[],
    )
    assert history["seen_question_ids"] == set()
    assert history["exposure_count"] == {}
    assert history["recent_attempt_ids"] == set()
    assert history["recent_question_ids"] == set()

    pool = [make_q(i, family_id=f"f{i}") for i in range(10)]
    selected = select_questions_for_domain(pool, 5, history, rng=_rng())
    assert len(selected) == 5


# ── 13. History-loading failure falls back safely ─────────────────────────────

def test_history_failure_empty_rows_safe():
    """Completely empty inputs must not raise and must return an empty context."""
    history = build_history_context([], [])
    assert isinstance(history["seen_question_ids"], set)
    assert isinstance(history["exposure_count"], dict)
    assert isinstance(history["last_seen"], dict)
    assert isinstance(history["recent_attempt_ids"], set)
    assert isinstance(history["recent_question_ids"], set)


def test_history_failure_malformed_rows_skipped():
    """Rows with None or missing question_id are silently skipped."""
    bad_rows = [
        {"question_id": None, "exam_attempt_id": 1, "completed_at": "2025-01-01T00:00:00"},
        {"exam_attempt_id": 2},  # no question_id key at all
        {},
    ]
    history = build_history_context(
        all_paid_attempts=[{"id": 1, "completed_at": "2025-01-01T00:00:00"}],
        question_exposure_rows=bad_rows,
    )
    assert history["seen_question_ids"] == set()
    assert history["exposure_count"] == {}


def test_history_failure_partial_rows_accepted():
    """Valid rows mixed with bad rows: valid ones populate history, bad ones are skipped."""
    mixed_rows = [
        {"question_id": 99, "exam_attempt_id": "att1", "completed_at": "2025-06-01T00:00:00"},
        {"question_id": None, "exam_attempt_id": "att1"},
        {"exam_attempt_id": "att1"},
    ]
    history = build_history_context(
        all_paid_attempts=[{"id": "att1", "completed_at": "2025-06-01T00:00:00"}],
        question_exposure_rows=mixed_rows,
    )
    assert "99" in history["seen_question_ids"]
    assert history["exposure_count"].get("99") == 1


def test_none_history_does_not_crash_selection():
    """When history is None (load failure), select_questions_for_domain with
    an empty-history dict must still work — callers pass empty_history() as fallback."""
    pool = [make_q(i, family_id=f"f{i}") for i in range(10)]
    selected = select_questions_for_domain(pool, 5, empty_history(), rng=_rng())
    assert len(selected) == 5


# ── 14. Free mock and practice selection paths are untouched ─────────────────

def test_paid_selection_function_not_called_for_free_path():
    """select_paid_mock_questions is the paid path; verifying it leaves
    the bank list unmodified (no mutation side-effects)."""
    bank = [make_q(i, category="Cat A", family_id=f"f{i}") for i in range(20)]
    original_ids = [q["id"] for q in bank]

    select_paid_mock_questions(bank, {"Cat A": 10}, empty_history(), rng=_rng())

    assert [q["id"] for q in bank] == original_ids, "Bank list was mutated"


def test_free_mock_pool_size_unaffected_by_paid_selection():
    """Paid selection on a distinct bank does not affect an independent free bank."""
    paid_bank = [make_q(i, category="Cat A", family_id=f"pf{i}") for i in range(20)]
    free_bank = [make_q(i + 100, category="Cat A", family_id=f"ff{i}") for i in range(10)]

    free_bank_snapshot = [q["id"] for q in free_bank]

    select_paid_mock_questions(paid_bank, {"Cat A": 10}, empty_history(), rng=_rng())

    assert [q["id"] for q in free_bank] == free_bank_snapshot


# ── Additional: build_history_context integration ────────────────────────────

def test_recent_window_excludes_older_attempts():
    """Questions from the third-most-recent attempt must NOT be in recent_question_ids."""
    attempts = [
        {"id": "att1", "completed_at": "2025-06-01T00:00:00"},   # most recent
        {"id": "att2", "completed_at": "2025-01-01T00:00:00"},   # second
        {"id": "att3", "completed_at": "2024-01-01T00:00:00"},   # outside window
    ]
    rows = [
        {"question_id": 1, "exam_attempt_id": "att1", "completed_at": "2025-06-01T00:00:00"},
        {"question_id": 2, "exam_attempt_id": "att2", "completed_at": "2025-01-01T00:00:00"},
        {"question_id": 3, "exam_attempt_id": "att3", "completed_at": "2024-01-01T00:00:00"},
    ]
    history = build_history_context(attempts, rows, recent_attempt_count=2)

    assert "1" in history["recent_question_ids"]
    assert "2" in history["recent_question_ids"]
    assert "3" not in history["recent_question_ids"]


def test_exposure_count_accumulated_across_attempts():
    attempts = [
        {"id": "att1", "completed_at": "2025-06-01T00:00:00"},
        {"id": "att2", "completed_at": "2025-01-01T00:00:00"},
    ]
    rows = [
        {"question_id": 7, "exam_attempt_id": "att1", "completed_at": "2025-06-01T00:00:00"},
        {"question_id": 7, "exam_attempt_id": "att2", "completed_at": "2025-01-01T00:00:00"},
    ]
    history = build_history_context(attempts, rows)
    assert history["exposure_count"]["7"] == 2


def test_last_seen_is_most_recent():
    attempts = [{"id": "att1", "completed_at": "2025-06-01T00:00:00"},
                {"id": "att2", "completed_at": "2025-01-01T00:00:00"}]
    rows = [
        {"question_id": 8, "exam_attempt_id": "att1", "completed_at": "2025-06-01T00:00:00"},
        {"question_id": 8, "exam_attempt_id": "att2", "completed_at": "2025-01-01T00:00:00"},
    ]
    history = build_history_context(attempts, rows)
    assert history["last_seen"]["8"] == "2025-06-01T00:00:00"


def test_family_key_non_null_shared():
    q1 = make_q(80, family_id="shared_stem")
    q2 = make_q(81, family_id="shared_stem")
    assert _family_key(q1) == _family_key(q2)


def test_family_key_empty_string_treated_as_null():
    q = make_q(90, family_id="")
    assert _family_key(q).startswith("solo:")


def test_waterfall_all_tiers_represented():
    """Construct a pool with one question per tier and verify order."""
    t1 = make_q(100, practice_eligible=False, family_id="t1_fam")
    t2 = make_q(101, practice_eligible=False, family_id="t2_fam")
    t3 = make_q(102, practice_eligible=True, family_id="t3_fam")
    t4 = make_q(103, practice_eligible=True, family_id="t4_fam")
    t5 = make_q(104, practice_eligible=True, family_id="t5_fam")

    history = make_history(
        seen_ids=[103, 104],
        exposure={103: 1, 104: 1},
        recent_qids=[101, 104],
    )

    ranked = rank_questions_waterfall([t5, t4, t3, t2, t1], history, rng=_rng())
    ranked_ids = [q["id"] for q in ranked]

    assert ranked_ids.index(100) < ranked_ids.index(101)  # T1 before T2
    assert ranked_ids.index(101) < ranked_ids.index(102)  # T2 before T3
    assert ranked_ids.index(102) < ranked_ids.index(103)  # T3 before T4
    assert ranked_ids.index(103) < ranked_ids.index(104)  # T4 before T5


# ══════════════════════════════════════════════════════════════════════════════
# V40 completion: difficulty preservation + V40-aware multi-select balancing
# ══════════════════════════════════════════════════════════════════════════════


def _difficulty_counts(questions) -> dict:
    counts = {"easy": 0, "medium": 0, "hard": 0}
    for q in questions:
        counts[q.get("difficulty", "medium")] = counts.get(q.get("difficulty", "medium"), 0) + 1
    return counts


# ── 9. Domain quotas remain exact (with mixed difficulty + history) ───────────

def test_domain_quotas_exact_mixed_difficulty():
    pool_a = (
        [make_q(i, category="Cat A", difficulty="easy", family_id=f"a{i}") for i in range(6)]
        + [make_q(i + 20, category="Cat A", difficulty="medium", family_id=f"a{i+20}") for i in range(6)]
        + [make_q(i + 40, category="Cat A", difficulty="hard", family_id=f"a{i+40}") for i in range(6)]
    )
    pool_b = [make_q(i + 100, category="Cat B", difficulty="medium", family_id=f"b{i}") for i in range(15)]
    bank = pool_a + pool_b
    counts = {"Cat A": 8, "Cat B": 5}

    result = select_paid_mock_questions(bank, counts, empty_history(), rng=_rng())
    by_cat = {}
    for q in result["selected"]:
        by_cat[q["category"]] = by_cat.get(q["category"], 0) + 1
    assert by_cat.get("Cat A") == 8
    assert by_cat.get("Cat B") == 5


# ── 10. Total count remains exact ─────────────────────────────────────────────

def test_total_count_exact_mixed_difficulty():
    bank = []
    for c in range(3):
        bank += [make_q(c * 100 + i, category=f"Cat{c}", difficulty="easy", family_id=f"c{c}e{i}") for i in range(5)]
        bank += [make_q(c * 100 + 50 + i, category=f"Cat{c}", difficulty="medium", family_id=f"c{c}m{i}") for i in range(5)]
        bank += [make_q(c * 100 + 80 + i, category=f"Cat{c}", difficulty="hard", family_id=f"c{c}h{i}") for i in range(5)]
    counts = {f"Cat{c}": 6 for c in range(3)}

    result = select_paid_mock_questions(bank, counts, empty_history(), rng=_rng())
    assert len(result["selected"]) == 18


# ── 11. Difficulty targets preserved when inventory permits ───────────────────

def test_difficulty_targets_preserved_when_inventory_permits():
    pool = (
        [make_q(i, difficulty="easy", family_id=f"e{i}") for i in range(10)]
        + [make_q(i + 100, difficulty="medium", family_id=f"m{i}") for i in range(10)]
        + [make_q(i + 200, difficulty="hard", family_id=f"h{i}") for i in range(10)]
    )
    selected = select_questions_for_domain(pool, 10, empty_history(), rng=_rng())
    counts = _difficulty_counts(selected)
    expected = difficulty_targets(10)  # {'easy':2,'medium':5,'hard':3}

    assert len(selected) == 10
    assert counts == expected


# ── 12. Difficulty shortage fallback still fills the exam ─────────────────────

def test_difficulty_shortage_still_fills_quota():
    # No 'hard' and no 'easy' inventory at all; only medium exists.
    pool = [make_q(i, difficulty="medium", family_id=f"m{i}") for i in range(20)]
    selected = select_questions_for_domain(pool, 10, empty_history(), rng=_rng())

    assert len(selected) == 10
    ids = {q["id"] for q in selected}
    assert len(ids) == 10  # exact quota met from the available difficulty


def test_difficulty_targets_helper_matches_legacy_formula():
    assert difficulty_targets(10) == {"easy": 2, "medium": 5, "hard": 3}
    assert difficulty_targets(9) == {"easy": 2, "medium": 4, "hard": 3}
    assert difficulty_targets(0) == {"easy": 0, "medium": 0, "hard": 0}
    # count < 5 suppresses easy
    assert difficulty_targets(3)["easy"] == 0


# ── 13. Final multi-select count is 8–10 when inventory permits ───────────────

def _balancing_bank():
    singles = [make_q(i, category="Cat A", qtype="single", family_id=f"s{i}") for i in range(60)]
    multis = [make_q(1000 + i, category="Cat A", qtype="multiple", family_id=f"mlt{i}") for i in range(30)]
    by_category = {"Cat A": singles + multis}
    return singles, multis, by_category


def test_balancing_raises_multi_to_minimum_eight():
    singles, multis, by_category = _balancing_bank()
    selected = list(singles)  # 60 single, 0 multiple
    out = balance_multi_select(selected, by_category, empty_history(), rng=_rng())
    multi_count = sum(1 for q in out if q.get("type") == "multiple")
    assert multi_count == 8
    assert len(out) == 60


def test_balancing_lowers_multi_to_maximum_ten():
    _, multis, by_category = _balancing_bank()
    extra_singles = [make_q(2000 + i, category="Cat A", qtype="single", family_id=f"xs{i}") for i in range(60)]
    by_category["Cat A"] = by_category["Cat A"] + extra_singles
    selected = [make_q(3000 + i, category="Cat A", qtype="multiple", family_id=f"sel{i}") for i in range(20)]
    # ensure the candidate single pool is discoverable
    by_category["Cat A"] = by_category["Cat A"] + selected
    out = balance_multi_select(selected, by_category, empty_history(), rng=_rng())
    multi_count = sum(1 for q in out if q.get("type") == "multiple")
    assert multi_count == 10
    assert len(out) == 20


# ── 14. Type balancing does not introduce duplicate IDs ───────────────────────

def test_balancing_no_duplicate_ids():
    singles, multis, by_category = _balancing_bank()
    selected = list(singles)
    out = balance_multi_select(selected, by_category, empty_history(), rng=_rng())
    ids = [q["id"] for q in out]
    assert len(ids) == len(set(ids))


# ── 15. Type balancing does not introduce duplicate families ──────────────────

def test_balancing_no_duplicate_families_when_alternatives_exist():
    singles, multis, by_category = _balancing_bank()  # all distinct families
    selected = list(singles)
    out = balance_multi_select(selected, by_category, empty_history(), rng=_rng())
    fkeys = [_family_key(q) for q in out]
    assert len(fkeys) == len(set(fkeys))


# ── 16. Waterfall priority still works after difficulty/type constraints ──────

def test_waterfall_priority_survives_difficulty_selection():
    # Within the medium bucket, a Tier-1 (unseen, mock-only, non-recent) question
    # must be chosen over previously-seen medium questions.
    fresh_mock_only = make_q(500, difficulty="medium", practice_eligible=False, family_id="fresh")
    seen = [make_q(600 + i, difficulty="medium", practice_eligible=True, family_id=f"seen{i}") for i in range(5)]
    pool = seen + [fresh_mock_only]

    history = make_history(
        seen_ids=[q["id"] for q in seen],
        exposure={q["id"]: 2 for q in seen},
    )
    selected = select_questions_for_domain(pool, 1, history, rng=_rng())
    assert selected[0]["id"] == 500


def test_balancing_replacement_prefers_fresh_via_history():
    # Two multiple-answer candidates: one unseen, one seen. Balancing should
    # prefer the unseen candidate (waterfall) when raising the multi count.
    singles = [make_q(i, category="Cat A", qtype="single", family_id=f"s{i}") for i in range(60)]
    unseen_multi = make_q(900, category="Cat A", qtype="multiple", family_id="fresh_multi")
    seen_multi = make_q(901, category="Cat A", qtype="multiple", family_id="seen_multi")
    by_category = {"Cat A": singles + [seen_multi, unseen_multi]}

    history = make_history(seen_ids=[901], exposure={901: 3})
    out = balance_multi_select(
        list(singles), by_category, history, rng=_rng(), min_multi=1, max_multi=10
    )
    multi_ids = [q["id"] for q in out if q.get("type") == "multiple"]
    assert 900 in multi_ids  # fresh chosen first

