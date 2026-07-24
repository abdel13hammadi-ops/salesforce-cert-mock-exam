"""SIM-INT-02 / SIM-INT-02A: real local-Supabase BA-201 controller
integration test, plus the pure, subprocess-mocked safety tests that harden
it.

Two very different kinds of tests live in this one file:

1. `test_full_ba201_controller_flow_against_real_local_supabase` -- the ONE
   real integration test. It is individually marked
   `@pytest.mark.skipif(not is_local_supabase_integration_enabled(), ...)`
   and performs ZERO filesystem/subprocess/network/Docker action unless the
   operator explicitly opts in via
   ``CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION=1``.

2. Every other test in this file is a PURE, subprocess-mocked safety test
   (credential-safety, Docker preflight, deterministic startup, fail-closed
   cleanup, context-manager resilience). These run under an ordinary
   ``pytest`` invocation with NO opt-in variable, NO Docker daemon, and NO
   Supabase CLI -- ``subprocess.run`` is patched in every one of them. The
   opt-in skip marker is deliberately placed on the ONE real test only (not
   as a module-level ``pytestmark``) so these safety tests always run.

Never imports ``utils.access_control``. Every Supabase client used by the
real integration test is either the real client returned by
``disposable_ba201_lab()`` (constructed directly from this lab's OWN
freshly-started local stack), or the test-only ``CommitThenRaiseProxyClient``
wrapping that exact client -- and is always passed EXPLICITLY into every
``utils.scenario_learner_controller`` / ``utils.scenario_persistence`` call
via ``client=...``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import ba201_supabase_lab  # noqa: E402
from ba201_supabase_lab import (  # noqa: E402
    Ba201IntegrationLabError,
    Ba201Lab,
    CleanupReport,
    CommitThenRaiseProxyClient,
    LabPorts,
    check_docker_daemon_available,
    connect_to_lab_database,
    disposable_ba201_lab,
    fetch_scenario_row,
    fetch_scenario_version_row,
    get_last_cleanup_report,
    is_local_supabase_integration_enabled,
    publish_second_version,
    run_disposable_lab_body_with_cleanup_verification,
    seed_ba201_scenario,
    start_disposable_stack,
    stop_and_cleanup_stack,
)

# A representative set of fake secrets used ONLY by the safety tests below --
# never real credentials. Every safety test that exercises a
# credential-producing code path asserts these substrings are ABSENT from
# every log file and every raised exception's `str(...)`.
_FAKE_SERVICE_ROLE_KEY = "eyJFAKESERVICEROLEKEYPAYLOAD.sim-int-02a-secret-should-never-leak"
_FAKE_ANON_KEY = "eyJFAKEANONKEYPAYLOAD.sim-int-02a-anon-secret-should-never-leak"
_FAKE_DB_PASSWORD = "SimInt02aFakeDbPassword!Secret"
_FAKE_DB_URL = f"postgresql://postgres:{_FAKE_DB_PASSWORD}@127.0.0.1:54399/postgres"
_ALL_FAKE_SECRETS = (_FAKE_SERVICE_ROLE_KEY, _FAKE_ANON_KEY, _FAKE_DB_PASSWORD, _FAKE_DB_URL)


def _fake_status_json_text() -> str:
    return json.dumps(
        {
            "API_URL": "http://127.0.0.1:54398",
            "DB_URL": _FAKE_DB_URL,
            "SERVICE_ROLE_KEY": _FAKE_SERVICE_ROLE_KEY,
            "ANON_KEY": _FAKE_ANON_KEY,
        }
    )


def _completed_process(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_ports() -> LabPorts:
    """Ten distinct placeholder port values (never real/allocated ports) --
    used only by tests that need SOME valid `LabPorts` instance but do not
    care about its exact values."""
    return LabPorts(
        api=1,
        db=2,
        shadow_db=3,
        pooler=4,
        studio=5,
        inbucket_web=6,
        inbucket_smtp=7,
        inbucket_pop3=8,
        analytics=9,
        edge_runtime_inspector=10,
    )


def _assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in _ALL_FAKE_SECRETS:
            assert secret not in text, f"leaked secret {secret[:12]}... found in: {text[:200]!r}"


def _assert_no_secrets_in_exception_chain(exc: BaseException) -> None:
    """Walk the FULL `__cause__`/`__context__` chain of `exc` (cycle-safe)
    and assert none of the fake secrets appear in any linked exception's
    own `str(...)`. Used alongside explicit `__cause__ is None` /
    `__context__ is None` assertions -- this additionally proves that even
    if some future change reintroduced chaining, no secret would leak
    through it."""
    seen: set = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        _assert_no_secrets(str(current))
        current = current.__cause__ or current.__context__


def _fake_run_with_local_docker_endpoint(other_handler, *, context_name: str = "default", endpoint: str = "unix:///var/run/docker.sock"):
    """Wrap another fake `subprocess.run` handler so that `docker context
    show`/`docker context inspect` -- the two new SIM-INT-02C endpoint-
    guard preflight commands -- are answered with a fixed LOCAL (`unix://`)
    endpoint, letting a test focus purely on a DOWNSTREAM command's own
    behavior (typically `docker version`) without having to also simulate
    the endpoint guard's own commands in every single test."""

    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "context", "show"]:
            return _completed_process(args, returncode=0, stdout=f"{context_name}\n", stderr="")
        if args[:3] == ["docker", "context", "inspect"]:
            return _completed_process(args, returncode=0, stdout=f"{endpoint}\n", stderr="")
        return other_handler(args, **kwargs)

    return fake_run


# =============================================================================
# 1. Gating: the real integration test stays skipped without the opt-in var.
# =============================================================================


def test_opt_in_helper_reports_false_without_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION", raising=False)
    assert is_local_supabase_integration_enabled() is False


def test_opt_in_helper_reports_true_only_for_exact_value_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION", "1")
    assert is_local_supabase_integration_enabled() is True
    monkeypatch.setenv("CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION", "true")
    assert is_local_supabase_integration_enabled() is False
    monkeypatch.setenv("CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION", "0")
    assert is_local_supabase_integration_enabled() is False


# =============================================================================
# 2. Docker-daemon preflight happens BEFORE any temporary directory exists.
# =============================================================================


def test_docker_daemon_failure_raises_before_any_temp_directory_is_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def other_handler(args, **kwargs):
        assert args[:2] == ["docker", "version"]
        return _completed_process(args, returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")

    before = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    with mock.patch(
        "ba201_supabase_lab.subprocess.run", side_effect=_fake_run_with_local_docker_endpoint(other_handler)
    ) as mocked_run:
        with pytest.raises(Ba201IntegrationLabError):
            with disposable_ba201_lab():
                pass  # pragma: no cover - must never be reached
    after = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    assert after == before, "a lab directory was created despite the Docker daemon being unavailable"
    # Only Docker preflight commands were ever attempted -- never `init`/`start`.
    called_args = [call.args[0] for call in mocked_run.call_args_list]
    assert all(args[0] == "docker" for args in called_args)
    assert not any("init" in args or "start" in args for args in called_args)


def test_check_docker_daemon_available_raises_sanitized_error_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def other_handler(args, **kwargs):
        return _completed_process(args, returncode=1, stdout="", stderr="docker: error during connect")

    with mock.patch(
        "ba201_supabase_lab.subprocess.run", side_effect=_fake_run_with_local_docker_endpoint(other_handler)
    ):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            check_docker_daemon_available()
    assert "error during connect" not in str(excinfo.value)


def test_check_docker_daemon_available_succeeds_on_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def other_handler(args, **kwargs):
        return _completed_process(args, returncode=0, stdout="Docker version 29.6.1", stderr="")

    with mock.patch(
        "ba201_supabase_lab.subprocess.run", side_effect=_fake_run_with_local_docker_endpoint(other_handler)
    ):
        check_docker_daemon_available()  # must not raise


# =============================================================================
# SIM-INT-02C: local Docker-endpoint guard. `docker version` succeeding
# proves ONLY that Docker responded -- these tests prove the EFFECTIVE
# endpoint (honoring a `DOCKER_HOST` override, else the current context's
# own recorded endpoint, which itself already reflects a `DOCKER_CONTEXT`
# override) must classify as local IPC (`npipe://` / `unix://`) before
# `check_docker_daemon_available()` will ever succeed.
# =============================================================================


def _fake_run_docker_context_endpoint(*, context_name: str = "default", endpoint: str = "unix:///var/run/docker.sock"):
    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "context", "show"]:
            return _completed_process(args, returncode=0, stdout=f"{context_name}\n", stderr="")
        if args[:3] == ["docker", "context", "inspect"]:
            return _completed_process(args, returncode=0, stdout=f"{endpoint}\n", stderr="")
        raise AssertionError(f"`docker version` must never be reached once the endpoint guard rejects: {args}")

    return fake_run


def test_windows_npipe_endpoint_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_with_local_docker_endpoint(
            lambda args, **kwargs: _completed_process(args, returncode=0, stdout="Docker version 29.6.1", stderr=""),
            endpoint="npipe:////./pipe/docker_engine",
        ),
    ):
        check_docker_daemon_available()  # must not raise


def test_unix_socket_endpoint_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_with_local_docker_endpoint(
            lambda args, **kwargs: _completed_process(args, returncode=0, stdout="Docker version 29.6.1", stderr=""),
            endpoint="unix:///var/run/docker.sock",
        ),
    ):
        check_docker_daemon_available()  # must not raise


def test_tcp_endpoint_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(endpoint="tcp://203.0.113.5:2376"),
    ):
        with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
            check_docker_daemon_available()


def test_ssh_endpoint_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(endpoint="ssh://user@203.0.113.5"),
    ):
        with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
            check_docker_daemon_available()


def test_http_and_https_endpoints_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for scheme_endpoint in ("http://203.0.113.5:2375", "https://203.0.113.5:2376"):
        with mock.patch(
            "ba201_supabase_lab.subprocess.run",
            side_effect=_fake_run_docker_context_endpoint(endpoint=scheme_endpoint),
        ):
            with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
                check_docker_daemon_available()


def test_malformed_endpoint_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    for malformed in ("", "   ", "not-a-valid-endpoint", "unix:///var/run/docker.sock\nextra-garbage-line"):
        with mock.patch(
            "ba201_supabase_lab.subprocess.run",
            side_effect=_fake_run_docker_context_endpoint(endpoint=malformed),
        ):
            with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
                check_docker_daemon_available()


def test_docker_host_env_var_remote_override_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIM-INT-02D case 3: `DOCKER_CONTEXT` is ABSENT and `DOCKER_HOST` is a
    remote override -- per the real Docker CLI's own documented precedence,
    `DOCKER_HOST` applies ONLY when no overriding `DOCKER_CONTEXT` is
    active, which is exactly this scenario. Classification must happen from
    the environment variable directly, WITHOUT even attempting a `docker
    context show`/`docker context inspect`/`docker version` subprocess
    call."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://203.0.113.5:2376")

    def fake_run(args, **kwargs):
        raise AssertionError(f"no subprocess call should occur once DOCKER_HOST is a rejected remote override: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
            check_docker_daemon_available()


def test_docker_context_selecting_remote_context_is_rejected_through_inspected_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates `DOCKER_CONTEXT` having selected some non-default context
    -- in the real CLI, `docker context show` itself is what reflects that
    override -- so this proves rejection follows from the INSPECTED
    endpoint of whatever context is shown, not from reading
    `DOCKER_CONTEXT`'s value directly."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-ctx")
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(context_name="remote-ctx", endpoint="tcp://198.51.100.9:2376"),
    ):
        with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
            check_docker_daemon_available()


# =============================================================================
# SIM-INT-02D: correct Docker CLI precedence -- `DOCKER_CONTEXT`, when set to
# a non-empty value, overrides `DOCKER_HOST` entirely; `DOCKER_HOST` applies
# only when no overriding `DOCKER_CONTEXT` is active; otherwise the
# configured/default current context applies. These four tests each pin
# down exactly ONE cell of that precedence table using a deliberately
# CONFLICTING pair of values (a remote context alongside a local host, or
# vice versa) so that only the correct precedence -- never a coincidence --
# can make the test pass.
# =============================================================================


def test_docker_context_remote_wins_over_docker_host_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 1: `DOCKER_CONTEXT` selects a REMOTE context while `DOCKER_HOST`
    is (deliberately, conflictingly) set to a LOCAL endpoint. The remote
    context must win -- the result must be REJECTED -- proving
    `DOCKER_HOST`'s locally-classifiable value is never consulted as the
    authority once an overriding `DOCKER_CONTEXT` is active."""
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-ctx")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")  # conflicting LOCAL value; must be ignored.
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(context_name="remote-ctx", endpoint="tcp://198.51.100.9:2376"),
    ) as mocked_run:
        with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
            check_docker_daemon_available()
    # The context WAS actually resolved/inspected (proving the remote
    # context -- not a coincidental default -- is what drove the rejection).
    called_args = [call.args[0] for call in mocked_run.call_args_list]
    assert any(args[:3] == ["docker", "context", "show"] for args in called_args)
    assert any(args[:3] == ["docker", "context", "inspect"] for args in called_args)


def test_docker_context_local_wins_over_docker_host_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 2: `DOCKER_CONTEXT` selects a LOCAL context while `DOCKER_HOST`
    is (deliberately, conflictingly) set to a REMOTE endpoint. The local
    context must win -- the endpoint guard must PASS (and `docker version`
    must then be allowed to run) -- proving the remote `DOCKER_HOST` value
    is never consulted as the authority once an overriding `DOCKER_CONTEXT`
    is active."""
    monkeypatch.setenv("DOCKER_CONTEXT", "local-ctx")
    monkeypatch.setenv("DOCKER_HOST", "tcp://203.0.113.5:2376")  # conflicting REMOTE value; must be ignored.

    def other_handler(args, **kwargs):
        assert args[:2] == ["docker", "version"]
        return _completed_process(args, returncode=0, stdout="Docker version 29.6.1", stderr="")

    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_with_local_docker_endpoint(other_handler, context_name="local-ctx"),
    ) as mocked_run:
        check_docker_daemon_available()  # must not raise
    called_args = [call.args[0] for call in mocked_run.call_args_list]
    assert any(args[:3] == ["docker", "context", "show"] for args in called_args)
    assert any(args[:3] == ["docker", "context", "inspect"] for args in called_args)


def test_docker_context_and_host_both_absent_inspects_current_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 4: neither override is set -- the configured/default CURRENT
    context must be resolved and inspected (`docker context show` followed
    by `docker context inspect`) rather than the guard trivially passing or
    failing without ever consulting it."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def other_handler(args, **kwargs):
        assert args[:2] == ["docker", "version"]
        return _completed_process(args, returncode=0, stdout="Docker version 29.6.1", stderr="")

    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_with_local_docker_endpoint(other_handler),
    ) as mocked_run:
        check_docker_daemon_available()  # must not raise
    called_args = [call.args[0] for call in mocked_run.call_args_list]
    assert any(args[:3] == ["docker", "context", "show"] for args in called_args)
    assert any(args[:3] == ["docker", "context", "inspect"] for args in called_args)


def test_empty_string_docker_context_and_docker_host_are_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case 5: an environment variable explicitly set to the EMPTY STRING
    (as opposed to unset/deleted) must be treated exactly like an absent
    variable -- the configured/default current context is inspected, and a
    remote-looking empty value never accidentally classifies as anything
    other than absent."""
    monkeypatch.setenv("DOCKER_CONTEXT", "")
    monkeypatch.setenv("DOCKER_HOST", "")

    def other_handler(args, **kwargs):
        assert args[:2] == ["docker", "version"]
        return _completed_process(args, returncode=0, stdout="Docker version 29.6.1", stderr="")

    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_with_local_docker_endpoint(other_handler),
    ) as mocked_run:
        check_docker_daemon_available()  # must not raise
    called_args = [call.args[0] for call in mocked_run.call_args_list]
    assert any(args[:3] == ["docker", "context", "show"] for args in called_args)
    assert any(args[:3] == ["docker", "context", "inspect"] for args in called_args)


def test_docker_host_secret_bearing_value_never_leaks_into_logs_or_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 6 (`DOCKER_HOST` branch): `DOCKER_HOST` is classified directly
    in Python -- never passed through a subprocess call -- so a
    secret-bearing value must still never surface in a log file or a raised
    exception's own text."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    secret_bearing_host = f"tcp://attacker.example:2376/?token={_FAKE_SERVICE_ROLE_KEY}"
    monkeypatch.setenv("DOCKER_HOST", secret_bearing_host)
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        raise AssertionError(f"no subprocess call should occur for a rejected DOCKER_HOST override: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            check_docker_daemon_available(log_path=log_path)
    _assert_no_secrets(str(excinfo.value))
    assert secret_bearing_host not in str(excinfo.value)
    assert not log_path.exists() or secret_bearing_host not in log_path.read_text(encoding="utf-8")


def test_docker_context_env_var_name_never_leaks_when_context_branch_is_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 6 (`DOCKER_CONTEXT` branch): a secret-bearing `DOCKER_CONTEXT`
    VALUE must never surface in a raised exception's own text, even though
    the ACTUAL context name resolved via `docker context show` (never the
    raw `DOCKER_CONTEXT` environment value itself) is what gets inspected
    and classified."""
    secret_bearing_docker_context_value = f"remote-ctx-{_FAKE_ANON_KEY}"
    monkeypatch.setenv("DOCKER_CONTEXT", secret_bearing_docker_context_value)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(
            context_name=secret_bearing_docker_context_value, endpoint="tcp://198.51.100.9:2376"
        ),
    ):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            check_docker_daemon_available()
    _assert_no_secrets(str(excinfo.value))
    assert secret_bearing_docker_context_value not in str(excinfo.value)


def test_local_docker_endpoint_rejection_occurs_before_any_temp_directory_is_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    before = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(endpoint="tcp://203.0.113.5:2376"),
    ) as mocked_run:
        with pytest.raises(Ba201IntegrationLabError, match="local IPC"):
            with disposable_ba201_lab():
                pass  # pragma: no cover - must never be reached
    after = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    assert after == before, "a lab directory was created despite a rejected remote Docker endpoint"
    called_args = [call.args[0] for call in mocked_run.call_args_list]
    assert not any("init" in args or "start" in args for args in called_args)


def test_no_docker_endpoint_text_reaches_logs_or_exceptions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    log_path = tmp_path / "lab.log"
    secret_bearing_endpoint = f"tcp://attacker.example:2376/?token={_FAKE_SERVICE_ROLE_KEY}"
    secret_bearing_context_name = f"ctx-{_FAKE_ANON_KEY}"

    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_fake_run_docker_context_endpoint(
            context_name=secret_bearing_context_name, endpoint=secret_bearing_endpoint
        ),
    ):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            check_docker_daemon_available(log_path=log_path)
    _assert_no_secrets(str(excinfo.value))
    assert secret_bearing_endpoint not in str(excinfo.value)
    assert secret_bearing_context_name not in str(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert secret_bearing_endpoint not in log_text
    assert secret_bearing_context_name not in log_text


# =============================================================================
# SIM-INT-02B: complete port-isolation -- ten distinct host bindings,
# section-aware config.toml patching, and fail-closed shape verification.
# =============================================================================


def test_lab_ports_allocate_returns_ten_unique_ports() -> None:
    ports = LabPorts.allocate()
    values = ports.as_tuple()
    assert len(values) == 10
    assert len(set(values)) == 10
    assert ports.all_unique() is True


def test_lab_ports_all_free_checks_all_ten_ports() -> None:
    """Mocks `_port_is_free(...)` rather than relying on a real live socket
    bind -- `SO_REUSEADDR` has platform-dependent (notably Windows) semantics
    that make a genuine "is this exact port now occupied" bind-race
    unreliable across platforms. This proves the SAME thing deterministically:
    every one of the ten fields is actually passed to `_port_is_free(...)`,
    and a single "occupied" report for ANY one of them (not just a fixed
    subset like `api`/`db`) makes the whole check fail."""
    ports = LabPorts.allocate()
    checked_ports = []

    def fake_port_is_free(port: int) -> bool:
        checked_ports.append(port)
        return True

    with mock.patch("ba201_supabase_lab._port_is_free", side_effect=fake_port_is_free):
        assert ports.all_free() is True
    assert sorted(checked_ports) == sorted(ports.as_tuple())

    for target_port in ports.as_tuple():
        with mock.patch(
            "ba201_supabase_lab._port_is_free",
            side_effect=lambda port, _target=target_port: port != _target,
        ):
            assert ports.all_free() is False, f"all_free() did not detect port {target_port} was occupied"


def _real_generated_config_text(project_id_placeholder: str = "placeholder") -> str:
    """A byte-for-byte copy of the RELEVANT sections of `supabase init`'s own
    generated `config.toml` under CLI 2.109.1 (confirmed directly by running
    `supabase init` in a scratch OS-temp directory during SIM-INT-02B
    preflight) -- enough of the real file's shape for
    `_patch_config_ports(...)` to be exercised faithfully without actually
    invoking the CLI in every test."""
    return f"""# For detailed configuration reference documentation, visit:
# https://supabase.com/docs/guides/local-development/cli/config
project_id = "{project_id_placeholder}"

[api]
enabled = true
port = 54321
schemas = ["public", "graphql_public"]

[db]
port = 54322
shadow_port = 54320
[db.pooler]
port = 54329
[db.migrations]
schema_paths = []

[studio]
port = 54323

[local_smtp]
port = 54324
# Uncomment to expose additional ports for testing user applications that send emails.
# smtp_port = 54325
# pop3_port = 54326

[edge_runtime]
inspector_port = 8083

[analytics]
port = 54327
"""


def test_patch_config_ports_patches_smtp_and_pop3_and_uncomments_them() -> None:
    ports = _fake_ports()
    patched = ba201_supabase_lab._patch_config_ports(_real_generated_config_text(), project_id="p1", ports=ports)
    assert f"smtp_port = {ports.inbucket_smtp}" in patched
    assert f"# smtp_port = {ports.inbucket_smtp}" not in patched
    assert f"pop3_port = {ports.inbucket_pop3}" in patched
    assert f"# pop3_port = {ports.inbucket_pop3}" not in patched
    assert f"port = {ports.inbucket_web}" in patched


def test_patch_config_ports_removes_every_expected_default() -> None:
    ports = _fake_ports()
    patched = ba201_supabase_lab._patch_config_ports(_real_generated_config_text(), project_id="p1", ports=ports)
    for default_line in (
        "port = 54321",
        "port = 54322",
        "shadow_port = 54320",
        "port = 54329",
        "port = 54323",
        "port = 54324",
        "smtp_port = 54325",
        "pop3_port = 54326",
        "port = 54327",
        "inspector_port = 8083",
    ):
        assert default_line not in patched, f"default line {default_line!r} was not removed"


def test_patch_config_ports_fails_closed_on_missing_section() -> None:
    config_without_analytics = _real_generated_config_text().replace("[analytics]\nport = 54327\n", "")
    with pytest.raises(Ba201IntegrationLabError, match=r"\[analytics\]"):
        ba201_supabase_lab._patch_config_ports(config_without_analytics, project_id="p1", ports=_fake_ports())


def test_patch_config_ports_fails_closed_on_duplicate_field_in_section() -> None:
    config_with_duplicate_studio_port = _real_generated_config_text().replace(
        "[studio]\nport = 54323\n", "[studio]\nport = 54323\nport = 54323\n"
    )
    with pytest.raises(Ba201IntegrationLabError, match="studio.port"):
        ba201_supabase_lab._patch_config_ports(config_with_duplicate_studio_port, project_id="p1", ports=_fake_ports())


def test_patch_config_ports_fails_closed_on_missing_field_in_section() -> None:
    config_without_shadow_port = _real_generated_config_text().replace("shadow_port = 54320\n", "")
    with pytest.raises(Ba201IntegrationLabError, match="db.shadow_port"):
        ba201_supabase_lab._patch_config_ports(config_without_shadow_port, project_id="p1", ports=_fake_ports())


# =============================================================================
# 3 & 4. Sensitive output is never written to logs and never exposed via
#        raised exceptions -- covers the required "safe in-memory status
#        parser" test with a fake service-role key, anon key, DB password,
#        and complete DB URL all present in the raw fake CLI output.
# =============================================================================


def test_supabase_start_and_status_secrets_never_written_to_log(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            return _completed_process(args, returncode=0, stdout=f"started; {_FAKE_SERVICE_ROLE_KEY}", stderr="")
        if args[:2] == ["supabase", "status"]:
            return _completed_process(args, returncode=0, stdout=_fake_status_json_text(), stderr="")
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        stack = start_disposable_stack(
            tmp_path,
            project_id="certbound-sim-int-02a-test",
            ports=_fake_ports(),
            log_path=log_path,
        )

    assert stack.service_role_key == _FAKE_SERVICE_ROLE_KEY  # in-memory only -- correctly parsed
    assert not hasattr(stack, "anon_key")  # SIM-INT-02A: anon_key must not exist on LocalSupabaseStack at all

    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    # The log must still contain SAFE metadata (category/exit code), proving
    # something was actually recorded and this isn't a vacuous assertion.
    assert "category=supabase.start" in log_text
    assert "category=supabase.status" in log_text
    assert "exit_code=0" in log_text


def test_supabase_start_failure_with_secret_bearing_stderr_never_leaks_via_exception(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            return _completed_process(
                args,
                returncode=1,
                stdout="",
                stderr=f"start failed while connecting to postgresql://postgres:{_FAKE_DB_PASSWORD}@127.0.0.1:5432/postgres",
            )
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            start_disposable_stack(
                tmp_path,
                project_id="certbound-sim-int-02a-test",
                ports=_fake_ports(),
                log_path=log_path,
            )

    _assert_no_secrets(str(excinfo.value))
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)


def test_status_missing_expected_key_error_names_only_the_fixed_logical_field(tmp_path: Path) -> None:
    """SIM-INT-02B: `_extract_status_field(...)` must name ONLY its own
    fixed logical field name (e.g. `"service_role_key"`) on failure -- NOT
    any raw JSON key name (e.g. `"SERVICE_ROLE_KEY"`) or value actually
    present in the untrusted `supabase status -o json` document."""
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        if args[:2] == ["supabase", "status"]:
            # SERVICE_ROLE_KEY deliberately omitted; ANON_KEY present with a
            # fake secret value to prove the exception never echoes it.
            return _completed_process(
                args,
                returncode=0,
                stdout=json.dumps({"API_URL": "http://127.0.0.1:1", "DB_URL": _FAKE_DB_URL, "ANON_KEY": _FAKE_ANON_KEY}),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            start_disposable_stack(
                tmp_path,
                project_id="certbound-sim-int-02a-test",
                ports=_fake_ports(),
                log_path=log_path,
            )
    _assert_no_secrets(str(excinfo.value))
    assert "service_role_key" in str(excinfo.value)  # the FIXED logical field name is safe and useful
    # The raw JSON key names actually present in the untrusted document
    # (including the one that WAS present, "ANON_KEY") must never appear.
    assert "SERVICE_ROLE_KEY" not in str(excinfo.value)
    assert "ANON_KEY" not in str(excinfo.value)
    assert "API_URL" not in str(excinfo.value)


# =============================================================================
# 5. Subprocess timeout does not expose captured secrets.
# =============================================================================


def test_subprocess_timeout_does_not_expose_captured_output(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=kwargs.get("timeout") or 1,
            output=f"partial output containing {_FAKE_SERVICE_ROLE_KEY}".encode(),
            stderr=f"partial stderr containing {_FAKE_DB_PASSWORD}".encode(),
        )

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            start_disposable_stack(
                tmp_path,
                project_id="certbound-sim-int-02a-test",
                ports=_fake_ports(),
                log_path=log_path,
            )
    _assert_no_secrets(str(excinfo.value))
    # SIM-INT-02C: the replacement exception retains NO trace of the
    # original `subprocess.TimeoutExpired` (which carried the fake secrets
    # in its own `.output`/`.stderr`) as either `__cause__` or `__context__`.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert "classification=timeout" in log_text


def test_missing_executable_does_not_raise_unsanitized_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    log_path = tmp_path / "lab.log"
    raw_os_message = "[WinError 2] The system cannot find the file specified: 'docker'"
    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=FileNotFoundError(raw_os_message)):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            check_docker_daemon_available(log_path=log_path)
    # The raised error must be our own fixed, generic message -- never the
    # raw OS-level exception text.
    assert raw_os_message not in str(excinfo.value)
    assert "executable not found" in str(excinfo.value)
    # SIM-INT-02C: no chaining at all -- the original `FileNotFoundError` is
    # neither `__cause__` nor `__context__`.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    log_text = log_path.read_text(encoding="utf-8")
    assert raw_os_message not in log_text
    assert "classification=executable_not_found" in log_text


# =============================================================================
# SIM-INT-02C: `_CommandResult`'s own `repr(...)`/`str(...)` must never
# expose raw stdout/stderr, even though those fields remain ordinary,
# directly-readable attributes for the narrow parser callers that need them.
# =============================================================================


def test_command_result_repr_and_str_never_expose_stdout_or_stderr() -> None:
    arbitrary_stderr_secret = "XYZ789-ARBITRARY-STDERR-SECRET-DO-NOT-LEAK"
    result = ba201_supabase_lab._CommandResult(
        returncode=1,
        stdout=f"stdout leaking {_FAKE_SERVICE_ROLE_KEY} and {_FAKE_DB_URL}",
        stderr=f"stderr leaking {_FAKE_ANON_KEY} and {arbitrary_stderr_secret}",
        classification="cli_nonzero_exit",
    )
    rendered_repr = repr(result)
    rendered_str = str(result)
    for secret in (*_ALL_FAKE_SECRETS, arbitrary_stderr_secret):
        assert secret not in rendered_repr, f"repr() leaked {secret[:12]}..."
        assert secret not in rendered_str, f"str() leaked {secret[:12]}..."
    # The rendering must still be USEFUL -- returncode and classification
    # (both non-sensitive, fixed/small values) are present.
    assert "returncode=1" in rendered_repr
    assert "cli_nonzero_exit" in rendered_repr
    # stdout/stderr remain reachable in memory for narrow parser callers --
    # only ORDINARY OBJECT RENDERING is blocked, not the fields themselves.
    assert _FAKE_SERVICE_ROLE_KEY in result.stdout
    assert _FAKE_ANON_KEY in result.stderr


# =============================================================================
# SIM-INT-02C: `_run_cli_command(...)`'s replacement exception retains NO
# original subprocess exception as `__cause__`/`__context__`, across all
# four handled subprocess-launch-failure categories -- tested directly
# against `_run_cli_command(...)` itself (rather than only indirectly
# through a particular caller) for the most precise possible coverage.
# =============================================================================


def test_run_cli_command_timeout_has_no_cause_or_context_and_leaks_nothing(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=1,
            output=f"partial stdout containing {_FAKE_SERVICE_ROLE_KEY}".encode(),
            stderr=f"partial stderr containing {_FAKE_DB_PASSWORD}".encode(),
        )

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            ba201_supabase_lab._run_cli_command(
                ["supabase", "start"], category="supabase.start", log_path=log_path, timeout=1
            )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert "classification=timeout" in log_text


def test_run_cli_command_missing_executable_has_no_cause_or_context_and_leaks_nothing(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"
    raw_message = f"[WinError 2] cannot find 'docker' near {_FAKE_SERVICE_ROLE_KEY}"

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=FileNotFoundError(raw_message)):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            ba201_supabase_lab._run_cli_command(
                ["docker", "version"], category="docker.daemon_check", log_path=log_path, timeout=1
            )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)
    assert raw_message not in str(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert raw_message not in log_text
    assert "classification=executable_not_found" in log_text


def test_run_cli_command_os_error_has_no_cause_or_context_and_leaks_nothing(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"
    raw_message = f"low-level OS failure referencing {_FAKE_DB_PASSWORD}"

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=OSError(raw_message)):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            ba201_supabase_lab._run_cli_command(
                ["docker", "version"], category="docker.daemon_check", log_path=log_path, timeout=1
            )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)
    assert raw_message not in str(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert "classification=os_error" in log_text


def test_run_cli_command_unexpected_exception_has_no_cause_or_context_and_leaks_nothing(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"

    class _WeirdSubprocessFailure(Exception):
        """Models a non-standard/unforeseen exception type that might carry
        a secret-bearing attribute a naive handler could accidentally
        stringify -- distinct from the four `except` clauses' own named
        types, to exercise `_run_cli_command(...)`'s final catch-all
        `except Exception` branch."""

        def __init__(self, secret_payload: str) -> None:
            super().__init__("a non-standard subprocess launch failure")
            self.secret_payload = secret_payload

    def fake_run(args, **kwargs):
        raise _WeirdSubprocessFailure(_FAKE_ANON_KEY)

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            ba201_supabase_lab._run_cli_command(
                ["docker", "version"], category="docker.daemon_check", log_path=log_path, timeout=1
            )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert "classification=unknown_subprocess_error" in log_text


# =============================================================================
# SIM-INT-02E: Windows subprocess reader-thread decode safety.
#
# `_run_cli_command(...)` now passes `encoding="utf-8", errors="replace"`
# EXPLICITLY to `subprocess.run(...)` (see that function's own SIM-INT-02E
# docstring section) instead of relying on the OS default/locale text
# encoding (`cp1252` on Windows), which cannot decode every byte sequence
# real CLI output can contain and, left implicit, crashes CPython's own
# background subprocess reader thread with an unhandled
# `UnicodeDecodeError` -- observed under pytest as a
# `PytestUnhandledThreadExceptionWarning`. These tests spawn REAL
# subprocesses (this interpreter itself, via `sys.executable` -- never
# Docker/Supabase) that deliberately emit an undecodable byte (`0x90`,
# invalid in both `cp1252` and UTF-8) to prove the reader thread can no
# longer crash, and that the existing credential-safety guarantees
# (never-logged, never-in-exception, sanitized `_CommandResult` rendering)
# still hold even when the underlying bytes are undecodable.
# =============================================================================


def test_real_subprocess_with_invalid_bytes_in_stdout_does_not_raise_reader_thread_exception(tmp_path: Path) -> None:
    """A real (not mocked) subprocess writing an undecodable byte to stdout
    must not raise `UnicodeDecodeError` -- from this call, OR from any
    background reader thread (which `pytest-asyncio`'s/pytest's own thread-
    exception hook would otherwise surface as a
    `PytestUnhandledThreadExceptionWarning`, which this test's own PASS
    result -- combined with running under `pytest -W error::...` in CI, or
    simply the absence of that warning in this run's own output -- proves
    did not happen)."""
    log_path = tmp_path / "lab.log"
    result = ba201_supabase_lab._run_cli_command(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0x90, 0x41])); sys.stdout.buffer.flush()"],
        category="test.invalid_stdout_bytes",
        log_path=log_path,
        timeout=30,
    )
    assert result.returncode == 0
    assert isinstance(result.stdout, str)
    # The undecodable byte (0x90) must have been replaced, never raised;
    # the following valid byte ('A') must have decoded normally.
    assert "A" in result.stdout


def test_real_subprocess_with_invalid_bytes_in_stderr_does_not_raise_reader_thread_exception(tmp_path: Path) -> None:
    """Symmetric with the stdout case above -- stderr is decoded by a
    SEPARATE reader thread when both streams are piped, so this must be
    proven independently."""
    log_path = tmp_path / "lab.log"
    result = ba201_supabase_lab._run_cli_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.buffer.write(bytes([0x90, 0x42])); sys.stderr.buffer.flush(); sys.exit(1)",
        ],
        category="test.invalid_stderr_bytes",
        log_path=log_path,
        timeout=30,
    )
    assert result.returncode == 1
    assert isinstance(result.stderr, str)
    assert "B" in result.stderr


def test_invalid_bytes_in_stdout_are_handled_deterministically(tmp_path: Path) -> None:
    """The SAME undecodable input must produce the SAME decoded `str` value
    every time -- `errors="replace"` is a deterministic substitution, never
    a source of flaky/nondeterministic test behavior."""
    log_path = tmp_path / "lab.log"
    args = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0x90, 0x90, 0x43])); sys.stdout.buffer.flush()"]
    first = ba201_supabase_lab._run_cli_command(args, category="test.deterministic_stdout", log_path=log_path, timeout=30)
    second = ba201_supabase_lab._run_cli_command(args, category="test.deterministic_stdout", log_path=log_path, timeout=30)
    assert first.stdout == second.stdout
    assert "C" in first.stdout
    assert first.stdout.count("\ufffd") == 2


def test_invalid_bytes_in_stderr_are_handled_deterministically(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"
    args = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.buffer.write(bytes([0x90, 0x90, 0x44])); sys.stderr.buffer.flush(); sys.exit(1)",
    ]
    first = ba201_supabase_lab._run_cli_command(args, category="test.deterministic_stderr", log_path=log_path, timeout=30)
    second = ba201_supabase_lab._run_cli_command(args, category="test.deterministic_stderr", log_path=log_path, timeout=30)
    assert first.stderr == second.stderr
    assert "D" in first.stderr
    assert first.stderr.count("\ufffd") == 2


def test_secret_and_invalid_bytes_combined_never_leak_through_result_or_log(tmp_path: Path) -> None:
    """A real subprocess emitting a secret-like string ADJACENT TO an
    undecodable byte, on a FAILING exit code (so `_run_cli_command(...)`
    both classifies the failure and appends a safe log line), must still
    never expose that secret through `_CommandResult.__repr__`/`__str__`
    or the persisted log -- proving the decode-safety fix did not, itself,
    open any new leak path alongside the pre-existing SIM-INT-02A/02C
    rendering/logging guarantees."""
    log_path = tmp_path / "lab.log"
    secret = _FAKE_SERVICE_ROLE_KEY
    script = (
        "import sys; "
        f"sys.stdout.buffer.write('{secret}'.encode('utf-8') + bytes([0x90])); "
        "sys.stdout.buffer.flush(); sys.exit(1)"
    )
    result = ba201_supabase_lab._run_cli_command(
        [sys.executable, "-c", script], category="test.secret_and_invalid_bytes", log_path=log_path, timeout=30
    )
    assert result.returncode == 1
    # The narrow, deliberate parser-facing field STILL holds the secret --
    # this proves the test itself is meaningful (the secret really was
    # produced), while the assertions below prove it never reaches any
    # RENDERING or LOGGING surface.
    assert secret in result.stdout
    assert secret not in repr(result)
    assert secret not in str(result)
    log_text = log_path.read_text(encoding="utf-8")
    assert secret not in log_text
    _assert_no_secrets(repr(result), str(result), log_text)


def test_valid_json_command_output_still_parses_correctly_through_real_subprocess(tmp_path: Path) -> None:
    """The encoding fix must not corrupt ordinary ASCII/UTF-8 JSON output --
    a real subprocess prints valid JSON, and `_run_cli_command(...)`'s own
    decoded `stdout` must round-trip through `json.loads(...)` unchanged,
    exactly like `supabase status -o json`'s own real output would."""
    log_path = tmp_path / "lab.log"
    payload = {"API_URL": "http://127.0.0.1:54321", "DB_URL": "postgresql://postgres:pw@127.0.0.1:54322/postgres"}
    script = f"import sys, json; sys.stdout.write(json.dumps({payload!r}))"
    result = ba201_supabase_lab._run_cli_command(
        [sys.executable, "-c", script], category="test.valid_json_roundtrip", log_path=log_path, timeout=30
    )
    assert result.returncode == 0
    parsed = json.loads(result.stdout)
    assert parsed == payload


def test_undecodable_status_json_fails_with_fixed_sanitized_error(tmp_path: Path) -> None:
    """SIM-INT-02E "MACHINE-READABLE OUTPUT" requirement: if decoded
    `supabase status -o json` output is corrupted (e.g. by a genuinely
    undecodable byte having been substituted with the Unicode replacement
    character, breaking JSON syntax), `start_disposable_stack(...)` must
    fail with the SAME FIXED, sanitized message it already uses for any
    other malformed-JSON case -- never echoing the corrupted content, the
    replacement character, or any secret adjacent to it."""
    log_path = tmp_path / "lab.log"
    # Models exactly what `errors="replace"` would produce from a real
    # undecodable byte landing inside an otherwise well-formed JSON
    # document -- the replacement character breaks JSON syntax here.
    corrupted_json_with_secret = f'{{"API_URL": "http://127.0.0.1:1\ufffd", "DB_URL": "{_FAKE_DB_URL}"'

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        if args[:2] == ["supabase", "status"]:
            return _completed_process(args, returncode=0, stdout=corrupted_json_with_secret, stderr="")
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError, match="did not return valid JSON") as excinfo:
            start_disposable_stack(
                tmp_path, project_id="certbound-sim-int-02e-test", ports=_fake_ports(), log_path=log_path
            )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "\ufffd" not in str(excinfo.value)
    _assert_no_secrets(str(excinfo.value))
    log_text = log_path.read_text(encoding="utf-8")
    assert "\ufffd" not in log_text
    _assert_no_secrets(log_text)


# =============================================================================
# SIM-INT-02B: status/URL sanitization is fully fail-closed. Each test below
# embeds a FAKE secret directly in the untrusted input and proves it never
# reaches `str(exception)`, any chained `__cause__`/`__context__` text
# exposed here, or any persisted log line.
# =============================================================================


def test_malformed_status_json_cannot_leak_secrets(tmp_path: Path) -> None:
    log_path = tmp_path / "lab.log"
    malformed_json_with_secret = f'{{"API_URL": "http://127.0.0.1:1", not-json {_FAKE_SERVICE_ROLE_KEY}'

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        if args[:2] == ["supabase", "status"]:
            return _completed_process(args, returncode=0, stdout=malformed_json_with_secret, stderr="")
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            start_disposable_stack(
                tmp_path, project_id="certbound-sim-int-02b-test", ports=_fake_ports(), log_path=log_path
            )
    _assert_no_secrets(str(excinfo.value))
    # `from None` must have fully suppressed the raw `JSONDecodeError` chain.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)


def test_unexpected_status_key_name_cannot_leak_secrets(tmp_path: Path) -> None:
    """An unexpected/unrecognized JSON key name is itself attacker- or
    CLI-version-controlled input -- it must never be echoed into the raised
    exception either, even though it is "just a key name" and not a value."""
    log_path = tmp_path / "lab.log"
    unexpected_key_name = f"UNEXPECTED_KEY_CONTAINING_{_FAKE_SERVICE_ROLE_KEY}"

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        if args[:2] == ["supabase", "status"]:
            return _completed_process(
                args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "API_URL": "http://127.0.0.1:1",
                        "DB_URL": _FAKE_DB_URL,
                        unexpected_key_name: _FAKE_ANON_KEY,
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            start_disposable_stack(
                tmp_path, project_id="certbound-sim-int-02b-test", ports=_fake_ports(), log_path=log_path
            )
    _assert_no_secrets(str(excinfo.value))
    assert unexpected_key_name not in str(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert unexpected_key_name not in log_text


def test_invalid_url_port_cannot_leak_secrets() -> None:
    """A non-numeric port in a credential-bearing URL raises a raw
    `ValueError` from `urlsplit(...).port` whose OWN message echoes the
    offending (potentially secret-derived) text -- `_redact_url(...)` and
    `_require_loopback(...)` must never let that propagate."""
    malformed_url = f"postgresql://postgres:{_FAKE_DB_PASSWORD}@127.0.0.1:{_FAKE_SERVICE_ROLE_KEY}/postgres"

    redacted = ba201_supabase_lab._redact_url(malformed_url)
    _assert_no_secrets(redacted)
    assert redacted == ba201_supabase_lab._REDACTED_URL_PLACEHOLDER

    with pytest.raises(Ba201IntegrationLabError) as excinfo:
        ba201_supabase_lab._require_loopback(malformed_url, label="Database URL")
    _assert_no_secrets(str(excinfo.value))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_malformed_ipv6_cannot_leak_secrets() -> None:
    """An unclosed/invalid IPv6 bracket in a credential-bearing URL raises a
    raw `ValueError` from `urlsplit(...).hostname` -- same guarantee as the
    invalid-port case above."""
    malformed_ipv6_url = f"postgresql://postgres:{_FAKE_DB_PASSWORD}@[::1:5432/postgres"

    redacted = ba201_supabase_lab._redact_url(malformed_ipv6_url)
    _assert_no_secrets(redacted)
    assert redacted == ba201_supabase_lab._REDACTED_URL_PLACEHOLDER

    with pytest.raises(Ba201IntegrationLabError) as excinfo:
        ba201_supabase_lab._require_loopback(malformed_ipv6_url, label="Database URL")
    _assert_no_secrets(str(excinfo.value))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_redact_url_returns_placeholder_for_missing_scheme_or_host() -> None:
    assert ba201_supabase_lab._redact_url("") == ba201_supabase_lab._REDACTED_URL_PLACEHOLDER
    assert ba201_supabase_lab._redact_url("not-a-url-at-all") == ba201_supabase_lab._REDACTED_URL_PLACEHOLDER
    assert ba201_supabase_lab._redact_url("http://") == ba201_supabase_lab._REDACTED_URL_PLACEHOLDER


def test_redact_url_accepts_a_well_formed_loopback_url() -> None:
    assert ba201_supabase_lab._redact_url("http://127.0.0.1:54321/some/path?x=1") == "http://127.0.0.1:54321"


def test_no_exception_cause_retains_credential_bearing_command_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_run_cli_command(...)`'s own `subprocess.TimeoutExpired` handler
    never uses `from exc`, and -- per SIM-INT-02C -- never even RAISES while
    still lexically/dynamically inside that `except` clause, so the
    resulting `Ba201IntegrationLabError` has BOTH `__cause__ is None` AND
    `__context__ is None` (no implicit chaining at all, not merely a
    secret-free chained message)."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=kwargs.get("timeout") or 1,
            output=f"partial output containing {_FAKE_SERVICE_ROLE_KEY}".encode(),
            stderr=f"partial stderr containing {_FAKE_DB_PASSWORD}".encode(),
        )

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            check_docker_daemon_available(log_path=log_path)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets(str(excinfo.value))
    _assert_no_secrets_in_exception_chain(excinfo.value)
    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)


# =============================================================================
# 6-11. `stop_and_cleanup_stack(...)` fails CLOSED.
# =============================================================================


def _mock_cleanup_commands(*, stop_rc, ps_rc, ps_stdout, vol_rc, vol_stdout):
    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "stop"]:
            return _completed_process(args, returncode=stop_rc, stdout="", stderr="" if stop_rc == 0 else "stop failed")
        if args[:2] == ["docker", "ps"]:
            return _completed_process(args, returncode=ps_rc, stdout=ps_stdout, stderr="" if ps_rc == 0 else "docker ps failed")
        if args[:2] == ["docker", "volume"]:
            return _completed_process(args, returncode=vol_rc, stdout=vol_stdout, stderr="" if vol_rc == 0 else "docker volume ls failed")
        raise AssertionError(f"unexpected command: {args}")

    return fake_run


def test_nonzero_supabase_stop_fails_cleanup_verification(tmp_path: Path) -> None:
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_mock_cleanup_commands(stop_rc=1, ps_rc=0, ps_stdout="", vol_rc=0, vol_stdout=""),
    ):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.stop_exit_code == 1
    assert report.cleanup_verified is False


def test_failed_docker_ps_fails_cleanup_verification(tmp_path: Path) -> None:
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_mock_cleanup_commands(stop_rc=0, ps_rc=1, ps_stdout="", vol_rc=0, vol_stdout=""),
    ):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.container_query_exit_code == 1
    assert report.cleanup_verified is False
    # Unknown state must never be reported as "confirmed empty".
    assert report.remaining_containers == ()


def test_failed_docker_volume_ls_fails_cleanup_verification(tmp_path: Path) -> None:
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_mock_cleanup_commands(stop_rc=0, ps_rc=0, ps_stdout="", vol_rc=1, vol_stdout=""),
    ):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.volume_query_exit_code == 1
    assert report.cleanup_verified is False


def test_remaining_container_fails_cleanup_verification(tmp_path: Path) -> None:
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_mock_cleanup_commands(stop_rc=0, ps_rc=0, ps_stdout="certbound-sim-int-02-abc_db_1\n", vol_rc=0, vol_stdout=""),
    ):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.remaining_containers == ("certbound-sim-int-02-abc_db_1",)
    assert report.cleanup_verified is False


def test_remaining_volume_fails_cleanup_verification(tmp_path: Path) -> None:
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_mock_cleanup_commands(stop_rc=0, ps_rc=0, ps_stdout="", vol_rc=0, vol_stdout="certbound-sim-int-02-abc_db-data\n"),
    ):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.remaining_volumes == ("certbound-sim-int-02-abc_db-data",)
    assert report.cleanup_verified is False


def test_empty_successful_docker_query_output_is_accepted(tmp_path: Path) -> None:
    with mock.patch(
        "ba201_supabase_lab.subprocess.run",
        side_effect=_mock_cleanup_commands(stop_rc=0, ps_rc=0, ps_stdout="", vol_rc=0, vol_stdout=""),
    ):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.cleanup_verified is True
    assert report.remaining_containers == ()
    assert report.remaining_volumes == ()


def test_stop_and_cleanup_stack_never_raises_even_on_subprocess_exception(tmp_path: Path) -> None:
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        report = stop_and_cleanup_stack(tmp_path, project_id="p1", log_path=tmp_path / "lab.log")
    assert report.cleanup_verified is False
    assert report.stop_exit_code is None


# =============================================================================
# SIM-INT-02B: temporary-directory lifecycle -- no directory can exist
# before project-id generation / port allocation succeed, and once created,
# every later failure still removes it.
# =============================================================================


def test_port_allocation_failure_creates_no_temporary_directory(tmp_path: Path) -> None:
    """`LabPorts.allocate()` is called AFTER the Docker-daemon preflight but
    BEFORE `tempfile.mkdtemp(...)` -- a failure there must leave zero trace
    on disk."""
    before = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    with mock.patch("ba201_supabase_lab.check_docker_daemon_available", return_value=None), mock.patch(
        "ba201_supabase_lab.LabPorts.allocate",
        side_effect=Ba201IntegrationLabError("could not allocate 10 free local ports"),
    ):
        with pytest.raises(Ba201IntegrationLabError, match="could not allocate"):
            with disposable_ba201_lab():
                pass  # pragma: no cover - must never be reached
    after = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    assert after == before, "a lab directory was created despite port allocation having failed"


def test_project_id_generation_failure_creates_no_temporary_directory() -> None:
    """Symmetric with the port-allocation case above: `generate_unique_project_id()`
    runs even earlier in the orchestration order, so a failure there must
    ALSO leave zero trace on disk."""
    before = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    with mock.patch("ba201_supabase_lab.check_docker_daemon_available", return_value=None), mock.patch(
        "ba201_supabase_lab.generate_unique_project_id",
        side_effect=RuntimeError("unexpected id-generation failure"),
    ):
        with pytest.raises(RuntimeError, match="unexpected id-generation failure"):
            with disposable_ba201_lab():
                pass  # pragma: no cover - must never be reached
    after = {p.name for p in Path(tempfile.gettempdir()).glob("ba201_supabase_lab_*")}
    assert after == before, "a lab directory was created despite project-id generation having failed"


def test_failure_immediately_after_mkdtemp_still_removes_directory() -> None:
    """Once `tempfile.mkdtemp(...)` itself has succeeded, EVERY later
    failure -- even the very first thing attempted afterward
    (`init_disposable_project(...)`) -- must still remove that directory."""
    created_dirs = []
    real_mkdtemp = tempfile.mkdtemp

    def spying_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created_dirs.append(Path(path))
        return path

    with mock.patch("ba201_supabase_lab.check_docker_daemon_available", return_value=None), mock.patch(
        "ba201_supabase_lab.tempfile.mkdtemp", side_effect=spying_mkdtemp
    ), mock.patch(
        "ba201_supabase_lab.init_disposable_project",
        side_effect=Ba201IntegrationLabError("init failed immediately"),
    ):
        with pytest.raises(Ba201IntegrationLabError, match="init failed immediately"):
            with disposable_ba201_lab():
                pass  # pragma: no cover - setup fails before yield

    assert len(created_dirs) == 1
    assert not created_dirs[0].exists(), "temporary lab directory survived a failure immediately after mkdtemp"
    report = get_last_cleanup_report()
    assert report is not None
    # `supabase start` was never even attempted (init itself failed first) --
    # this is treated as "nothing to clean up", but the directory removal
    # itself is unconditional either way.
    assert report.lab_dir_removed is True


# =============================================================================
# SIM-INT-02C: temporary-directory LOCATION guard -- an in-repository
# temporary directory (e.g. a misconfigured OS `TMPDIR`/`TEMP`) must be
# rejected immediately after `mkdtemp(...)`, BEFORE `supabase init` (or any
# other Supabase/Docker command) ever touches it, and must still be removed.
#
# Uses an entirely FAKE, mocked "repository root" created under the real OS
# temporary directory -- never the actual project repository -- so these
# tests can never write into, or depend on, this repository's own layout.
# =============================================================================


def test_temp_directory_inside_repo_root_is_rejected_before_any_supabase_command() -> None:
    fake_repo_root = Path(tempfile.mkdtemp(prefix="sim_int_02c_fake_repo_root_"))
    try:
        inside_repo_child = Path(tempfile.mkdtemp(dir=str(fake_repo_root), prefix="ba201_supabase_lab_"))

        def fake_mkdtemp(*args, **kwargs):
            return str(inside_repo_child)

        with mock.patch("ba201_supabase_lab.REPO_ROOT", fake_repo_root), mock.patch(
            "ba201_supabase_lab.check_docker_daemon_available", return_value=None
        ), mock.patch("ba201_supabase_lab.tempfile.mkdtemp", side_effect=fake_mkdtemp), mock.patch(
            "ba201_supabase_lab.init_disposable_project"
        ) as mocked_init, mock.patch("ba201_supabase_lab.start_disposable_stack") as mocked_start:
            with pytest.raises(Ba201IntegrationLabError, match="repository"):
                with disposable_ba201_lab():
                    pass  # pragma: no cover - must never be reached
        mocked_init.assert_not_called()
        mocked_start.assert_not_called()
        assert not inside_repo_child.exists(), "the rejected in-repo temporary directory was not removed"
    finally:
        shutil.rmtree(fake_repo_root, ignore_errors=True)


def test_temp_directory_equal_to_repo_root_is_rejected_before_any_supabase_command() -> None:
    fake_repo_root = Path(tempfile.mkdtemp(prefix="sim_int_02c_fake_repo_root_equal_"))
    try:

        def fake_mkdtemp(*args, **kwargs):
            return str(fake_repo_root)

        with mock.patch("ba201_supabase_lab.REPO_ROOT", fake_repo_root), mock.patch(
            "ba201_supabase_lab.check_docker_daemon_available", return_value=None
        ), mock.patch("ba201_supabase_lab.tempfile.mkdtemp", side_effect=fake_mkdtemp), mock.patch(
            "ba201_supabase_lab.init_disposable_project"
        ) as mocked_init, mock.patch("ba201_supabase_lab.start_disposable_stack") as mocked_start:
            with pytest.raises(Ba201IntegrationLabError, match="repository"):
                with disposable_ba201_lab():
                    pass  # pragma: no cover - must never be reached
        mocked_init.assert_not_called()
        mocked_start.assert_not_called()
    finally:
        shutil.rmtree(fake_repo_root, ignore_errors=True)


def test_temp_directory_outside_fake_repo_root_is_accepted_by_the_location_guard() -> None:
    """Sanity/negative control for the two tests above: a lab directory that
    is a SIBLING of (not inside/equal to) the fake repo root must NOT be
    rejected by `_require_lab_dir_outside_repo(...)` itself -- proving the
    guard is discriminating on actual containment, not merely always
    failing. (`init_disposable_project` is still mocked to fail for an
    unrelated, deliberately distinct reason, so this test does not attempt
    any real Supabase/Docker command.)"""
    fake_repo_root = Path(tempfile.mkdtemp(prefix="sim_int_02c_fake_repo_root_sibling_"))
    sibling_lab_dir = Path(tempfile.mkdtemp(prefix="sim_int_02c_sibling_lab_dir_"))
    try:

        def fake_mkdtemp(*args, **kwargs):
            return str(sibling_lab_dir)

        with mock.patch("ba201_supabase_lab.REPO_ROOT", fake_repo_root), mock.patch(
            "ba201_supabase_lab.check_docker_daemon_available", return_value=None
        ), mock.patch("ba201_supabase_lab.tempfile.mkdtemp", side_effect=fake_mkdtemp), mock.patch(
            "ba201_supabase_lab.init_disposable_project",
            side_effect=Ba201IntegrationLabError("distinct-marker: reached init, guard did not block a valid sibling dir"),
        ):
            with pytest.raises(Ba201IntegrationLabError, match="distinct-marker"):
                with disposable_ba201_lab():
                    pass  # pragma: no cover - init mock raises before yield
    finally:
        shutil.rmtree(fake_repo_root, ignore_errors=True)
        shutil.rmtree(sibling_lab_dir, ignore_errors=True)


# =============================================================================
# SIM-INT-02C: optional defense-in-depth -- a `psycopg2.connect(...)`
# failure must never expose the (potentially password-bearing) DB URL it
# was given.
# =============================================================================


def test_migration_apply_connection_failure_never_leaks_db_url_or_password() -> None:
    secret_bearing_db_url = _FAKE_DB_URL

    def fake_connect(dsn, *args, **kwargs):
        # Models a real `psycopg2` connection-time exception whose OWN
        # message embeds the raw DSN -- including the password -- verbatim.
        raise RuntimeError(f"could not connect using dsn={dsn}")

    with mock.patch("ba201_supabase_lab.psycopg2.connect", side_effect=fake_connect):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            ba201_supabase_lab.apply_migration_files(secret_bearing_db_url, [("some/migration.sql", "SELECT 1;")])
    _assert_no_secrets(str(excinfo.value))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)


def test_verify_schema_connection_failure_never_leaks_db_url_or_password() -> None:
    secret_bearing_db_url = _FAKE_DB_URL

    def fake_connect(dsn, *args, **kwargs):
        raise RuntimeError(f"could not connect using dsn={dsn}")

    with mock.patch("ba201_supabase_lab.psycopg2.connect", side_effect=fake_connect):
        with pytest.raises(Ba201IntegrationLabError) as excinfo:
            ba201_supabase_lab.verify_schema_and_grants(secret_bearing_db_url)
    _assert_no_secrets(str(excinfo.value))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    _assert_no_secrets_in_exception_chain(excinfo.value)


def test_connect_to_lab_database_is_public_and_wraps_psycopg2_connect() -> None:
    """SIM-INT-02D: `connect_to_lab_database(...)` is the ONE sanitized
    connection helper -- exposed under its PUBLIC name (no leading
    underscore) precisely so the live integration test can import and call
    it directly instead of ever calling `psycopg2.connect(...)` itself."""
    sentinel_connection = object()

    with mock.patch("ba201_supabase_lab.psycopg2.connect", return_value=sentinel_connection) as fake_connect:
        result = connect_to_lab_database("postgresql://postgres:pw@127.0.0.1:54399/postgres")
    assert result is sentinel_connection
    fake_connect.assert_called_once_with("postgresql://postgres:pw@127.0.0.1:54399/postgres")


def test_connect_to_lab_database_success_behaves_unchanged_for_downstream_queries() -> None:
    """A successful connection must behave completely unchanged -- a
    subsequent cursor/query error that carries NO credential must still
    surface normally (this wrapper only ever intercepts CONNECTION-time
    failures, never query-time ones)."""

    class _FakeCursor:
        def execute(self, *args, **kwargs):
            raise RuntimeError("relation \"does_not_exist\" does not exist")

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeConnection:
        def cursor(self):
            return _FakeCursor()

        def close(self):
            pass

    fake_connection = _FakeConnection()
    with mock.patch("ba201_supabase_lab.psycopg2.connect", return_value=fake_connection):
        conn = connect_to_lab_database("postgresql://postgres:pw@127.0.0.1:54399/postgres")
    assert conn is fake_connection
    with pytest.raises(RuntimeError, match="does_not_exist"):
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM does_not_exist")


def test_live_test_module_never_calls_psycopg2_connect_directly_on_stack_db_url() -> None:
    """SIM-INT-02D: pure, mocked proof that the live integration test's own
    source no longer contains (or invokes) a direct psycopg2 connection call
    against the disposable stack's own database URL -- every database
    connection it makes must go through the sanitized
    ``connect_to_lab_database(...)`` helper instead. Reads THIS module's own
    source text rather than exercising the (Docker-gated) live test itself.
    (The forbidden literal is assembled from parts here so that describing
    it in THIS docstring can never itself trip the very assertion below.)"""
    this_module_source = Path(__file__).read_text(encoding="utf-8")
    forbidden_direct_call = "psycopg2" + ".connect(lab.stack.db_url)"
    required_sanitized_call = "connect_to_lab_database(lab.stack.db_url)"
    assert forbidden_direct_call not in this_module_source
    assert required_sanitized_call in this_module_source


# =============================================================================
# 12. Temporary directory is removed even when cleanup raises unexpectedly.
# =============================================================================


def test_temp_directory_removed_even_when_stop_and_cleanup_stack_raises() -> None:
    with mock.patch("ba201_supabase_lab.check_docker_daemon_available", return_value=None), mock.patch(
        "ba201_supabase_lab.init_disposable_project", return_value=None
    ), mock.patch("ba201_supabase_lab.start_disposable_stack", side_effect=Ba201IntegrationLabError("start failed")), mock.patch(
        "ba201_supabase_lab.stop_and_cleanup_stack", side_effect=RuntimeError("unexpected bug in cleanup")
    ):
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp

        def spying_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created_dirs.append(Path(path))
            return path

        with mock.patch("ba201_supabase_lab.tempfile.mkdtemp", side_effect=spying_mkdtemp):
            with pytest.raises(Ba201IntegrationLabError, match="start failed"):
                with disposable_ba201_lab():
                    pass  # pragma: no cover - setup fails before yield

        assert len(created_dirs) == 1
        assert not created_dirs[0].exists(), "temporary lab directory survived a cleanup-time exception"
        report = get_last_cleanup_report()
        assert report is not None
        assert report.cleanup_verified is False
        assert report.lab_dir_removed is True


# =============================================================================
# SIM-INT-02D: cleanup truthfulness -- `cleanup_verified` must incorporate
# ACTUAL temporary-directory removal, not just the container/volume
# residue queries. These tests exercise the REAL `disposable_ba201_lab()`
# context manager end-to-end (a real `tempfile.mkdtemp(...)` directory is
# created), with every Docker/Supabase-touching step mocked out from
# `check_docker_daemon_available` through `stop_and_cleanup_stack`, and
# `shutil.rmtree(...)` itself mocked to a no-op -- so the directory
# deliberately survives, exactly modeling a failed removal (e.g. a locked
# file, a stray open handle) without needing a real failing filesystem.
# =============================================================================


def _fake_stack_for_cleanup_tests() -> Any:
    return ba201_supabase_lab.LocalSupabaseStack(
        project_id="sim-int-02d-fake-project",
        lab_dir=Path("unused-placeholder"),
        ports=_fake_ports(),
        api_url="http://127.0.0.1:54321",
        db_url="postgresql://postgres:pw@127.0.0.1:54322/postgres",
        service_role_key="fake-service-role-key",
        log_path=Path("unused-placeholder/lab.log"),
    )


@contextlib.contextmanager
def _fully_mocked_successful_lab_setup(created_dirs: list) -> Any:
    """Patches every step `disposable_ba201_lab(...)` needs in order to
    reach a successful `yield lab`, AND makes `shutil.rmtree(...)` itself a
    no-op -- used ONLY by the cleanup-truthfulness tests below, which need
    the REAL context manager (not mocked away like `_make_lab_cm(...)`
    above) to actually exercise its own post-`shutil.rmtree(...)`
    `lab_dir.exists()` computation. Appends the ONE real temporary
    directory this creates to `created_dirs`, so the caller can remove it
    (with the REAL, unmocked `shutil.rmtree`) once the test itself is done."""
    fake_stack = _fake_stack_for_cleanup_tests()

    def fake_stop_and_cleanup_stack(lab_dir: Path, *, project_id: str, log_path: Optional[Path]) -> CleanupReport:
        # `disposable_ba201_lab(...)` only ever recomputes the FINAL report
        # for a `stop_and_cleanup_stack(...)` result whose `project_id`
        # matches its own real, dynamically-generated one -- a fixed fake
        # `project_id` here would silently make that reconciliation a
        # no-op, masking exactly the bug this test suite exists to catch.
        return CleanupReport(
            project_id=project_id,
            stop_exit_code=0,
            container_query_exit_code=0,
            volume_query_exit_code=0,
            remaining_containers=(),
            remaining_volumes=(),
            lab_dir_removed=False,  # placeholder -- filled in by disposable_ba201_lab's own outer finally.
            cleanup_verified=True,
            note=None,
        )

    real_mkdtemp = tempfile.mkdtemp

    def spying_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created_dirs.append(Path(path))
        return path

    with contextlib.ExitStack() as patches:
        patches.enter_context(mock.patch("ba201_supabase_lab.check_docker_daemon_available", return_value=None))
        patches.enter_context(mock.patch("ba201_supabase_lab.tempfile.mkdtemp", side_effect=spying_mkdtemp))
        patches.enter_context(mock.patch("ba201_supabase_lab.init_disposable_project", return_value=None))
        patches.enter_context(mock.patch("ba201_supabase_lab.start_disposable_stack", return_value=fake_stack))
        patches.enter_context(mock.patch("ba201_supabase_lab.enforce_production_isolation_guards", return_value=None))
        patches.enter_context(mock.patch("ba201_supabase_lab.read_migration_files", return_value=[]))
        patches.enter_context(mock.patch("ba201_supabase_lab.apply_migration_files", return_value=[]))
        patches.enter_context(mock.patch("ba201_supabase_lab.verify_schema_and_grants", return_value={}))
        patches.enter_context(mock.patch("supabase.create_client", return_value=mock.MagicMock()))
        patches.enter_context(
            mock.patch("ba201_supabase_lab.stop_and_cleanup_stack", side_effect=fake_stop_and_cleanup_stack)
        )
        patches.enter_context(mock.patch("ba201_supabase_lab.shutil.rmtree", return_value=None))
        yield


def test_directory_removal_failure_alone_marks_cleanup_unverified_with_successful_body() -> None:
    """Core SIM-INT-02D fix: container/volume cleanup succeeds, but
    `shutil.rmtree(...)` itself fails to actually remove the directory --
    the FINAL `cleanup_verified` must become `False`, `lab_dir_removed`
    must be `False`, and the sanitized note must mention temporary-
    directory removal WITHOUT ever including the directory's own path."""
    created_dirs: list = []
    try:
        with _fully_mocked_successful_lab_setup(created_dirs):
            with disposable_ba201_lab() as lab:
                assert lab is not None

        assert len(created_dirs) == 1
        report = get_last_cleanup_report()
        assert report is not None
        assert report.lab_dir_removed is False
        assert report.cleanup_verified is False
        assert report.note is not None
        assert "directory" in report.note.lower()
        assert str(created_dirs[0]) not in report.note
        assert created_dirs[0].exists(), "the mocked no-op shutil.rmtree should have left the directory in place"
    finally:
        for created_dir in created_dirs:
            shutil.rmtree(created_dir, ignore_errors=True)


def test_wrapper_fails_when_body_succeeds_but_directory_removal_failed() -> None:
    """`run_disposable_lab_body_with_cleanup_verification(...)`: the body
    itself succeeds, but directory removal alone failed -> must still raise
    `Ba201IntegrationLabError` (so the live test fails), with a sanitized
    note that never contains the directory's own path."""
    created_dirs: list = []
    try:
        with _fully_mocked_successful_lab_setup(created_dirs):
            with pytest.raises(Ba201IntegrationLabError, match="cleanup could not be verified") as excinfo:
                run_disposable_lab_body_with_cleanup_verification(lambda lab: None)
        assert len(created_dirs) == 1
        assert "directory" in str(excinfo.value).lower()
        assert str(created_dirs[0]) not in str(excinfo.value)
        _assert_no_secrets(str(excinfo.value))
    finally:
        for created_dir in created_dirs:
            shutil.rmtree(created_dir, ignore_errors=True)


def test_wrapper_preserves_primary_exception_when_body_and_directory_removal_both_fail() -> None:
    """`run_disposable_lab_body_with_cleanup_verification(...)`: when the
    body ALSO raises its own (primary, distinct) exception AND directory
    removal separately failed, the PRIMARY exception must be re-raised
    UNCHANGED (never replaced/masked by the cleanup failure), with a
    sanitized cleanup note attached via `add_note(...)` -- never a path."""

    class _DistinctPrimaryBodyError(RuntimeError):
        pass

    def failing_body(lab: Ba201Lab) -> None:
        raise _DistinctPrimaryBodyError("distinct-marker: primary body assertion failure")

    created_dirs: list = []
    try:
        with _fully_mocked_successful_lab_setup(created_dirs):
            with pytest.raises(_DistinctPrimaryBodyError, match="distinct-marker") as excinfo:
                run_disposable_lab_body_with_cleanup_verification(failing_body)
        assert len(created_dirs) == 1
        notes = getattr(excinfo.value, "__notes__", [])
        assert any("directory" in note.lower() for note in notes), (
            "expected a sanitized directory-removal diagnostic note to be attached to the primary exception"
        )
        assert not any(str(created_dirs[0]) in note for note in notes)
        _assert_no_secrets(*notes)
    finally:
        for created_dir in created_dirs:
            shutil.rmtree(created_dir, ignore_errors=True)


# =============================================================================
# 13 & 14. `run_disposable_lab_body_with_cleanup_verification(...)` semantics.
# =============================================================================


def _fake_lab() -> Ba201Lab:
    return Ba201Lab(stack=mock.MagicMock(), real_client=mock.MagicMock(), lab_dir=Path("."), log_path=Path("lab.log"))


def _make_lab_cm(report: CleanupReport):
    """A stand-in for `disposable_ba201_lab()` used ONLY by the pure
    `run_disposable_lab_body_with_cleanup_verification(...)` tests below: it
    yields a fake `Ba201Lab`, then -- exactly like the real context manager's
    `finally` block -- stashes `report` via `_set_last_cleanup_report(...)`
    regardless of whether the body raised."""

    @contextlib.contextmanager
    def _cm():
        try:
            yield _fake_lab()
        finally:
            ba201_supabase_lab._set_last_cleanup_report(report)

    return _cm()


_VERIFIED_REPORT = CleanupReport(
    project_id="p1",
    stop_exit_code=0,
    container_query_exit_code=0,
    volume_query_exit_code=0,
    remaining_containers=(),
    remaining_volumes=(),
    lab_dir_removed=True,
    cleanup_verified=True,
)

_UNVERIFIED_REPORT = CleanupReport(
    project_id="p1",
    stop_exit_code=1,
    container_query_exit_code=0,
    volume_query_exit_code=0,
    remaining_containers=(),
    remaining_volumes=(),
    lab_dir_removed=True,
    cleanup_verified=False,
    note="stop_exit=1 container_query_exit=0 volume_query_exit=0 remaining_containers=0 remaining_volumes=0",
)


def test_successful_body_with_verified_cleanup_raises_nothing() -> None:
    with mock.patch("ba201_supabase_lab.disposable_ba201_lab", side_effect=lambda: _make_lab_cm(_VERIFIED_REPORT)):
        run_disposable_lab_body_with_cleanup_verification(lambda lab: None)


def test_successful_body_with_unverified_cleanup_raises() -> None:
    with mock.patch("ba201_supabase_lab.disposable_ba201_lab", side_effect=lambda: _make_lab_cm(_UNVERIFIED_REPORT)):
        with pytest.raises(Ba201IntegrationLabError, match="cleanup could not be verified"):
            run_disposable_lab_body_with_cleanup_verification(lambda lab: None)


def test_failed_body_with_verified_cleanup_preserves_original_exception() -> None:
    def failing_body(lab: Ba201Lab) -> None:
        raise ValueError("primary assertion failure")

    with mock.patch("ba201_supabase_lab.disposable_ba201_lab", side_effect=lambda: _make_lab_cm(_VERIFIED_REPORT)):
        with pytest.raises(ValueError, match="primary assertion failure"):
            run_disposable_lab_body_with_cleanup_verification(failing_body)


def test_failed_body_with_unverified_cleanup_preserves_primary_failure_and_attaches_note() -> None:
    def failing_body(lab: Ba201Lab) -> None:
        raise ValueError("primary assertion failure")

    with mock.patch("ba201_supabase_lab.disposable_ba201_lab", side_effect=lambda: _make_lab_cm(_UNVERIFIED_REPORT)):
        with pytest.raises(ValueError, match="primary assertion failure") as excinfo:
            run_disposable_lab_body_with_cleanup_verification(failing_body)
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("cleanup could not be verified" in note for note in notes), (
        "expected a sanitized secondary cleanup diagnostic note to be attached to the primary exception"
    )
    _assert_no_secrets(*notes)


# =============================================================================
# 15. The live test's own wrapper reaches post-context cleanup assertions
#     under fully mocked, successful orchestration.
# =============================================================================


def test_live_test_wrapper_reaches_cleanup_assertions_under_mocked_success() -> None:
    body_calls = []

    def trivial_body(lab: Ba201Lab) -> None:
        body_calls.append(lab)

    with mock.patch("ba201_supabase_lab.disposable_ba201_lab", side_effect=lambda: _make_lab_cm(_VERIFIED_REPORT)):
        run_disposable_lab_body_with_cleanup_verification(trivial_body)

    assert len(body_calls) == 1
    report = get_last_cleanup_report()
    assert report is not None
    assert report.cleanup_verified is True
    assert report.stop_exit_code == 0
    assert report.container_query_exit_code == 0
    assert report.volume_query_exit_code == 0
    assert report.remaining_containers == ()
    assert report.remaining_volumes == ()
    assert report.lab_dir_removed is True


# =============================================================================
# 16. Only one Supabase start is attempted (no trimmed/fallback retry).
# =============================================================================


def test_only_one_supabase_start_is_attempted(tmp_path: Path) -> None:
    start_calls = []

    def fake_run(args, **kwargs):
        if args[:2] == ["supabase", "start"]:
            start_calls.append(args)
            return _completed_process(args, returncode=1, stdout="", stderr="boom")
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        with pytest.raises(Ba201IntegrationLabError):
            start_disposable_stack(
                tmp_path,
                project_id="certbound-sim-int-02a-test",
                ports=_fake_ports(),
                log_path=tmp_path / "lab.log",
            )
    assert len(start_calls) == 1, "start_disposable_stack must attempt exactly one `supabase start` call"


# =============================================================================
# 17. No raw command output is ever placed in any persisted log, across a
#     representative mix of successful and failing commands.
# =============================================================================


def test_no_raw_command_output_in_persisted_log_across_success_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    log_path = tmp_path / "lab.log"

    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "context", "show"]:
            return _completed_process(args, returncode=0, stdout=f"default-containing-{_FAKE_ANON_KEY}\n", stderr="")
        if args[:3] == ["docker", "context", "inspect"]:
            return _completed_process(args, returncode=0, stdout="unix:///var/run/docker.sock\n", stderr="")
        if args[:2] == ["docker", "version"]:
            return _completed_process(args, returncode=0, stdout=f"Docker info containing {_FAKE_DB_PASSWORD}", stderr="")
        if args[:2] == ["supabase", "init"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        if args[:2] == ["supabase", "start"]:
            return _completed_process(args, returncode=1, stdout="", stderr=f"failed near {_FAKE_SERVICE_ROLE_KEY}")
        if args[:2] == ["supabase", "stop"]:
            return _completed_process(args, returncode=0, stdout=f"stopped {_FAKE_ANON_KEY}", stderr="")
        if args[:2] == ["docker", "ps"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        if args[:2] == ["docker", "volume"]:
            return _completed_process(args, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    with mock.patch("ba201_supabase_lab.subprocess.run", side_effect=fake_run):
        check_docker_daemon_available(log_path=log_path)
        # Exercise the raw command runner directly for the "init" category
        # (rather than the full `init_disposable_project(...)`, which also
        # requires a real generated `config.toml` on disk and is already
        # covered by other tests) -- this test is specifically about what
        # DOES/DOES NOT reach the log file across a representative mix of
        # commands.
        ba201_supabase_lab._run_cli_command(
            ["supabase", "init", "--workdir", str(tmp_path), "--yes"],
            category="supabase.init",
            log_path=log_path,
            timeout=60,
        )
        with pytest.raises(Ba201IntegrationLabError):
            start_disposable_stack(
                tmp_path,
                project_id="certbound-sim-int-02a-test",
                ports=_fake_ports(),
                log_path=log_path,
            )
        stop_and_cleanup_stack(tmp_path, project_id="certbound-sim-int-02a-test", log_path=log_path)

    log_text = log_path.read_text(encoding="utf-8")
    _assert_no_secrets(log_text)
    assert _FAKE_ANON_KEY not in log_text
    for category in (
        "docker.context_show",
        "docker.context_inspect",
        "docker.daemon_check",
        "supabase.init",
        "supabase.start",
        "supabase.stop",
        "docker.ps",
        "docker.volume_ls",
    ):
        assert f"category={category}" in log_text


# =============================================================================
# The one real, opt-in Supabase/Docker integration test.
# =============================================================================


@pytest.mark.skipif(
    not is_local_supabase_integration_enabled(),
    reason=(
        "Set CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION=1 to run the real disposable "
        "local Supabase integration lab (requires a running Docker daemon)."
    ),
)
def test_full_ba201_controller_flow_against_real_local_supabase() -> None:
    # Imported lazily, inside the gated test, so a normal pytest run of this
    # file never even imports utils.scenario_learner_controller.
    from utils.scenario_catalog import resolve_default_scenario_version_path
    from utils.scenario_engine import ENGINE_VERSION
    from utils.scenario_learner_controller import (
        BA201_CERTIFICATION_EXAM_NAME,
        BA201_SIMULATION_ID,
        ScenarioLearnerBackendError,
        load_ba201_completion_result,
        prepare_ba201_decision,
        start_or_resume_ba201_attempt,
        submit_prepared_ba201_decision,
    )
    from utils.scenario_persistence import get_attempt
    from utils.scenario_schema import compute_canonical_content_sha256, load_json_document, load_scenario_content

    learner_email = f"sim-int-02-{uuid.uuid4().hex[:12]}@example.test"
    repo_root = Path(__file__).resolve().parents[2]

    def body(lab: Ba201Lab) -> None:
        client = lab.real_client

        # ---- V66/V67/V68 installation results ----
        assert lab.migrations_applied == [
            "supabase/migrations/20260718170000_v66_scenario_definition_persistence_foundation.sql",
            "supabase/migrations/20260719003000_v67_harden_scenario_definition_security.sql",
            "supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql",
        ]
        assert all(lab.schema_report["tables"].values()), lab.schema_report["tables"]
        assert all(lab.schema_report["functions"].values()), lab.schema_report["functions"]
        for role in ("anon", "authenticated"):
            for fn, allowed in lab.schema_report["grants"][role].items():
                assert allowed is False, f"{role} must NOT be able to execute {fn}"
        for fn, allowed in lab.schema_report["grants"]["service_role"].items():
            assert allowed is True, f"service_role must be able to execute {fn}"

        # ---- BA-201 seed ----
        default_path = resolve_default_scenario_version_path(
            certification_exam_name=BA201_CERTIFICATION_EXAM_NAME,
            simulation_id=BA201_SIMULATION_ID,
        )
        content = load_scenario_content(default_path)
        raw_document = load_json_document(default_path)
        assert content.canonical_content_sha256 == compute_canonical_content_sha256(raw_document)

        seed = seed_ba201_scenario(
            client,
            certification_exam_name=content.certification_exam_name,
            simulation_id=content.simulation_id,
            title=content.title,
            version=content.version,
            schema_version=content.schema_version,
            engine_version=ENGINE_VERSION,
            canonical_content_sha256=content.canonical_content_sha256,
            content_snapshot=raw_document,
            source_repository_path=str(default_path.relative_to(repo_root)),
        )
        assert seed.became_current is True
        assert seed.current_published_version_id == seed.scenario_version_id
        assert seed.canonical_content_sha256 == content.canonical_content_sha256
        assert seed.engine_version == ENGINE_VERSION

        # ---- SIM-INT-02B: database seed-row proof (first published version) ----
        # `seed` above merely echoes back several of ITS OWN input parameters
        # -- it is not proof of what PostgREST/Postgres actually persisted.
        # Re-fetch the real row directly, through the real client, and
        # assert against those ACTUAL stored values instead.
        seeded_version_row = fetch_scenario_version_row(client, scenario_version_id=seed.scenario_version_id)
        assert seeded_version_row["id"] == seed.scenario_version_id
        assert seeded_version_row["scenario_id"] == seed.scenario_id
        assert seeded_version_row["version"] == content.version
        assert seeded_version_row["lifecycle_status"] == "published"
        assert seeded_version_row["engine_version"] == ENGINE_VERSION
        assert seeded_version_row["canonical_content_sha256"] == content.canonical_content_sha256

        seeded_scenario_row = fetch_scenario_row(client, scenario_id=seed.scenario_id)
        assert seeded_scenario_row["current_published_version_id"] == seeded_version_row["id"]

        seeded_snapshot = seeded_version_row.get("content_snapshot")
        assert seeded_snapshot is not None
        assert seeded_snapshot.get("simulationId") == content.simulation_id
        assert seeded_snapshot.get("version") == content.version

        # ---- start_or_resume: create then resume, no second active attempt ----
        first_view = start_or_resume_ba201_attempt(learner_email, client=client)
        assert first_view.is_new_attempt is True
        assert first_view.is_complete is False
        first_attempt_id = first_view.attempt_id

        second_view = start_or_resume_ba201_attempt(learner_email, client=client)
        assert second_view.is_new_attempt is False
        assert second_view.attempt_id == first_attempt_id

        conn = connect_to_lab_database(lab.stack.db_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.scenario_attempts WHERE user_email = %s",
                    (learner_email.strip().lower(),),
                )
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()

        # ---- lost-response simulation on the first nonterminal decision ----
        assert first_view.current_scene is not None
        chosen_option = first_view.current_scene.options[0]
        idempotency_key = str(uuid.uuid4())

        prepared = prepare_ba201_decision(
            learner_email,
            attempt_id=first_attempt_id,
            selected_option_id=chosen_option.option_id,
            idempotency_key=idempotency_key,
            client=client,
        )

        # ---- SIM-INT-02B: retain a value-equivalent, independently-owned
        # snapshot of `prepared` BEFORE it is ever handed to the lost-response
        # proxy -- `PreparedScenarioDecision` is a frozen dataclass of plain
        # scalars/immutable JSON strings, so nothing here CAN mutate it, but
        # this snapshot lets the assertions below PROVE that directly rather
        # than merely assume it.
        prepared_before_proxy_call = dataclasses.replace(prepared)
        state_before_snapshot = prepared.reconstruct_state_before()
        state_after_snapshot = prepared.reconstruct_state_after()

        proxy_client = CommitThenRaiseProxyClient(client, target_idempotency_key=idempotency_key)
        with pytest.raises(ScenarioLearnerBackendError):
            submit_prepared_ba201_decision(learner_email, prepared, client=proxy_client)
        assert proxy_client.captured_committed_response is not None

        # ---- prove `prepared` itself is still value-identical to its
        # pre-call snapshot after the proxy raised the uncertain backend error ----
        assert prepared == prepared_before_proxy_call
        assert prepared.idempotency_key == prepared_before_proxy_call.idempotency_key
        assert prepared.reconstruct_state_before() == state_before_snapshot
        assert prepared.reconstruct_state_after() == state_after_snapshot

        conn = connect_to_lab_database(lab.stack.db_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.scenario_decisions WHERE attempt_id = %s AND idempotency_key = %s",
                    (first_attempt_id, idempotency_key),
                )
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()

        # ---- retry with the ORIGINAL client and the SAME prepared object ----
        retry_outcome = submit_prepared_ba201_decision(learner_email, prepared, client=client)
        assert retry_outcome.idempotent_replay is True
        assert retry_outcome.attempt_id == first_attempt_id

        conn = connect_to_lab_database(lab.stack.db_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM public.scenario_decisions WHERE attempt_id = %s AND idempotency_key = %s",
                    (first_attempt_id, idempotency_key),
                )
                assert cur.fetchone()[0] == 1
                cur.execute("SELECT next_sequence_number FROM public.scenario_attempts WHERE id = %s", (first_attempt_id,))
                assert cur.fetchone()[0] == 2
        finally:
            conn.close()

        # ---- SIM-INT-02B: prove the RETRY's persisted state exactly matches
        # what `prepared` itself already committed to, via the EXISTING
        # persistence adapter (`get_attempt`) -- never raw SQL for these
        # value-equality checks. ----
        authoritative_attempt_after_retry = get_attempt(client, user_email=learner_email, attempt_id=first_attempt_id)
        assert authoritative_attempt_after_retry.next_sequence_number == prepared.expected_sequence_number + 1
        assert authoritative_attempt_after_retry.serialized_engine_state == prepared.reconstruct_state_after()
        if prepared.is_terminal:
            assert authoritative_attempt_after_retry.status == "completed"
            assert authoritative_attempt_after_retry.current_scene_id is None
        else:
            assert authoritative_attempt_after_retry.status == "in_progress"
            assert authoritative_attempt_after_retry.current_scene_id == prepared.resulting_scene_id

        # `get_scenario_attempt_v1` deliberately EXCLUDES idempotency_key
        # from its own returned `decisions` array (see V68's own migration
        # comment) -- `sequence_number` is the adapter-visible key that
        # uniquely identifies THIS exact decision instead.
        adapter_visible_decisions = [
            entry
            for entry in authoritative_attempt_after_retry.decisions
            if entry.get("sequenceNumber") == prepared.expected_sequence_number
        ]
        assert len(adapter_visible_decisions) == 1
        persisted_decision_payload = adapter_visible_decisions[0]
        assert persisted_decision_payload["selectedOptionId"] == prepared.selected_option_id
        assert persisted_decision_payload["expectedSceneId"] == prepared.expected_scene_id
        assert persisted_decision_payload["resultingSceneId"] == prepared.resulting_scene_id
        assert persisted_decision_payload["isTerminal"] == prepared.is_terminal
        assert persisted_decision_payload["stateBefore"] == prepared.reconstruct_state_before()
        assert persisted_decision_payload["stateAfter"] == prepared.reconstruct_state_after()

        # ---- continue deterministically until terminal ----
        current_view = start_or_resume_ba201_attempt(learner_email, client=client)
        guard = 0
        step_prepared = prepared
        while not current_view.is_complete:
            guard += 1
            assert guard <= 50
            assert current_view.current_scene is not None
            option = current_view.current_scene.options[0]
            key = str(uuid.uuid4())
            step_prepared = prepare_ba201_decision(
                learner_email,
                attempt_id=first_attempt_id,
                selected_option_id=option.option_id,
                idempotency_key=key,
                client=client,
            )
            outcome = submit_prepared_ba201_decision(learner_email, step_prepared, client=client)
            assert outcome.idempotent_replay is False
            if outcome.is_complete:
                break
            current_view = start_or_resume_ba201_attempt(learner_email, client=client)

        completed_attempt = get_attempt(client, user_email=learner_email, attempt_id=first_attempt_id)
        assert completed_attempt.status == "completed"
        assert completed_attempt.current_scene_id is None
        assert completed_attempt.terminal_ending_id
        assert completed_attempt.terminal_result_snapshot is not None
        assert completed_attempt.next_sequence_number == len(completed_attempt.decisions) + 1

        terminal_retry_outcome = submit_prepared_ba201_decision(learner_email, step_prepared, client=client)
        assert terminal_retry_outcome.idempotent_replay is True
        conn = connect_to_lab_database(lab.stack.db_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.scenario_decisions WHERE attempt_id = %s", (first_attempt_id,))
                assert cur.fetchone()[0] == len(completed_attempt.decisions)
        finally:
            conn.close()

        # ---- completion result ----
        result_view = load_ba201_completion_result(learner_email, attempt_id=first_attempt_id, client=client)
        assert result_view.scenario_title == content.title
        assert result_view.certification_exam_name == BA201_CERTIFICATION_EXAM_NAME
        matching_ending = next(e for e in content.endings if e.id == completed_attempt.terminal_ending_id)
        assert result_view.ending_title == matching_ending.score_band
        assert result_view.ending_narrative == matching_ending.narrative
        for forbidden_attr in ("attempt_id", "scenario_version_id", "terminal_result_snapshot"):
            assert not hasattr(result_view, forbidden_attr)

        # ---- historical pointer test ----
        second_version_string = f"{content.version}-sim-int-02-{uuid.uuid4().hex[:8]}"
        modified_document = copy.deepcopy(dict(raw_document))
        modified_document["version"] = second_version_string

        with tempfile.TemporaryDirectory(prefix="ba201_second_version_") as tmp_dir:
            temp_path = Path(tmp_dir) / "second_version.json"
            temp_path.write_text(json.dumps(modified_document), encoding="utf-8")
            second_content = load_scenario_content(temp_path)
            second_raw_document = load_json_document(temp_path)

        second_seed = publish_second_version(
            client,
            scenario_id=seed.scenario_id,
            version=second_version_string,
            schema_version=second_content.schema_version,
            engine_version=ENGINE_VERSION,
            canonical_content_sha256=second_content.canonical_content_sha256,
            content_snapshot=second_raw_document,
            source_repository_path=f"integration-lab-synthetic-second-version:{second_version_string}",
        )
        assert second_seed.became_current is True
        assert second_seed.current_published_version_id == second_seed.scenario_version_id
        assert second_seed.current_published_version_id != seed.scenario_version_id

        # ---- SIM-INT-02B: database seed-row proof (second published version) ----
        # Same actual-row verification as the first version above -- never
        # satisfied via `second_seed`'s own echoed input fields.
        second_version_row = fetch_scenario_version_row(client, scenario_version_id=second_seed.scenario_version_id)
        assert second_version_row["id"] == second_seed.scenario_version_id
        assert second_version_row["scenario_id"] == seed.scenario_id
        assert second_version_row["version"] == second_version_string
        assert second_version_row["lifecycle_status"] == "published"
        assert second_version_row["engine_version"] == ENGINE_VERSION
        assert second_version_row["canonical_content_sha256"] == second_content.canonical_content_sha256

        second_scenario_row = fetch_scenario_row(client, scenario_id=seed.scenario_id)
        assert second_scenario_row["current_published_version_id"] == second_version_row["id"]
        # The pointer now moved -- confirms this is a REAL, current row read,
        # not a stale/cached value from the first seed's own row fetch above.
        assert second_scenario_row["current_published_version_id"] != seeded_version_row["id"]

        second_snapshot = second_version_row.get("content_snapshot")
        assert second_snapshot is not None
        assert second_snapshot.get("simulationId") == second_content.simulation_id
        assert second_snapshot.get("version") == second_version_string

        historical_result_view = load_ba201_completion_result(learner_email, attempt_id=first_attempt_id, client=client)
        assert historical_result_view == result_view

        historical_attempt = get_attempt(client, user_email=learner_email, attempt_id=first_attempt_id)
        assert historical_attempt.scenario_version_id == seed.scenario_version_id
        assert historical_attempt.scenario_version_id != second_seed.scenario_version_id

    run_disposable_lab_body_with_cleanup_verification(body)

    # SIM-INT-02A: the test cannot pass without these post-context cleanup
    # assertions actually executing -- `run_disposable_lab_body_with_cleanup_
    # verification(...)` already raises if cleanup was not verified, but the
    # assertions below additionally pin down every individual required field
    # so a future regression that weakens that helper is still caught here.
    report = get_last_cleanup_report()
    assert report is not None, "no cleanup report was recorded after the real integration run"
    assert report.cleanup_verified is True
    assert report.stop_exit_code == 0
    assert report.container_query_exit_code == 0
    assert report.volume_query_exit_code == 0
    assert report.remaining_containers == ()
    assert report.remaining_volumes == ()
    assert report.lab_dir_removed is True
