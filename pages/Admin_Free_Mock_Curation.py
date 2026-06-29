"""Admin free-mock curation — assemble and publish versioned 15-question sets."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_supabase_admin_client,
    render_app_chrome,
    require_admin,
)
from utils.free_mock_curation import (
    ADM_EXAM_NAME,
    BA_EXAM_NAME,
    DEFAULT_FREE_MOCK_LANGUAGE,
    FREE_MOCK_CURATION_SETUP_MESSAGE,
    FREE_MOCK_MIN_MULTI_SELECT,
    FREE_MOCK_SLOT_COUNT,
    FreeMockCurationError,
    FreeMockCurationSetupError,
    compare_domain_counts,
    count_multi_select,
    create_draft,
    format_failures,
    get_curation_state,
    normalize_draft_items,
    publish_draft,
    replace_draft_items,
    validate_draft,
    validate_draft_items_local,
)
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

st.set_page_config(page_title="Admin Free Mock Curation", layout="wide")
render_app_chrome()
require_admin()
enforce_session_timeout()
show_session_expired_notice()

EXAM_OPTIONS = [
    (ADM_EXAM_NAME, "ADM-201 — Salesforce Certified Platform Administrator"),
    (BA_EXAM_NAME, "BA-201 — Salesforce Certified Business Analyst"),
]
EXAM_DISPLAY = {name: label for name, label in EXAM_OPTIONS}


def _client():
    return get_supabase_admin_client()


def _draft_session_key(exam_name: str) -> str:
    return f"free_mock_draft_items_{exam_name}"


def _draft_set_id_key(exam_name: str) -> str:
    return f"free_mock_draft_set_id_{exam_name}"


@st.cache_data(ttl=60)
def load_eligible_questions(exam_name: str, language_code: str) -> List[dict]:
    result = (
        _client()
        .table("questions")
        .select(
            "id, exam_name, language_code, category, difficulty, question_text, "
            "question_type, select_count, explanation, is_active, is_exam_eligible, "
            "mock_eligible, quality_status"
        )
        .eq("exam_name", exam_name)
        .eq("language_code", language_code)
        .eq("is_active", True)
        .eq("is_exam_eligible", True)
        .eq("mock_eligible", True)
        .eq("quality_status", "approved")
        .order("id")
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=60)
def load_answer_options_for_exam(exam_name: str, language_code: str) -> Dict[int, List[dict]]:
    questions = load_eligible_questions(exam_name, language_code)
    if not questions:
        return {}
    qids = [q["id"] for q in questions]
    options_by_question: Dict[int, List[dict]] = {qid: [] for qid in qids}
    chunk_size = 100
    for i in range(0, len(qids), chunk_size):
        chunk = qids[i : i + chunk_size]
        result = (
            _client()
            .table("answer_options")
            .select("id, question_id, option_label, option_text, is_correct, display_order")
            .in_("question_id", chunk)
            .order("display_order")
            .execute()
        )
        for row in result.data or []:
            options_by_question.setdefault(row["question_id"], []).append(row)
    return options_by_question


def _ensure_draft_loaded(exam_name: str, language_code: str, actor_email: str) -> str:
    set_id = st.session_state.get(_draft_set_id_key(exam_name))
    if set_id and st.session_state.get(_draft_session_key(exam_name)) is not None:
        return str(set_id)

    draft_row = create_draft(
        _client(),
        exam_name=exam_name,
        language_code=language_code,
        actor_email=actor_email,
    )
    set_id = str(draft_row.get("free_mock_set_id"))

    state = get_curation_state(_client(), exam_name=exam_name, language_code=language_code)
    draft = state.get("draft") or {}
    items = normalize_draft_items(draft.get("items") or [])

    st.session_state[_draft_set_id_key(exam_name)] = set_id
    st.session_state[_draft_session_key(exam_name)] = items
    return set_id


def _save_draft_to_db(exam_name: str, actor_email: str) -> None:
    items = st.session_state.get(_draft_session_key(exam_name)) or []
    set_id = st.session_state.get(_draft_set_id_key(exam_name))
    if not set_id:
        raise FreeMockCurationError("draft set id missing")
    replace_draft_items(
        _client(),
        set_id=str(set_id),
        items=items,
        actor_email=actor_email,
    )


def _add_to_next_slot(exam_name: str, question_id: int, domain_name: str) -> None:
    items = list(st.session_state.get(_draft_session_key(exam_name)) or [])
    used_slots = {int(i["slot_order"]) for i in items}
    used_questions = {int(i["question_id"]) for i in items}
    if question_id in used_questions:
        st.warning(f"Question {question_id} is already in the draft.")
        return
    if len(items) >= FREE_MOCK_SLOT_COUNT:
        st.warning(f"All {FREE_MOCK_SLOT_COUNT} slots are filled.")
        return
    next_slot = min(set(range(1, FREE_MOCK_SLOT_COUNT + 1)) - used_slots)
    items.append(
        {
            "slot_order": next_slot,
            "question_id": question_id,
            "domain_name": domain_name,
        }
    )
    items.sort(key=lambda row: row["slot_order"])
    st.session_state[_draft_session_key(exam_name)] = items


def _remove_slot(exam_name: str, slot_order: int) -> None:
    items = [
        row
        for row in (st.session_state.get(_draft_session_key(exam_name)) or [])
        if int(row["slot_order"]) != slot_order
    ]
    st.session_state[_draft_session_key(exam_name)] = items


def _move_slot(exam_name: str, slot_order: int, direction: int) -> None:
    items = normalize_draft_items(st.session_state.get(_draft_session_key(exam_name)) or [])
    index = next((i for i, row in enumerate(items) if row["slot_order"] == slot_order), None)
    if index is None:
        return
    swap_index = index + direction
    if swap_index < 0 or swap_index >= len(items):
        return
    items[index], items[swap_index] = items[swap_index], items[index]
    for new_order, row in enumerate(items, start=1):
        row["slot_order"] = new_order
    st.session_state[_draft_session_key(exam_name)] = items


st.title("Admin Free Mock Curation")
st.caption(
    f"Phase 1 — assemble and publish immutable 15-question free-mock sets. "
    f"Learner runtime still uses legacy flags until a later phase. App {APP_VERSION}"
)

reviewer_email = get_current_user_email()
if not reviewer_email:
    st.error("Signed-in admin email is required.")
    st.stop()

selected_exam = st.selectbox(
    "Certification",
    [name for name, _ in EXAM_OPTIONS],
    format_func=lambda name: EXAM_DISPLAY.get(name, name),
    key="free_mock_curation_exam",
)
language_code = DEFAULT_FREE_MOCK_LANGUAGE
st.caption(f"Language: {language_code} (Phase 1 English only)")

try:
    draft_set_id = _ensure_draft_loaded(selected_exam, language_code, reviewer_email)
except FreeMockCurationSetupError:
    st.warning(FREE_MOCK_CURATION_SETUP_MESSAGE)
    st.info(
        "This page is safe to open before the migration is applied. "
        "Question browsing below still works; draft save, validate, and publish "
        "require the migration."
    )
    draft_set_id = None
except FreeMockCurationError as exc:
    st.error(str(exc))
    st.stop()

questions = load_eligible_questions(selected_exam, language_code)
options_by_question = load_answer_options_for_exam(selected_exam, language_code)
questions_by_id = {int(q["id"]): q for q in questions}

published = None
draft_items = st.session_state.get(_draft_session_key(selected_exam)) or []
if draft_set_id:
    try:
        curation_state = get_curation_state(
            _client(), exam_name=selected_exam, language_code=language_code
        )
    except FreeMockCurationSetupError:
        curation_state = {"draft": None, "published": None}
    except FreeMockCurationError as exc:
        st.error(f"Could not load curation state: {exc}")
        curation_state = {"draft": None, "published": None}
    published = curation_state.get("published")
    draft_items = st.session_state.get(_draft_session_key(selected_exam)) or []
else:
    draft_items = []

st.subheader("Published set")
if published:
    pub_items = normalize_draft_items(published.get("items") or [])
    st.success(
        f"Version {published.get('version_number')} published "
        f"at {published.get('published_at') or '—'} by {published.get('published_by') or '—'}"
    )
    st.caption(f"Set ID: {published.get('set_id')}")
    if pub_items:
        st.write(
            "Question IDs (slot order): "
            + ", ".join(f"{row['slot_order']}→{row['question_id']}" for row in pub_items)
        )
else:
    st.info("No published free-mock set yet for this certification.")

st.divider()
if not draft_set_id:
    st.subheader("Draft slots")
    st.info("Draft curation is unavailable until the free-mock migration is applied.")
else:
    st.subheader("Draft slots")
    st.caption(f"Draft set ID: {draft_set_id}")

if draft_set_id:
    domain_rows = compare_domain_counts(draft_items, questions_by_id, selected_exam)
    multi_count = count_multi_select(draft_items, questions_by_id)
    valid_local, local_failures = validate_draft_items_local(
        draft_items,
        questions_by_id,
        options_by_question,
        exam_name=selected_exam,
        language_code=language_code,
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Filled slots", f"{len(draft_items)} / {FREE_MOCK_SLOT_COUNT}")
    c2.metric("Multi-select", f"{multi_count} (min {FREE_MOCK_MIN_MULTI_SELECT})")
    c3.metric("Validation", "Ready" if valid_local else f"{len(local_failures)} issue(s)")

    st.markdown("**Domain blueprint**")
    st.dataframe(pd.DataFrame(domain_rows), use_container_width=True, hide_index=True)

    if local_failures:
        st.markdown("**Validation failures**")
        for line in format_failures(local_failures):
            st.error(line)

    slot_rows = []
    for slot in range(1, FREE_MOCK_SLOT_COUNT + 1):
        match = next((row for row in draft_items if int(row["slot_order"]) == slot), None)
        if match:
            q = questions_by_id.get(int(match["question_id"]), {})
            slot_rows.append(
                {
                    "Slot": slot,
                    "Question ID": match["question_id"],
                    "Domain": q.get("category") or match.get("domain_name"),
                    "Type": q.get("question_type"),
                    "select_count": q.get("select_count"),
                }
            )
        else:
            slot_rows.append({"Slot": slot, "Question ID": "—", "Domain": "—", "Type": "—", "select_count": "—"})
    st.dataframe(pd.DataFrame(slot_rows), use_container_width=True, hide_index=True)
else:
    valid_local = False
    local_failures = []

if draft_set_id:
    action_cols = st.columns(4)
    with action_cols[0]:
        if st.button("Save draft to database", type="primary"):
            try:
                _save_draft_to_db(selected_exam, reviewer_email)
                st.success("Draft saved.")
                st.cache_data.clear()
            except FreeMockCurationError as exc:
                st.error(str(exc))
    with action_cols[1]:
        if st.button("Validate draft (server)"):
            try:
                _save_draft_to_db(selected_exam, reviewer_email)
                result = validate_draft(_client(), set_id=str(draft_set_id))
                if result["valid"]:
                    st.success("Server validation passed.")
                else:
                    for line in format_failures(result["failures"]):
                        st.error(line)
            except FreeMockCurationError as exc:
                st.error(str(exc))
    with action_cols[2]:
        confirm_publish = st.checkbox(
            "I confirm this 15-question set is ready to publish",
            key="free_mock_publish_confirm",
        )
    with action_cols[3]:
        publish_reason = st.text_input("Publish reason", key="free_mock_publish_reason")

    if st.button("Publish draft", disabled=not confirm_publish):
        if not publish_reason.strip():
            st.error("Publish reason is required.")
        else:
            try:
                _save_draft_to_db(selected_exam, reviewer_email)
                result = publish_draft(
                    _client(),
                    set_id=str(draft_set_id),
                    actor_email=reviewer_email,
                    reason=publish_reason.strip(),
                )
                st.success(
                    f"Published version {result.get('version_number')} "
                    f"(retired prior set: {result.get('retired_set_id') or 'none'})"
                )
                st.session_state.pop(_draft_session_key(selected_exam), None)
                st.session_state.pop(_draft_set_id_key(selected_exam), None)
                st.cache_data.clear()
                st.rerun()
            except FreeMockCurationError as exc:
                st.error(str(exc))

    for row in normalize_draft_items(draft_items):
        slot = int(row["slot_order"])
        cols = st.columns([1, 2, 1, 1, 1])
        with cols[0]:
            st.write(f"Slot {slot}")
        with cols[1]:
            st.write(f"Q{row['question_id']} — {row.get('domain_name', '')}")
        with cols[2]:
            if st.button("Up", key=f"slot_up_{selected_exam}_{slot}"):
                _move_slot(selected_exam, slot, -1)
                st.rerun()
        with cols[3]:
            if st.button("Down", key=f"slot_down_{selected_exam}_{slot}"):
                _move_slot(selected_exam, slot, 1)
                st.rerun()
        with cols[4]:
            if st.button("Remove", key=f"slot_remove_{selected_exam}_{slot}"):
                _remove_slot(selected_exam, slot)
                st.rerun()

st.divider()
st.subheader("Eligible question candidates")
st.caption("Read-only preview. Question content is not editable on this page.")

filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)
domains = sorted({q.get("category") for q in questions if q.get("category")})
with filter_col1:
    domain_filter = st.selectbox("Domain", ["All"] + domains, key="free_mock_filter_domain")
with filter_col2:
    qid_filter = st.text_input("Question ID", key="free_mock_filter_qid")
with filter_col3:
    type_filter = st.selectbox("Question type", ["All", "single", "multiple"], key="free_mock_filter_type")
with filter_col4:
    stem_filter = st.text_input("Stem search", key="free_mock_filter_stem")

filtered: List[dict] = []
for q in questions:
    if domain_filter != "All" and q.get("category") != domain_filter:
        continue
    if qid_filter.strip() and str(q.get("id")) != qid_filter.strip():
        continue
    if type_filter != "All" and q.get("question_type") != type_filter:
        continue
    if stem_filter and stem_filter.lower() not in (q.get("question_text") or "").lower():
        continue
    opts = options_by_question.get(q["id"], [])
    filtered.append(
        {
            "id": q.get("id"),
            "domain": q.get("category"),
            "type": q.get("question_type"),
            "select_count": q.get("select_count"),
            "explanation_ok": bool(str(q.get("explanation") or "").strip()),
            "quality_status": q.get("quality_status"),
            "active": q.get("is_active"),
            "exam_eligible": q.get("is_exam_eligible"),
            "mock_eligible": q.get("mock_eligible"),
            "preview": (q.get("question_text") or "")[:120],
            "_question": q,
            "_options": opts,
        }
    )

st.write(f"Showing **{len(filtered)}** of **{len(questions)}** eligible questions.")
if filtered:
    table = pd.DataFrame(
        [
            {k: v for k, v in row.items() if not k.startswith("_")}
            for row in filtered
        ]
    )
    st.dataframe(table, use_container_width=True, hide_index=True)

if not filtered:
    st.stop()

labels = [
    f"ID {row['id']} | {row['domain']} | {row['type']} | {row['preview']}"
    for row in filtered
]
selected_index = st.selectbox(
    "Select a candidate to preview / add",
    range(len(filtered)),
    format_func=lambda i: labels[i],
    key="free_mock_candidate_select",
)
candidate = filtered[selected_index]
q = candidate["_question"]
opts = candidate["_options"]

st.markdown("**Question preview**")
st.caption(f"Question ID: {q.get('id')} | Domain: {q.get('category')} | Type: {q.get('question_type')}")
st.write(q.get("question_text") or "")

option_rows = [
    {
        "Label": o.get("option_label"),
        "Text": o.get("option_text"),
        "Correct": o.get("is_correct"),
    }
    for o in opts
]
st.dataframe(pd.DataFrame(option_rows), use_container_width=True, hide_index=True)
correct = [o.get("option_text") for o in opts if o.get("is_correct")]
st.write("Correct answer(s): " + (", ".join(correct) if correct else "—"))
st.markdown("**Explanation**")
st.info(q.get("explanation") or "(empty)")

if st.button("Add to draft (next open slot)", key="free_mock_add_candidate", disabled=not draft_set_id):
    _add_to_next_slot(selected_exam, int(q["id"]), str(q.get("category") or ""))
    st.rerun()
if not draft_set_id:
    st.caption("Apply the free-mock migration to enable draft slot editing.")
