import logging

import streamlit as st

from utils.access_control import (
    get_current_user_email,
    get_supabase_admin_client,
    is_admin_unlocked,
    is_admin_user,
    render_app_chrome,
    require_admin,
)
from utils.audit_review import (
    ALLOWED_DECISIONS,
    AuditReviewAccessError,
    AuditReviewError,
    DECISION_PERSISTENCE_ERROR_MESSAGE,
    build_evidence_contract_view,
    escape_review_text,
    format_finding_label,
    format_run_label,
    get_finding_review_detail,
    list_audit_findings,
    list_audit_runs,
    load_immutable_question_version,
    record_finding_decision,
)
from utils.publication_gate import (
    PublicationGateError,
    approve_question_version,
    format_blocking_findings_summary,
    format_publication_status_message,
    get_publication_status,
    publish_question_version,
)
from utils.session_timeout import enforce_session_timeout, show_session_expired_notice
from utils.version import APP_VERSION

logger = logging.getLogger(__name__)

st.set_page_config(page_title="Admin Audit Review", layout="wide")
render_app_chrome()
require_admin()
enforce_session_timeout()
show_session_expired_notice()

st.title("Admin Audit Review")
st.caption(f"Inspect audit runs, review version-anchored findings, and record decisions. App {APP_VERSION}")


def _client():
    return get_supabase_admin_client()


def _short_id(value) -> str:
    text = str(value or "")
    return text[:8] + "…" if len(text) > 8 else text


@st.cache_data(ttl=30)
def _cached_runs(
    run_status: str,
    audit_type: str,
    certification_code: str,
    blocking_only: bool,
):
    return list_audit_runs(
        _client(),
        run_status=None if run_status == "All" else run_status,
        audit_type=None if audit_type == "All" else audit_type,
        certification_code=certification_code or None,
        blocking_only=blocking_only,
    )


def _render_run_filters() -> tuple:
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        run_status = st.selectbox(
            "Run status",
            ["All", "completed", "running", "pending", "failed", "cancelled"],
            key="audit_review_run_status",
        )
    with col2:
        audit_type = st.selectbox(
            "Audit source",
            ["All", "deterministic", "llm", "hybrid", "human"],
            key="audit_review_audit_type",
        )
    with col3:
        certification_code = st.text_input(
            "Certification code",
            key="audit_review_cert_code",
            placeholder="e.g. ADM-201",
        ).strip()
    with col4:
        blocking_only = st.checkbox(
            "Blocking findings only",
            key="audit_review_blocking_only",
        )
    return run_status, audit_type, certification_code, blocking_only


def _render_run_list(runs: list) -> str | None:
    st.subheader("Audit runs")
    if not runs:
        st.info("No audit runs match the current filters.")
        return None

    labels = [format_run_label(run) for run in runs]
    selected_label = st.selectbox(
        "Select audit run",
        labels,
        key="audit_review_selected_run_label",
    )
    selected_index = labels.index(selected_label)
    selected_run = runs[selected_index]
    run_id = str(selected_run.get("audit_run_id"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Run ID", _short_id(run_id))
    c2.metric("Findings", selected_run.get("finding_count", 0))
    c3.metric("Blocking", selected_run.get("blocking_finding_count", 0))
    c4.metric("High/Critical", selected_run.get("high_severity_count", 0))

    st.caption(
        f"Type: {escape_review_text(selected_run.get('audit_type'))} | "
        f"Status: {escape_review_text(selected_run.get('run_status'))} | "
        f"Cert: {escape_review_text(selected_run.get('certification_code') or '—')} | "
        f"Version: {escape_review_text(selected_run.get('target_question_version_id') or '—')} | "
        f"Started: {escape_review_text(selected_run.get('started_at') or '—')} | "
        f"Completed: {escape_review_text(selected_run.get('completed_at') or '—')}"
    )
    return run_id


def _render_finding_list(run_id: str) -> str | None:
    st.subheader("Findings")
    try:
        findings = list_audit_findings(_client(), audit_run_id=run_id)
    except AuditReviewError as exc:
        st.error(f"Could not load findings: {escape_review_text(exc)}")
        return None

    if not findings:
        st.info("This audit run has no findings.")
        return None

    labels = [format_finding_label(item) for item in findings]
    selected_label = st.selectbox(
        "Select finding",
        labels,
        key=f"audit_review_finding_{run_id}",
    )
    finding = findings[labels.index(selected_label)]
    finding_id = str(finding.get("finding_id"))

    st.caption(
        f"Status: {escape_review_text(finding.get('finding_status'))} | "
        f"Source: {escape_review_text(finding.get('audit_source'))} | "
        f"Question ID: {escape_review_text(finding.get('question_id') or '—')} | "
        f"Version ID: {escape_review_text(finding.get('question_version_id') or '—')} | "
        f"Version #: {escape_review_text(finding.get('question_version_number') or '—')}"
    )
    return finding_id


def _render_finding_detail(finding_id: str) -> None:
    st.subheader("Finding detail")
    try:
        detail = get_finding_review_detail(_client(), finding_id=finding_id)
    except AuditReviewError as exc:
        st.error(f"Could not load finding detail: {escape_review_text(exc)}")
        return

    contract = build_evidence_contract_view(detail)
    version_snapshot = load_immutable_question_version(detail)

    st.markdown(f"**Summary:** {escape_review_text(contract.get('summary') or detail.get('title'))}")
    st.text_area(
        "Detailed rationale",
        value=str(contract.get("detailed_rationale") or detail.get("description") or ""),
        height=120,
        disabled=True,
        key=f"audit_review_rationale_{finding_id}",
    )

    c1, c2, c3 = st.columns(3)
    c1.write(f"Evidence contract: `{escape_review_text(contract.get('contract_version'))}`")
    c2.write(f"Audit source: `{escape_review_text(contract.get('audit_source'))}`")
    c3.write(f"Legacy: `{escape_review_text(contract.get('legacy', False))}`")

    st.text(f"Field path: {contract.get('field_path') or detail.get('field_path') or '—'}")
    st.text(f"Expected rule/value: {contract.get('expected_rule_or_value') or '—'}")
    st.text(f"Observed evidence: {contract.get('observed_evidence') or '—'}")
    st.text(f"Suggested correction: {contract.get('suggested_correction') or '—'}")
    if contract.get("confidence") is not None:
        st.text(f"Confidence: {contract.get('confidence')}")
    st.text(f"Fingerprint: {contract.get('fingerprint') or '—'}")

    if contract.get("deterministic_rule"):
        st.markdown("**Deterministic rule metadata**")
        st.json(contract["deterministic_rule"])
    if contract.get("model_metadata"):
        st.markdown("**Model / provider metadata**")
        st.json(contract["model_metadata"])
    if contract.get("prompt_version") or contract.get("ruleset_version"):
        st.caption(
            f"Prompt: {escape_review_text(contract.get('prompt_version') or '—')} | "
            f"Ruleset: {escape_review_text(contract.get('ruleset_version') or '—')}"
        )

    st.markdown("**Immutable question version**")
    if version_snapshot:
        st.text(
            f"Question ID {version_snapshot.get('question_id')} | "
            f"Version {version_snapshot.get('version_number')} | "
            f"Version ID {version_snapshot.get('question_version_id')}"
        )
        st.text_area(
            "Question stem",
            value=str(version_snapshot.get("question_text") or ""),
            height=120,
            disabled=True,
            key=f"audit_review_stem_{finding_id}",
        )
        options = version_snapshot.get("options") or []
        if options:
            st.markdown("**Answer options (immutable version)**")
            for option in options:
                mark = "✓" if option.get("is_correct") else " "
                st.text(
                    f"[{mark}] {option.get('option_label')}: {option.get('option_text')}"
                )
        else:
            st.caption("No option rows stored for this immutable version.")
    else:
        st.warning(
            "Immutable question version snapshot is unavailable. "
            "The page will not substitute the current live question."
        )

    version_id = detail.get("target_question_version_id")
    if version_id:
        st.markdown("**Publication gate**")
        pub_status = None
        try:
            pub_status = get_publication_status(_client(), question_version_id=str(version_id))
            if pub_status.get("publishable"):
                st.success(format_publication_status_message(pub_status))
            else:
                st.error(format_publication_status_message(pub_status))
                summary = format_blocking_findings_summary(pub_status)
                if summary:
                    st.caption(f"Blocking findings: {escape_review_text(summary)}")
                st.caption(
                    "Accepted findings remain blocking. Rejected or resolved findings release the gate."
                )
        except PublicationGateError as exc:
            st.error(f"Could not load publication status: {escape_review_text(exc)}")

        with st.expander("Approve version (admin only)"):
            st.caption(
                "Approval only records an append-only approved event through the existing "
                "database RPC. It does not publish the version."
            )
            is_publishable = bool(pub_status and pub_status.get("publishable"))
            if not is_publishable:
                st.warning(
                    "Approval is disabled while open or accepted blocking findings remain "
                    "for this version. Reject or resolve the blocking findings above first."
                )
            approve_reason = st.text_input(
                "Approval reason",
                key=f"audit_review_approve_reason_{finding_id}",
                disabled=not is_publishable,
            )
            if st.button(
                "Approve version",
                key=f"audit_review_approve_{finding_id}",
                disabled=not is_publishable,
            ):
                reviewer_email = get_current_user_email()
                if not approve_reason.strip():
                    st.error("Approval reason is required.")
                else:
                    try:
                        approve_question_version(
                            _client(),
                            question_version_id=str(version_id),
                            actor_email=reviewer_email or "",
                            reason=approve_reason,
                        )
                        st.success("Version approved. Publication may now be attempted below.")
                        st.cache_data.clear()
                        st.rerun()
                    except PublicationGateError as exc:
                        st.error(escape_review_text(exc))

        with st.expander("Manual publish attempt (admin only)"):
            st.caption(
                "Publication is enforced in the database. This action does not bypass the audit gate."
            )
            publish_reason = st.text_input(
                "Publish reason",
                key=f"audit_review_publish_reason_{finding_id}",
            )
            if st.button("Attempt publish", key=f"audit_review_publish_{finding_id}"):
                reviewer_email = get_current_user_email()
                try:
                    publish_question_version(
                        _client(),
                        question_version_id=str(version_id),
                        actor_email=reviewer_email or "",
                        reason=publish_reason,
                    )
                    st.success("Version published successfully.")
                    st.cache_data.clear()
                    st.rerun()
                except PublicationGateError as exc:
                    st.error(escape_review_text(exc))

    references = contract.get("supporting_references") or detail.get("evidence") or []
    if references:
        st.markdown("**Supporting references**")
        for ref in references:
            st.text(
                f"{ref.get('evidence_role')}: chunk {ref.get('resource_chunk_id')} | "
                f"quote={ref.get('quote_text') or '—'}"
            )

    history = detail.get("decision_history") or []
    if history:
        st.markdown("**Decision history**")
        st.dataframe(history, use_container_width=True, hide_index=True)

    _render_decision_form(detail)


def _render_decision_form(detail: dict) -> None:
    finding_id = str(detail.get("finding_id"))
    current_status = str(detail.get("finding_status") or "open")
    reviewer_email = get_current_user_email()

    st.markdown("**Record decision**")
    st.caption(f"Current status: `{escape_review_text(current_status)}`")
    if not reviewer_email:
        st.error("Signed-in reviewer email is required.")
        return

    decision = st.selectbox(
        "Decision",
        sorted(ALLOWED_DECISIONS),
        key=f"audit_review_decision_{finding_id}",
    )
    note = st.text_area(
        "Reviewer note (required)",
        key=f"audit_review_note_{finding_id}",
        height=100,
    )

    if st.button("Submit decision", key=f"audit_review_submit_{finding_id}"):
        try:
            result = record_finding_decision(
                _client(),
                finding_id=finding_id,
                decision=decision,
                reviewer_email=reviewer_email,
                reviewer_note=note,
                is_admin_user=is_admin_user(reviewer_email),
                is_admin_unlocked=is_admin_unlocked(),
            )
        except AuditReviewAccessError as exc:
            st.error(escape_review_text(exc))
        except AuditReviewError as exc:
            if str(exc) == DECISION_PERSISTENCE_ERROR_MESSAGE:
                st.error(DECISION_PERSISTENCE_ERROR_MESSAGE)
            else:
                st.error(escape_review_text(exc))
        except Exception:
            logger.exception(
                "Unexpected error recording audit finding decision finding_id=%s",
                finding_id,
            )
            st.error(DECISION_PERSISTENCE_ERROR_MESSAGE)
        else:
            if result.get("idempotent"):
                st.info("Finding already has this status; no duplicate decision was recorded.")
            else:
                st.success(
                    f"Recorded {escape_review_text(result.get('new_status'))} "
                    f"from {escape_review_text(result.get('previous_status'))}."
                )
            st.cache_data.clear()
            st.rerun()


run_status, audit_type, certification_code, blocking_only = _render_run_filters()
try:
    runs = _cached_runs(run_status, audit_type, certification_code, blocking_only)
except AuditReviewError as exc:
    st.error(f"Could not load audit runs: {escape_review_text(exc)}")
    runs = []

selected_run_id = _render_run_list(runs)
if selected_run_id:
    selected_finding_id = _render_finding_list(selected_run_id)
    if selected_finding_id:
        _render_finding_detail(selected_finding_id)
