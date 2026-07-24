"""SIM-INT-02: disposable local-Supabase integration lab for the committed
BA-201 learner controller/engine/persistence stack.

This module is deliberately NOT part of the application. It is only ever
imported by ``tests/integration/test_ba201_supabase_integration.py``, and only
does anything when that test is explicitly opted in via
``CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION=1`` (see
``is_local_supabase_integration_enabled()`` below).

Everything this module creates lives under the OS temporary directory,
completely outside this repository:

  * a fresh ``supabase init``-scaffolded project directory
    (``tempfile.mkdtemp(prefix="ba201_supabase_lab_")``);
  * that project's own generated ``supabase/config.toml``, patched in place
    with a unique set of currently-free high local ports so it can never
    collide with any other Supabase stack (this repository's own included);
  * its own Docker-backed local Supabase stack, started via exactly ONE
    deterministic ``supabase start --workdir <that directory>`` call (no
    trimmed/fallback retry sequence -- see ``start_disposable_stack(...)``);
  * a SANITIZED metadata-only log (``lab.log``), containing only a
    timestamp, a fixed command category, an exit code, and a fixed
    classification label per command -- see ``_run_cli_command(...)`` /
    ``_append_safe_log(...)``.

This module NEVER:

  * imports ``utils.access_control`` or calls
    ``utils.access_control.get_supabase_admin_client()`` / reads
    ``SUPABASE_URL`` / ``SUPABASE_SERVICE_ROLE_KEY`` from the process
    environment for its own client construction -- every Supabase client it
    builds is constructed directly from THIS lab's own freshly-started local
    stack's own ``supabase status`` output;
  * writes raw stdout/stderr from ANY subprocess call to disk, or embeds raw
    captured output in a raised exception -- ``supabase start`` and
    ``supabase status -o json`` can both emit service-role keys, anon keys,
    JWT secrets, database passwords, or complete database URLs, on success
    AND on failure, so ``_run_cli_command(...)`` returns stdout/stderr to the
    caller in memory ONLY (for JSON parsing / line counting) and never lets
    either reach a log file, an exception message, a dataclass repr, or a
    pytest assertion message -- see ``_redact_url(...)`` and
    ``LocalSupabaseStack.__repr__``;
  * starts creating any temporary lab directory or resource before
    ``check_docker_daemon_available(...)`` has confirmed the Docker daemon
    itself is reachable;
  * retries a failed ``supabase start`` over potentially partial resources --
    exactly one deterministic attempt is made; a failure is left for
    ``stop_and_cleanup_stack(...)`` to tear down;
  * modifies any file inside this repository, or executes/splits SQL beyond
    reading each of the three authorized V66/V67/V68 migration files verbatim
    and sending each one, unmodified, as a single ``cursor.execute(...)``
    call (see ``apply_migration_files(...)``);
  * runs ``docker system prune`` / ``docker volume prune`` / any wildcard or
    global Docker cleanup -- ``stop_and_cleanup_stack(...)`` only ever
    targets containers/volumes whose name contains this lab's own generated
    ``project_id``, and fails CLOSED (``cleanup_verified = False``) whenever
    either residue query itself fails, rather than treating an unknown
    Docker state as "no residue";
  * runs ANY Docker/Supabase command -- ``docker version`` included --
    against anything other than a proven LOCAL Docker endpoint: see
    ``check_docker_daemon_available(...)`` /
    ``_resolve_effective_docker_endpoint_classification(...)``, which
    reject a remote (``tcp://``/``ssh://``/``http(s)://``), empty, or
    unparseable/ambiguous effective Docker endpoint -- honoring the REAL
    Docker CLI's own precedence (``DOCKER_CONTEXT``, when set to a
    non-empty value, overrides ``DOCKER_HOST``; ``DOCKER_HOST`` applies
    only when no overriding ``DOCKER_CONTEXT`` is active; otherwise the
    configured/default current context applies -- see
    ``_resolve_effective_docker_endpoint_classification(...)``'s own
    docstring) -- BEFORE any temporary directory, port allocation, or
    container operation, and without ever printing/logging the endpoint
    text, the context name, or either environment variable's value;
  * writes a single file into this directory, or this repository, before
    confirming the freshly-created ``tempfile.mkdtemp(...)`` lab directory
    (and its own soon-to-be-created ``supabase`` subdirectory) is actually
    OUTSIDE this repository -- see ``_require_lab_dir_outside_repo(...)``,
    called immediately after directory creation and before ``supabase
    init``, so a misconfigured OS temporary directory can never cause this
    lab to write into the repository it is validating;
  * renders raw subprocess ``stdout``/``stderr`` through ordinary object
    rendering -- ``_CommandResult.__repr__``/``__str__`` are overridden to
    show only ``returncode`` and ``classification``; and its own internal
    exception-construction never retains an original subprocess exception
    (``subprocess.TimeoutExpired`` included) as ``__cause__`` or
    ``__context__`` -- see ``_run_cli_command(...)``'s own docstring;
  * calls ``psycopg2.connect(...)`` directly anywhere other than inside
    ``connect_to_lab_database(...)`` -- every other internal caller, and the
    live integration test's own direct row-count/state assertions, must go
    through that ONE sanitized helper instead, so a connection-time
    failure can never leak a database password or a complete database URL
    -- see ``connect_to_lab_database(...)``'s own docstring;
  * reports ``cleanup_verified = True`` unless the temporary lab directory
    was ALSO actually removed -- ``disposable_ba201_lab(...)``'s own outer
    ``finally`` block recomputes the final ``CleanupReport.cleanup_verified``
    as the conjunction of the container/volume cleanup outcome AND
    ``not lab_dir.exists()`` after ``shutil.rmtree(...)`` has run, and
    ``run_disposable_lab_body_with_cleanup_verification(...)`` therefore
    fails (or attaches a sanitized note to a body's own exception) whenever
    EITHER one fails -- see ``CleanupReport``'s own docstring.

SIM-INT-02D also corrected this module's Docker-context/``DOCKER_HOST``
precedence to match the real Docker CLI's own documented resolution order
(``DOCKER_CONTEXT``, when set to a non-empty value, overrides
``DOCKER_HOST`` entirely; ``DOCKER_HOST`` applies only when no overriding
``DOCKER_CONTEXT`` is active) -- see
``_resolve_effective_docker_endpoint_classification(...)``'s own docstring.

SIM-INT-02E: the ONE real ``subprocess.run(...)`` call this module ever
makes (inside ``_run_cli_command(...)``) now passes ``encoding="utf-8",
errors="replace"`` EXPLICITLY alongside ``text=True`` -- never relying on
the operating system's default/locale text encoding (``cp1252`` on
Windows), which cannot decode every byte sequence real Supabase/Docker CLI
output can contain and, left implicit, can crash a background subprocess
reader thread with an unhandled ``UnicodeDecodeError`` (surfacing under
pytest as a ``PytestUnhandledThreadExceptionWarning``). ``errors="replace"``
guarantees decoding itself can never raise; undecodable bytes become the
Unicode replacement character instead. This changes ONLY how bytes become
`str` -- it does not weaken any existing sanitization guarantee: raw
stdout/stderr (however they decoded) still never reach a log file, an
exception message, or ordinary object rendering, and machine-readable
(JSON) parsing continues to fail closed with a fixed, sanitized message
rather than ever echoing corrupted or replacement-character-laden content
-- see ``start_disposable_stack(...)``'s own JSON-decode handling.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import psycopg2

# ---------------------------------------------------------------------------
# Repository paths -- read-only. This module never writes into any of these.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    # Only so that `utils.scenario_*` / `utils.scenario_learner_controller`
    # (pure Python, no Supabase-config side effects at import time) can be
    # imported by this lab and by the gated test module. This does NOT import
    # utils.access_control anywhere in this file.
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_RELATIVE_PATHS: Tuple[str, ...] = (
    "supabase/migrations/20260718170000_v66_scenario_definition_persistence_foundation.sql",
    "supabase/migrations/20260719003000_v67_harden_scenario_definition_security.sql",
    "supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql",
)

ENV_OPT_IN_VAR = "CERTBOUND_RUN_LOCAL_SUPABASE_INTEGRATION"

_LOOPBACK_HOSTNAMES = {"localhost"}

class Ba201IntegrationLabError(RuntimeError):
    """Raised for any lab setup, isolation-guard, migration, seed, or
    teardown failure. Never constructed with a credential in its message."""


def is_local_supabase_integration_enabled() -> bool:
    """`True` only when the explicit opt-in environment variable is set to
    exactly `"1"`. The gated integration test must check this BEFORE this
    module attempts any filesystem, subprocess, network, or Docker action."""
    return str(os.environ.get(ENV_OPT_IN_VAR, "") or "").strip() == "1"


# ---------------------------------------------------------------------------
# Sanitized reporting helpers -- never print a credential.
# ---------------------------------------------------------------------------


_REDACTED_URL_PLACEHOLDER = "<redacted-unparseable-url>"


def _redact_url(url: str) -> str:
    """Return `scheme://host:port` only -- strips any userinfo/credentials,
    path, query string, or fragment. Safe to print/log/include in a report.

    SIM-INT-02B: fully fail-closed. `urlsplit(...)` itself rarely raises,
    but its `.hostname`/`.port` PROPERTIES raise `ValueError` lazily, on
    ACCESS, for a non-numeric port or malformed IPv6 host (e.g. an unclosed
    `[` bracket) -- a bare `try/except` around `urlsplit(...)` alone does
    NOT catch that. Every parsing step (`urlsplit` AND the two property
    accesses) is therefore inside ONE `try/except Exception` below, and ANY
    failure -- invalid URL syntax, a non-numeric port, malformed IPv6
    brackets, or a missing scheme/host -- returns the SAME fixed
    `_REDACTED_URL_PLACEHOLDER` constant. This never echoes back any
    fragment of the raw input, and never lets a `urllib.parse` exception's
    own message (which can itself quote part of the offending input)
    propagate to a caller."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        scheme = parsed.scheme
    except Exception:  # noqa: BLE001 - any parser failure must fail closed to the fixed placeholder.
        return _REDACTED_URL_PLACEHOLDER
    if not scheme or not host:
        return _REDACTED_URL_PLACEHOLDER
    port_suffix = f":{port}" if port else ""
    return f"{scheme}://{host}{port_suffix}"


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().strip("[]")
    if not host:
        return False
    if host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Port allocation -- pure `socket` calls, no Docker/Postgres required.
# ---------------------------------------------------------------------------


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def allocate_unique_high_ports(
    count: int,
    *,
    low: int = 40000,
    high: int = 65000,
    attempts: int = 400,
) -> List[int]:
    """Allocate `count` DISTINCT, currently-free, high loopback TCP ports.

    Uses `secrets.randbelow` (not a fixed offset) specifically so that two
    concurrent invocations of this lab on the same machine are extremely
    unlikely to collide, and so this lab can never collide with this
    repository's OWN default local Supabase ports (54320-54329), which are
    well below `low`.
    """
    if count <= 0:
        return []
    chosen: List[int] = []
    seen: set = set()
    for _ in range(attempts):
        if len(chosen) >= count:
            break
        candidate = low + secrets.randbelow(max(high - low, 1))
        if candidate in seen:
            continue
        seen.add(candidate)
        if _port_is_free(candidate):
            chosen.append(candidate)
    if len(chosen) < count:
        raise Ba201IntegrationLabError(
            f"Could not allocate {count} free local ports in [{low}, {high}) "
            f"after {attempts} attempts (only found {len(chosen)})."
        )
    return chosen


@dataclass(frozen=True)
class LabPorts:
    """SIM-INT-02B: every distinct host-binding port the generated
    `config.toml` actually exposes for a local Supabase stack -- including
    the previously-uncovered Inbucket SMTP/POP3 ports (`local_smtp.smtp_port`
    / `local_smtp.pop3_port`), which are present but COMMENTED OUT in the
    CLI's own generated default (see `_patch_config_ports(...)`, which
    uncomments and patches them). All ten fields are always allocated as
    ten mutually distinct free ports -- never a repeated value -- so this
    lab can never collide with this repository's own default local Supabase
    ports, with another concurrent run of this same lab, or with itself
    across any two of these ten bindings."""

    api: int
    db: int
    shadow_db: int
    pooler: int
    studio: int
    inbucket_web: int
    inbucket_smtp: int
    inbucket_pop3: int
    analytics: int
    edge_runtime_inspector: int

    @classmethod
    def allocate(cls) -> "LabPorts":
        ports = allocate_unique_high_ports(10)
        instance = cls(
            api=ports[0],
            db=ports[1],
            shadow_db=ports[2],
            pooler=ports[3],
            studio=ports[4],
            inbucket_web=ports[5],
            inbucket_smtp=ports[6],
            inbucket_pop3=ports[7],
            analytics=ports[8],
            edge_runtime_inspector=ports[9],
        )
        if not instance.all_unique():
            # Defense in depth: `allocate_unique_high_ports(...)` already
            # de-duplicates internally, so this should be unreachable, but
            # this lab must never proceed with two host bindings sharing one
            # port regardless of how that could happen.
            raise Ba201IntegrationLabError("Allocated lab ports were not all mutually distinct; aborting.")
        return instance

    def as_tuple(self) -> Tuple[int, ...]:
        return (
            self.api,
            self.db,
            self.shadow_db,
            self.pooler,
            self.studio,
            self.inbucket_web,
            self.inbucket_smtp,
            self.inbucket_pop3,
            self.analytics,
            self.edge_runtime_inspector,
        )

    def all_free(self) -> bool:
        """`True` only when ALL TEN allocated host-binding ports are
        currently free -- checked immediately before `supabase start`."""
        return all(_port_is_free(p) for p in self.as_tuple())

    def all_unique(self) -> bool:
        return len(set(self.as_tuple())) == len(self.as_tuple())


# ---------------------------------------------------------------------------
# `supabase init` + config.toml port patching
# ---------------------------------------------------------------------------


def generate_unique_project_id() -> str:
    return f"certbound-sim-int-02-{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# SIM-INT-02A: credential-safe command runner.
#
# `supabase start` and `supabase status -o json` can emit service-role keys,
# anon keys, JWT secrets, database passwords, and complete database URLs on
# BOTH stdout and stderr -- on success (normal `status` output) and on
# failure (an error message can echo back part of a connection string). This
# runner NEVER persists raw stdout/stderr to disk for ANY command, and NEVER
# embeds captured output in a raised exception's message. Callers still
# receive stdout/stderr in memory (as `_CommandResult` fields) so they can
# parse JSON / count filtered lines -- "never written to disk, never printed,
# never included in an exception" is enforced here once, centrally, rather
# than by every caller remembering to redact.
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Fixed, small, non-sensitive classification labels. Matching is done
# in-memory against captured stderr, but only the LABEL (never the matched
# text itself) is ever logged or raised.
_FAILURE_CLASSIFIERS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "docker_daemon_unreachable",
        re.compile(
            r"cannot connect to the docker daemon|dockerdesktoplinuxengine|"
            r"docker daemon is not running|pipe/docker_engine|error during connect",
            re.IGNORECASE,
        ),
    ),
)


def _classify_failure(returncode: Optional[int], stderr_text: str) -> str:
    """Return a fixed, safe classification label. Never returns or logs the
    matched text itself -- only ever one of a small predefined label set."""
    if returncode == 0:
        return "success"
    text = stderr_text or ""
    for label, pattern in _FAILURE_CLASSIFIERS:
        if pattern.search(text):
            return label
    return "cli_nonzero_exit"


def _append_safe_log(log_path: Optional[Path], *, category: str, exit_code: Optional[int], classification: str) -> None:
    """Append ONE line of SAFE metadata only: timestamp, command category,
    exit code, classification label. Never writes stdout/stderr. A logging
    failure itself (e.g. disk full) is swallowed -- logging is diagnostic
    only and must never mask a real setup/cleanup outcome."""
    if log_path is None:
        return
    try:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(
                f"{_utc_now_iso()} category={category} exit_code={exit_code} classification={classification}\n"
            )
    except OSError:
        pass


@dataclass(frozen=True, repr=False)
class _CommandResult:
    """SIM-INT-02C: `repr=False` plus a custom `__repr__`/`__str__` below --
    the DEFAULT dataclass-generated `repr(...)` would render every field,
    including `stdout`/`stderr`, which can hold service-role keys, anon
    keys, JWT secrets, database passwords, or complete database URLs on
    EITHER success or failure output. `stdout`/`stderr` remain ordinary,
    directly-accessible attributes -- the narrow parser callers that
    actually need them (JSON parsing, line counting) still read
    `result.stdout` / `result.stderr` directly -- only ORDINARY OBJECT
    RENDERING (`repr(...)`, `str(...)`, an f-string `{result!r}`, a pytest
    failure diff that renders this object, a debugger's default display) is
    blocked from ever showing them."""

    returncode: int
    stdout: str
    stderr: str
    classification: str

    def __repr__(self) -> str:
        return f"_CommandResult(returncode={self.returncode!r}, classification={self.classification!r})"

    __str__ = __repr__


# SIM-INT-02C: fixed, credential-free messages for each subprocess-launch-
# level failure classification `_run_cli_command(...)` can produce. Every
# value is a closure over `category` only (a caller-supplied fixed label
# like `"docker.daemon_check"`) -- never over any subprocess exception's own
# text/attributes.
def _fixed_launch_failure_message(category: str, classification: str) -> str:
    return {
        "timeout": f"Command category {category!r} timed out.",
        "executable_not_found": f"Command category {category!r} failed: executable not found.",
        "os_error": f"Command category {category!r} failed to start (OS-level error).",
        "unknown_subprocess_error": f"Command category {category!r} failed with an unexpected subprocess error.",
    }[classification]


def _run_cli_command(
    args: Sequence[str],
    *,
    category: str,
    log_path: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> _CommandResult:
    """Run one subprocess command. Returns stdout/stderr to the CALLER in
    memory only (for JSON parsing / line counting) -- never writes either to
    `log_path`, and never embeds either in a raised exception. Any
    subprocess-launch-level exception (timeout, missing executable, other OS
    error, or any other unexpected exception) is converted to a sanitized
    `Ba201IntegrationLabError` built ONLY from `category` and a fixed
    classification label -- `exc.stdout` / `exc.stderr` / `str(exc)` (which,
    for `subprocess.TimeoutExpired`, can itself carry partial captured
    output) are never read or embedded.

    SIM-INT-02C: the four `except` clauses below ONLY ever set a local
    `failure_classification` variable -- they never `raise` while the
    original subprocess exception is still the ACTIVE exception being
    handled. Once the `try/except` statement below has completed (having
    taken one of those branches), Python has already cleared that active
    exception state entirely (per the language's own except-block
    scoping), so the `Ba201IntegrationLabError` raised further down --
    OUTSIDE every `except` clause, with an explicit `from None` for
    extra defense-in-depth/clarity -- can NEVER pick up the original
    exception as either `__cause__` (would require an explicit `from exc`,
    which is never used here) OR the implicit `__context__` (would require
    the `raise` to still be lexically/dynamically inside the handler,
    which it is not). This closes a leak vector `subprocess.TimeoutExpired`
    in particular is prone to: even though `TimeoutExpired.__str__()` itself
    never renders `.output`/`.stderr`, a naive `raise NewError(...)` written
    directly inside the `except TimeoutExpired:` block would still attach
    the original `TimeoutExpired` (captured output and all) as
    `__context__`, which a full traceback print (e.g. an uncaught failure
    in CI logs) WOULD render in full.

    SIM-INT-02E: `encoding="utf-8", errors="replace"` are passed EXPLICITLY
    alongside `text=True` -- WITHOUT them, `subprocess.run(...)` falls back
    to `locale.getpreferredencoding(False)` for decoding captured
    stdout/stderr, which on Windows is typically `cp1252`. Real Supabase/
    Docker CLI output (a Unicode box-drawing character in a table, an
    emoji in a status message, etc.) can contain byte sequences that
    `cp1252` cannot decode -- and when BOTH stdout and stderr are piped,
    CPython's own `subprocess.Popen` decodes each in a background
    "reader thread" (see `subprocess.py`'s own `_readerthread`), so a
    decode failure there raises `UnicodeDecodeError` on THAT thread, which
    Python cannot propagate back to this function's own `try/except` at
    all -- it can only ever surface later as an unhandled thread exception
    (a `PytestUnhandledThreadExceptionWarning` under pytest). Explicit
    `encoding="utf-8"` makes decoding deterministic regardless of the host
    OS's configured locale, and `errors="replace"` guarantees that decoding
    itself can NEVER raise -- any byte sequence that is not valid UTF-8 is
    substituted with the Unicode replacement character (U+FFFD) instead,
    so the reader thread can never crash and `proc.stdout`/`proc.stderr`
    always end up as ordinary `str` values, exactly as before this change.
    """
    failure_classification: Optional[str] = None
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        failure_classification = "timeout"
    except FileNotFoundError:
        failure_classification = "executable_not_found"
    except OSError:
        failure_classification = "os_error"
    except Exception:  # noqa: BLE001 - last-resort: never let an unknown subprocess exception leak raw text.
        failure_classification = "unknown_subprocess_error"
    else:
        classification = _classify_failure(proc.returncode, proc.stderr or "")
        _append_safe_log(log_path, category=category, exit_code=proc.returncode, classification=classification)
        return _CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            classification=classification,
        )

    # Reached ONLY on a subprocess-launch-level exception, and only AFTER
    # the `try/except` statement above has fully completed -- see this
    # function's own docstring for why this placement is what actually
    # prevents `__context__` chaining.
    _append_safe_log(log_path, category=category, exit_code=None, classification=failure_classification)
    raise Ba201IntegrationLabError(_fixed_launch_failure_message(category, failure_classification)) from None


# ---------------------------------------------------------------------------
# SIM-INT-02C: local Docker-endpoint guard.
#
# `docker version` succeeding proves ONLY that Docker responded -- it says
# nothing about WHICH daemon answered. Docker can be pointed at a REMOTE
# daemon via a `DOCKER_HOST=tcp://...`/`DOCKER_HOST=ssh://...` environment
# override, or via a non-default context selected either explicitly
# (`docker context use ...`, persisted) or via a `DOCKER_CONTEXT=...`
# environment override for the current invocation. This lab must never run
# ANY container operation -- `supabase init`/`start`/`stop`, `docker
# ps`/`volume ls` -- against anything other than the LOCAL Docker Desktop/
# Engine instance, so the effective endpoint is resolved and classified
# BEFORE `docker version` itself is ever called (context inspection is a
# purely local, read-only config-file lookup -- it never contacts a daemon,
# so this ordering means no network call is ever made towards a
# misconfigured remote endpoint).
# ---------------------------------------------------------------------------

_LOCAL_DOCKER_ENDPOINT_PREFIXES: Tuple[str, ...] = ("npipe://", "unix://")


def _classify_docker_endpoint(endpoint: Optional[str]) -> str:
    """Classify a raw Docker endpoint string into a FIXED, safe label --
    the caller must NEVER return, log, or embed the raw `endpoint` text
    itself anywhere; only this function's fixed return label may propagate
    further. Returns `"local_ipc"` ONLY for a single-line value starting
    with `npipe://` (Windows named pipe) or `unix://` (Linux/macOS Unix
    domain socket). Returns `"remote_or_unknown"` for EVERYTHING else --
    `tcp://`, `ssh://`, `http://`/`https://`, empty/whitespace-only,
    multi-line (ambiguous/unparseable), or any other/unrecognized scheme."""
    text = (endpoint or "").strip()
    if not text or "\n" in text or "\r" in text:
        return "remote_or_unknown"
    if text.lower().startswith(_LOCAL_DOCKER_ENDPOINT_PREFIXES):
        return "local_ipc"
    return "remote_or_unknown"


def _resolve_current_context_endpoint_classification(*, log_path: Optional[Path] = None) -> str:
    """Resolve and classify the CURRENT Docker context's own recorded
    endpoint via `docker context show` followed by `docker context inspect
    <name> --format {{.Endpoints.docker.Host}}`. `docker context show`
    itself already reflects a `DOCKER_CONTEXT` environment-variable
    override when one is active -- this helper never reads that variable
    directly; it only ever inspects WHATEVER context the CLI itself reports
    as current. The context name and the inspected endpoint text are read
    only long enough to classify them in this function's own local scope,
    and are never returned, logged, or embedded in any exception."""
    context_result = _run_cli_command(
        ["docker", "context", "show"],
        category="docker.context_show",
        log_path=log_path,
        timeout=20,
    )
    if context_result.returncode != 0:
        return "remote_or_unknown"
    context_name = context_result.stdout.strip()
    if not context_name or "\n" in context_name or "\r" in context_name:
        return "remote_or_unknown"

    inspect_result = _run_cli_command(
        ["docker", "context", "inspect", context_name, "--format", "{{.Endpoints.docker.Host}}"],
        category="docker.context_inspect",
        log_path=log_path,
        timeout=20,
    )
    if inspect_result.returncode != 0:
        return "remote_or_unknown"
    return _classify_docker_endpoint(inspect_result.stdout)


def _resolve_effective_docker_endpoint_classification(*, log_path: Optional[Path] = None) -> str:
    """Determine the FIXED classification (`"local_ipc"` /
    `"remote_or_unknown"`) of the Docker endpoint that would actually be
    used for the next Docker command.

    SIM-INT-02D: this now matches the Docker CLI's OWN documented
    precedence exactly (the SIM-INT-02C version incorrectly treated
    `DOCKER_HOST` as always taking priority over any context):

    1. If `DOCKER_CONTEXT` is set to a non-empty value, IT overrides
       `DOCKER_HOST` entirely -- `DOCKER_HOST` is never even read/classified
       in this branch. The actual selected context is resolved and
       inspected via `_resolve_current_context_endpoint_classification(...)`
       (which itself calls `docker context show` -- already
       `DOCKER_CONTEXT`-aware -- then `docker context inspect`), and THAT
       inspected endpoint is what gets classified.
    2. Otherwise, if `DOCKER_HOST` is set to a non-empty value, it is
       classified DIRECTLY -- the configured/default context is never
       inspected in this branch.
    3. Otherwise (neither override active), the configured/default CURRENT
       context is resolved and inspected, exactly as in case 1.

    An environment variable set to the empty string is treated as ABSENT
    (`.strip()` before the truthiness check) -- matching the real Docker
    CLI's own treatment of an empty override as "not set". Only the
    PRESENCE of `DOCKER_CONTEXT` / `DOCKER_HOST` is ever branched on; VALUES
    are read only long enough to classify them in local scope and are never
    returned, logged, or embedded in any exception. The same is true of the
    context name and the inspected endpoint text resolved in cases 1 and 3."""
    docker_context_override = (os.environ.get("DOCKER_CONTEXT") or "").strip()
    if docker_context_override:
        # DOCKER_CONTEXT overrides DOCKER_HOST -- DOCKER_HOST is
        # deliberately never read in this branch.
        return _resolve_current_context_endpoint_classification(log_path=log_path)

    docker_host_override = (os.environ.get("DOCKER_HOST") or "").strip()
    if docker_host_override:
        # No overriding DOCKER_CONTEXT is active -- DOCKER_HOST applies
        # directly; the configured/default context is never inspected here.
        return _classify_docker_endpoint(docker_host_override)

    # Neither override is active: the configured/default current context
    # applies.
    return _resolve_current_context_endpoint_classification(log_path=log_path)


def check_docker_daemon_available(*, log_path: Optional[Path] = None) -> None:
    """Docker preflight. Must be called BEFORE any temporary lab directory
    is created, and before `supabase init` / `supabase start` are ever
    invoked. Two independent, fail-closed guards, in this order:

    1. LOCAL ENDPOINT (`_resolve_effective_docker_endpoint_classification`):
       the effective endpoint must classify as `"local_ipc"`. A remote
       (`tcp://`, `ssh://`, `http(s)://`), empty, or unparseable/ambiguous
       endpoint is REJECTED with a fixed sanitized message -- never the raw
       endpoint, context name, or environment-variable value.
    2. DAEMON REACHABILITY (`docker version`): must succeed.

    Raises a sanitized `Ba201IntegrationLabError` (no raw Docker output, no
    endpoint text, no context name, no environment-variable value) on
    either failure -- daemon unreachable, `docker` executable missing,
    timeout, or a remote/unknown effective endpoint."""
    endpoint_classification = _resolve_effective_docker_endpoint_classification(log_path=log_path)
    if endpoint_classification != "local_ipc":
        raise Ba201IntegrationLabError(
            "Docker's effective endpoint is not a local IPC endpoint (expected `npipe://` on "
            "Windows or `unix://` on Linux/macOS). Refusing to create any disposable lab "
            "resources against a remote or unknown Docker endpoint."
        )

    result = _run_cli_command(
        ["docker", "version"],
        category="docker.daemon_check",
        log_path=log_path,
        timeout=20,
    )
    if result.returncode != 0:
        raise Ba201IntegrationLabError(
            "Docker daemon is not available (`docker version` did not succeed; "
            f"classification={result.classification}). Refusing to create any disposable "
            "lab resources."
        )


# SIM-INT-02B: SECTION-AWARE port patching.
#
# Exact default port assignments and TOML section layout generated by
# `supabase init` under CLI 2.109.1, confirmed by directly running
# `supabase init` in a scratch OS-temp directory during SIM-INT-02/02B
# preflight. In particular, under this CLI version, the generated
# `[local_smtp]` section is:
#
#     [local_smtp]
#     port = 54324
#     # smtp_port = 54325
#     # pop3_port = 54326
#
# -- i.e. the Inbucket web port is active, but the SMTP/POP3 ports are
# present in the generated file yet COMMENTED OUT by default. This lab
# UNCOMMENTS and patches both to a unique port each, so no default
# host-binding port (active OR latent) is ever left in place, regardless of
# whether a future CLI version enables them by default.
#
# Each (section, pattern, label) tuple below is matched ONLY within that
# EXACT top-level-or-nested TOML section's own text span (see
# `_split_toml_sections(...)`) -- never against the whole file -- so this
# can never patch an unrelated field merely because it happens to share the
# literal text `port = ...`, and a match count other than exactly 1 WITHIN
# that specific section (including the section itself not being found
# exactly once) fails closed with `Ba201IntegrationLabError` rather than
# silently leaving a colliding default port in place if a future CLI version
# ever changes its generated section layout or defaults.
_PORT_PATCH_SPECS: Tuple[Tuple[str, str, str], ...] = (
    # (section, pattern, label) -- section "" means the file's own top-level
    # preamble, before the first "[section]" header, where `project_id` lives.
    ("", r'(?m)^project_id = ".*"$', "project_id"),
    ("api", r"(?m)^port = 54321$", "api.port"),
    ("db", r"(?m)^port = 54322$", "db.port"),
    ("db", r"(?m)^shadow_port = 54320$", "db.shadow_port"),
    ("db.pooler", r"(?m)^port = 54329$", "db.pooler.port"),
    ("studio", r"(?m)^port = 54323$", "studio.port"),
    ("local_smtp", r"(?m)^port = 54324$", "local_smtp.port"),
    ("local_smtp", r"(?m)^# smtp_port = 54325$", "local_smtp.smtp_port"),
    ("local_smtp", r"(?m)^# pop3_port = 54326$", "local_smtp.pop3_port"),
    ("analytics", r"(?m)^port = 54327$", "analytics.port"),
    ("edge_runtime", r"(?m)^inspector_port = 8083$", "edge_runtime.inspector_port"),
)

_PORT_PATCH_REPLACEMENTS: Dict[str, Callable[[str, "LabPorts"], str]] = {
    "project_id": lambda project_id, ports: f'project_id = "{project_id}"',
    "api.port": lambda project_id, ports: f"port = {ports.api}",
    "db.port": lambda project_id, ports: f"port = {ports.db}",
    "db.shadow_port": lambda project_id, ports: f"shadow_port = {ports.shadow_db}",
    "db.pooler.port": lambda project_id, ports: f"port = {ports.pooler}",
    "studio.port": lambda project_id, ports: f"port = {ports.studio}",
    "local_smtp.port": lambda project_id, ports: f"port = {ports.inbucket_web}",
    # Uncommented in place: the CLI's own generated default has these two
    # lines commented out (inactive) -- see the docstring block above.
    "local_smtp.smtp_port": lambda project_id, ports: f"smtp_port = {ports.inbucket_smtp}",
    "local_smtp.pop3_port": lambda project_id, ports: f"pop3_port = {ports.inbucket_pop3}",
    "analytics.port": lambda project_id, ports: f"port = {ports.analytics}",
    "edge_runtime.inspector_port": lambda project_id, ports: f"inspector_port = {ports.edge_runtime_inspector}",
}

_TOML_SECTION_HEADER_PATTERN = re.compile(r'(?m)^\[([^\]]+)\]\s*$')


def _split_toml_sections(config_text: str) -> List[Tuple[str, int, int]]:
    """Split `config_text` into `(section_name, start_offset, end_offset)`
    spans covering the ENTIRE text with no gaps and no overlaps.
    `section_name` is `""` for the leading preamble before the first
    `[section]` header line (where top-level keys such as `project_id`
    live). A COMMENTED-OUT header line (e.g. `# [db.vault]`) is never
    matched, because the pattern is anchored to the exact start of the
    line. Each real section's span starts at its own header line
    (inclusive) and ends immediately before the next header line (or EOF) --
    so a nested header like `[db.pooler]` is its OWN distinct span, separate
    from its parent `[db]` span, giving unambiguous per-field scoping even
    for sibling fields that share a literal value or name across sections."""
    headers = list(_TOML_SECTION_HEADER_PATTERN.finditer(config_text))
    spans: List[Tuple[str, int, int]] = []
    preamble_end = headers[0].start() if headers else len(config_text)
    if preamble_end > 0:
        spans.append(("", 0, preamble_end))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(config_text)
        spans.append((header.group(1), header.start(), end))
    return spans


def _patch_config_ports(config_text: str, *, project_id: str, ports: LabPorts) -> str:
    """Patch `config_text` in place, field-by-field, using SECTION-SCOPED
    matching (see `_split_toml_sections(...)`) -- never a whole-file regex
    substitution. Fails closed (`Ba201IntegrationLabError`) if the named
    section does not appear EXACTLY once, or if the field's own pattern does
    not match EXACTLY once within that section's own text, for ANY of the
    eleven required fields (`project_id` plus the ten `LabPorts` fields)."""
    patched = config_text
    for section, pattern, label in _PORT_PATCH_SPECS:
        spans = _split_toml_sections(patched)
        matching_spans = [(start, end) for name, start, end in spans if name == section]
        if len(matching_spans) != 1:
            section_desc = "top-level preamble" if section == "" else f"[{section}]"
            raise Ba201IntegrationLabError(
                f"Expected exactly one {section_desc} section while patching {label!r} in the "
                f"generated config.toml, found {len(matching_spans)}. Refusing to patch ports blindly "
                "-- the generated CLI configuration shape may have changed."
            )
        start, end = matching_spans[0]
        section_text = patched[start:end]
        field_matches = list(re.finditer(pattern, section_text))
        if len(field_matches) != 1:
            raise Ba201IntegrationLabError(
                f"Expected exactly one match for {label!r} within its own section of the generated "
                f"config.toml, found {len(field_matches)}. Refusing to patch ports blindly -- the "
                "generated CLI configuration shape may have changed."
            )
        replacement = _PORT_PATCH_REPLACEMENTS[label](project_id, ports)
        patched_section_text = re.sub(pattern, replacement, section_text, count=1)
        patched = patched[:start] + patched_section_text + patched[end:]
    return patched


def init_disposable_project(lab_dir: Path, *, project_id: str, ports: LabPorts, log_path: Path) -> None:
    """Run `supabase init` in `lab_dir` (created fresh, outside the repo),
    then patch its generated config.toml in place with unique ports and the
    unique project_id. Never touches Docker."""
    result = _run_cli_command(
        ["supabase", "init", "--workdir", str(lab_dir), "--yes"],
        category="supabase.init",
        log_path=log_path,
        timeout=60,
    )
    if result.returncode != 0:
        raise Ba201IntegrationLabError(
            f"`supabase init` failed (exit={result.returncode}, classification={result.classification})."
        )
    config_path = lab_dir / "supabase" / "config.toml"
    if not config_path.is_file():
        raise Ba201IntegrationLabError(f"Expected generated config not found: {config_path}")
    original_text = config_path.read_text(encoding="utf-8")
    patched_text = _patch_config_ports(original_text, project_id=project_id, ports=ports)
    config_path.write_text(patched_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stack lifecycle
# ---------------------------------------------------------------------------


@dataclass(repr=False, eq=False)
class LocalSupabaseStack:
    """Credentials/handles for ONE just-started disposable local stack.

    `__repr__`/`__str__` are overridden so this object can never leak a
    credential into a pytest failure traceback, a log line, or an assertion
    message -- only sanitized `host:port` values are ever rendered.
    """

    project_id: str
    lab_dir: Path
    ports: LabPorts
    api_url: str
    db_url: str
    service_role_key: str
    log_path: Path

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return (
            "LocalSupabaseStack("
            f"project_id={self.project_id!r}, "
            f"api={_redact_url(self.api_url)!r}, "
            f"db={_redact_url(self.db_url)!r})"
        )

    __str__ = __repr__

    def sanitized_summary(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "api": _redact_url(self.api_url),
            "db": _redact_url(self.db_url),
            "ports": {
                "api": self.ports.api,
                "db": self.ports.db,
                "shadow_db": self.ports.shadow_db,
                "pooler": self.ports.pooler,
                "studio": self.ports.studio,
                "inbucket_web": self.ports.inbucket_web,
                "inbucket_smtp": self.ports.inbucket_smtp,
                "inbucket_pop3": self.ports.inbucket_pop3,
                "analytics": self.ports.analytics,
                "edge_runtime_inspector": self.ports.edge_runtime_inspector,
            },
        }


_STATUS_KEY_CANDIDATES = {
    "api_url": ("API_URL",),
    "db_url": ("DB_URL",),
    "service_role_key": ("SERVICE_ROLE_KEY",),
}


def _extract_status_field(status_json: Mapping[str, Any], field_name: str) -> str:
    """SIM-INT-02B: treat `status_json` as fully untrusted, credential-
    bearing input. On a missing/empty field, raise a FIXED message naming
    ONLY the requested LOGICAL field name (`field_name`, one of this
    module's own constant strings, e.g. `"service_role_key"`) -- never any
    key name or value actually present in `status_json` itself. An
    attacker-controlled or malformed status document therefore can never
    inject its own key names or values into a raised exception, a log line,
    or a pytest failure message."""
    for key in _STATUS_KEY_CANDIDATES[field_name]:
        value = status_json.get(key) if isinstance(status_json, Mapping) else None
        if value:
            return str(value)
    raise Ba201IntegrationLabError(
        f"`supabase status -o json` did not contain a usable value for the required "
        f"{field_name!r} field."
    )


def start_disposable_stack(
    lab_dir: Path,
    *,
    project_id: str,
    ports: LabPorts,
    log_path: Path,
) -> LocalSupabaseStack:
    """Start THIS lab's own disposable stack and return its live
    credentials, obtained directly from `supabase status -o json` for this
    exact `--workdir`. Never reads any credential from the process
    environment or from `utils.access_control`.

    SIM-INT-02A: exactly ONE deterministic `supabase start` invocation --
    the full default container set, for maximum production fidelity -- with
    NO trimmed `--exclude` attempt and NO fallback retry. A failed start is
    reported and left for `stop_and_cleanup_stack(...)` to tear down; this
    function never issues a second `start` over potentially partial
    resources.
    """
    if not ports.all_free():
        raise Ba201IntegrationLabError(
            "One or more allocated lab ports are no longer free immediately before start; aborting."
        )

    start_result = _run_cli_command(
        ["supabase", "start", "--workdir", str(lab_dir), "--yes"],
        category="supabase.start",
        log_path=log_path,
        timeout=600,
    )
    if start_result.returncode != 0:
        raise Ba201IntegrationLabError(
            f"`supabase start` failed (exit={start_result.returncode}, "
            f"classification={start_result.classification})."
        )

    status_result = _run_cli_command(
        ["supabase", "status", "--workdir", str(lab_dir), "-o", "json"],
        category="supabase.status",
        log_path=log_path,
        timeout=60,
    )
    if status_result.returncode != 0:
        raise Ba201IntegrationLabError(
            f"`supabase status` failed (exit={status_result.returncode}, "
            f"classification={status_result.classification})."
        )
    try:
        status_json = json.loads(status_result.stdout)
    except json.JSONDecodeError:
        # SIM-INT-02B: `from None`, deliberately NOT `from exc` -- a
        # `JSONDecodeError` raised against malformed/truncated
        # credential-bearing CLI output must never be attached as this
        # exception's `__cause__` (a `JSONDecodeError`'s own message can
        # include positional context derived from the raw document).
        raise Ba201IntegrationLabError("`supabase status -o json` did not return valid JSON") from None
    if not isinstance(status_json, Mapping):
        raise Ba201IntegrationLabError("`supabase status -o json` did not return a JSON object") from None

    api_url = _extract_status_field(status_json, "api_url")
    db_url = _extract_status_field(status_json, "db_url")
    service_role_key = _extract_status_field(status_json, "service_role_key")

    return LocalSupabaseStack(
        project_id=project_id,
        lab_dir=lab_dir,
        ports=ports,
        api_url=api_url,
        db_url=db_url,
        service_role_key=service_role_key,
        log_path=log_path,
    )


@dataclass(frozen=True)
class CleanupReport:
    """SIM-INT-02A/02D: the sole authority on whether cleanup actually
    succeeded. Contains ONLY sanitized metadata -- exit codes, container/
    volume NAMES (which are just this lab's own generated `project_id`
    substring, never a credential), and counts.

    Two stages produce this dataclass:

    1. `stop_and_cleanup_stack(...)` returns one with `cleanup_verified`
       `True` only when its OWN five container/volume fail-closed
       conditions all hold (see that function's own docstring), and
       `lab_dir_removed=False` as a placeholder -- it runs BEFORE
       `shutil.rmtree(...)` and so cannot yet know that outcome.
    2. `disposable_ba201_lab(...)`'s own outer `finally` block then
       overwrites that report's `lab_dir_removed` with the ACTUAL
       post-`shutil.rmtree(...)` outcome, and recomputes `cleanup_verified`
       as `stage-1 cleanup_verified AND lab_dir_removed` -- so the value any
       caller ultimately observes (via `get_last_cleanup_report()`) is
       `True` only when BOTH the container/volume cleanup AND the temporary
       directory removal are verified."""

    project_id: str
    stop_exit_code: Optional[int]
    container_query_exit_code: Optional[int]
    volume_query_exit_code: Optional[int]
    remaining_containers: Tuple[str, ...]
    remaining_volumes: Tuple[str, ...]
    lab_dir_removed: bool
    cleanup_verified: bool
    note: Optional[str] = None


def _run_cleanup_step(
    args: Sequence[str],
    *,
    category: str,
    log_path: Optional[Path],
    timeout: float,
) -> Tuple[Optional[int], str]:
    """Like `_run_cli_command`, but NEVER raises -- a cleanup step must
    always produce *some* result so `stop_and_cleanup_stack(...)` can keep
    attempting the remaining steps and still return a complete, honest
    `CleanupReport` (an unknown/failed step is treated as `returncode=None`,
    which the fail-closed formula below always treats as "not verified")."""
    try:
        result = _run_cli_command(args, category=category, log_path=log_path, timeout=timeout)
        return result.returncode, result.stdout
    except Ba201IntegrationLabError:
        return None, ""


def stop_and_cleanup_stack(
    lab_dir: Path,
    *,
    project_id: str,
    log_path: Optional[Path],
) -> CleanupReport:
    """Stop and remove ONLY this lab's own disposable project's containers
    and volumes (`supabase stop --project-id <id> --no-backup`), then verify,
    via a NAME-FILTERED (never global/wildcard) `docker ps`/`docker volume
    ls`, that nothing bearing this lab's own `project_id` remains. Never runs
    `docker system prune`, `docker volume prune`, or `--all`.

    SIM-INT-02A: fails CLOSED. `cleanup_verified` is `True` only when ALL of
    the following hold -- `supabase stop` exited 0, the `docker ps` query
    exited 0, the `docker volume ls` query exited 0, AND both queries
    reported zero matching names. A failed/unknown-exit-code query is an
    UNKNOWN cleanup state, not an empty one -- it is never treated as "no
    residue". This function itself never raises, so the calling context
    manager's `finally` block can never be short-circuited by a cleanup
    failure before it removes the temporary directory.
    """
    stop_exit_code, _ = _run_cleanup_step(
        ["supabase", "stop", "--workdir", str(lab_dir), "--project-id", project_id, "--no-backup", "--yes"],
        category="supabase.stop",
        log_path=log_path,
        timeout=180,
    )

    container_query_exit_code, containers_stdout = _run_cleanup_step(
        ["docker", "ps", "-a", "--filter", f"name={project_id}", "--format", "{{.Names}}"],
        category="docker.ps",
        log_path=log_path,
        timeout=30,
    )
    volume_query_exit_code, volumes_stdout = _run_cleanup_step(
        ["docker", "volume", "ls", "--filter", f"name={project_id}", "--format", "{{.Name}}"],
        category="docker.volume_ls",
        log_path=log_path,
        timeout=30,
    )

    # "Empty stdout is accepted only when the Docker command itself
    # succeeded" -- remaining_containers/volumes are only ever populated
    # from stdout when the corresponding query's own exit code was 0.
    remaining_containers: Tuple[str, ...] = (
        tuple(line for line in containers_stdout.splitlines() if line.strip())
        if container_query_exit_code == 0
        else ()
    )
    remaining_volumes: Tuple[str, ...] = (
        tuple(line for line in volumes_stdout.splitlines() if line.strip())
        if volume_query_exit_code == 0
        else ()
    )

    cleanup_verified = (
        stop_exit_code == 0
        and container_query_exit_code == 0
        and volume_query_exit_code == 0
        and len(remaining_containers) == 0
        and len(remaining_volumes) == 0
    )

    note: Optional[str] = None
    if not cleanup_verified:
        note = (
            f"stop_exit={stop_exit_code} container_query_exit={container_query_exit_code} "
            f"volume_query_exit={volume_query_exit_code} remaining_containers={len(remaining_containers)} "
            f"remaining_volumes={len(remaining_volumes)}"
        )

    return CleanupReport(
        project_id=project_id,
        stop_exit_code=stop_exit_code,
        container_query_exit_code=container_query_exit_code,
        volume_query_exit_code=volume_query_exit_code,
        remaining_containers=remaining_containers,
        remaining_volumes=remaining_volumes,
        lab_dir_removed=False,  # filled in by the caller once shutil.rmtree has actually run
        cleanup_verified=cleanup_verified,
        note=note,
    )


# ---------------------------------------------------------------------------
# SIM-INT-02C: temporary-directory LOCATION guard.
#
# Called immediately after `tempfile.mkdtemp(...)` succeeds and BEFORE
# `supabase init` (or any other command) is ever invoked against that
# directory -- if the OS temporary directory were ever misconfigured to
# resolve inside this repository (e.g. a `TMPDIR`/`TEMP` environment
# variable accidentally pointed inward), this guard fires before a single
# file is written into the repo, rather than only being caught by
# `enforce_production_isolation_guards(...)` AFTER `supabase init` AND
# `supabase start` have already run.
# ---------------------------------------------------------------------------


def _require_path_outside_repo(path: Path, *, repo_root: Path, label: str) -> None:
    resolved_path = path.resolve()
    resolved_repo_root = repo_root.resolve()
    if resolved_path == resolved_repo_root:
        raise Ba201IntegrationLabError(f"{label} must not equal the repository root.")
    try:
        resolved_path.relative_to(resolved_repo_root)
    except ValueError:
        return  # Good: NOT inside the repository.
    raise Ba201IntegrationLabError(f"{label} must be outside the repository, but is inside it.")


def _require_lab_dir_outside_repo(lab_dir: Path, *, repo_root: Path) -> None:
    """Fail-closed: `lab_dir` itself, AND the Supabase project path that
    `supabase init` will scaffold inside it (`<lab_dir>/supabase`), must
    both be outside `repo_root`. The nested check is asserted explicitly,
    rather than merely assumed to follow from the outer one, in case of an
    unusual symlink/junction configuration on the host OS."""
    _require_path_outside_repo(lab_dir, repo_root=repo_root, label="Disposable project directory")
    _require_path_outside_repo(
        lab_dir / "supabase", repo_root=repo_root, label="Disposable project's generated Supabase path"
    )


# ---------------------------------------------------------------------------
# Production-isolation guards -- fail closed. Never returns/logs a credential.
# ---------------------------------------------------------------------------


def _require_loopback(url: str, *, label: str) -> Tuple[str, Optional[int]]:
    """SIM-INT-02B: fully fail-closed, exactly like `_redact_url(...)` above
    -- `urlsplit(...)`, `.hostname`, and `.port` are ALL inside one
    `try/except Exception`, since `.hostname`/`.port` can raise `ValueError`
    lazily (non-numeric port, malformed IPv6 brackets). ANY parsing failure
    raises a FIXED, sanitized `Ba201IntegrationLabError` chained `from None`
    -- never `from exc` -- so the original `urllib.parse` exception (whose
    own message can itself quote a fragment of the raw, potentially
    credential-bearing, URL) is never attached as this exception's
    `__cause__` and can never surface through a traceback, a chained
    exception, or a pytest failure message."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except Exception:  # noqa: BLE001 - any parser failure must fail closed with a fixed message.
        raise Ba201IntegrationLabError(
            f"{label} could not be parsed as a valid loopback URL; refusing to proceed."
        ) from None
    if not _is_loopback_host(host):
        raise Ba201IntegrationLabError(
            f"{label} hostname is not localhost/loopback; refusing to proceed."
        ) from None
    return host, port


def enforce_production_isolation_guards(
    *,
    lab_dir: Path,
    repo_root: Path,
    stack: LocalSupabaseStack,
) -> None:
    """Raise `Ba201IntegrationLabError` (fail closed) unless every SIM-INT-02
    production-isolation guard holds. Called immediately after the stack
    starts and BEFORE any migration/seed/controller call is issued against
    it."""
    resolved_lab_dir = lab_dir.resolve()
    resolved_repo_root = repo_root.resolve()
    try:
        resolved_lab_dir.relative_to(resolved_repo_root)
        raise Ba201IntegrationLabError(
            "Disposable project directory must be outside the repository, but is inside it."
        )
    except ValueError:
        pass  # Good: NOT inside the repository.

    if resolved_lab_dir == resolved_repo_root:
        raise Ba201IntegrationLabError("Disposable project directory must not equal the repository root.")

    _, api_port = _require_loopback(stack.api_url, label="Supabase API URL")
    _, db_port = _require_loopback(stack.db_url, label="Database URL")

    if api_port != stack.ports.api:
        raise Ba201IntegrationLabError(
            "Live API port does not match this disposable project's own configured port."
        )
    if db_port != stack.ports.db:
        raise Ba201IntegrationLabError(
            "Live DB port does not match this disposable project's own configured port."
        )

    # "Not linked to a remote project": this lab NEVER calls `supabase link`,
    # and the CLI records a link via a project-ref file under
    # `<lab_dir>/supabase/.temp/project-ref`. Verify that file does not exist.
    project_ref_path = lab_dir / "supabase" / ".temp" / "project-ref"
    if project_ref_path.exists():
        raise Ba201IntegrationLabError(
            "Disposable project unexpectedly appears linked to a remote Supabase project."
        )

    for env_var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        # This lab never READS these for its own client construction (see
        # module docstring); this loop only documents/asserts the guard by
        # name for reviewers, it does not change behavior.
        _ = os.environ.get(env_var)


# ---------------------------------------------------------------------------
# SIM-INT-02C/02D: sanitized psycopg2 connection -- the ONE helper anything
# in this lab (this module's own internal callers AND the live integration
# test) must use to obtain a `psycopg2` connection to the disposable stack's
# own database. Never call `psycopg2.connect(...)` directly anywhere else.
#
# A `psycopg2.connect(db_url)` FAILURE (unreachable host, auth failure, a
# malformed DSN) can raise a `psycopg2` exception whose own message embeds
# the raw DSN -- including the database password -- verbatim. This wrapper
# converts ANY such CONNECTION failure into a fixed, sanitized
# `Ba201IntegrationLabError` `from None`, never echoing `db_url` or the
# original exception's text. Once CONNECTED, ordinary per-statement SQL/
# cursor errors (missing table, syntax error, a failing migration, a
# row-count assertion helper) are NOT affected by this wrapper -- they
# continue to surface their own descriptive, credential-free
# `psycopg2.Error` text unchanged, since they describe schema/SQL content,
# never a connection string.
# ---------------------------------------------------------------------------


def connect_to_lab_database(db_url: str) -> Any:
    """SIM-INT-02C: mirrors `_run_cli_command(...)`'s own chaining-avoidance
    pattern -- the `except` clause below ONLY records that the connection
    failed; it never raises while `psycopg2.connect(...)`'s own exception
    (whose message can embed the raw, password-bearing DSN) is still the
    ACTIVE exception being handled. A bare `raise ... from None` INSIDE the
    `except` block would still populate `__context__` with that original
    exception (`from None` only sets `__suppress_context__`, hiding it from
    DEFAULT traceback rendering -- it does not clear the attribute itself,
    so anything that explicitly inspects `exc.__context__` could still
    reach the credential). Raising only AFTER the `try/except` has fully
    exited -- with no active exception left to attach -- gives a
    replacement exception with a genuinely empty `__context__`.

    SIM-INT-02D: this is now the PUBLIC name -- the live integration test's
    own direct row-count/state assertions must call this exact function
    (never `psycopg2.connect(...)` directly) so a connection failure during
    one of THOSE checks gets the same sanitized treatment as a connection
    failure during migration application or schema verification."""
    connected = False
    try:
        connection = psycopg2.connect(db_url)
        connected = True
    except Exception:  # noqa: BLE001 - any connection-time failure must fail closed with a fixed message.
        pass
    if connected:
        return connection
    raise Ba201IntegrationLabError(
        "Could not connect to the disposable lab's own local Postgres database."
    ) from None


# ---------------------------------------------------------------------------
# Migration application -- psycopg2, one unmodified `execute()` per file.
# ---------------------------------------------------------------------------


def read_migration_files(repo_root: Path, relative_paths: Sequence[str] = MIGRATION_RELATIVE_PATHS) -> List[Tuple[str, str]]:
    """Read each authorized migration file's COMPLETE, unmodified text.
    Never scans `supabase/migrations/` -- only reads these exact three
    repository-relative paths, in the exact order given."""
    files: List[Tuple[str, str]] = []
    for rel_path in relative_paths:
        full_path = (repo_root / rel_path).resolve()
        if not full_path.is_file():
            raise Ba201IntegrationLabError(f"Authorized migration file not found: {full_path}")
        files.append((rel_path, full_path.read_text(encoding="utf-8")))
    return files


def apply_migration_files(
    db_url: str,
    files: Sequence[Tuple[str, str]],
) -> List[str]:
    """Apply each (label, full_sql_text) pair, IN ORDER, as ONE unmodified
    `cursor.execute(full_sql_text)` call per file, with the connection in
    `autocommit = True` mode.

    Why this satisfies "no naive semicolon splitting, preserve dollar-quoted
    bodies and explicit transaction blocks": the file text is never split,
    parsed, or rewritten client-side at all -- it is sent to PostgreSQL as a
    single multi-statement Simple-Query-protocol message. PostgreSQL itself
    (not psycopg2, not this module) parses that message, correctly handling
    every dollar-quoted function body internally, and -- per the documented
    Simple Query protocol -- executes an ordinary (no explicit BEGIN/COMMIT)
    multi-statement message as one implicit transaction, or honors an
    explicit `BEGIN; ... COMMIT;` pair inside the message exactly as written
    (as V67 uses) if present. `autocommit = True` ensures psycopg2 itself
    never wraps the message in an additional client-side transaction of its
    own on top of that.

    Stops immediately (raises `Ba201IntegrationLabError`, wrapping the
    original `psycopg2.Error`) on the first file that fails; every
    subsequent file in `files` is never sent.
    """
    applied: List[str] = []
    conn = connect_to_lab_database(db_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for label, sql_text in files:
                try:
                    cur.execute(sql_text)
                except psycopg2.Error as exc:
                    raise Ba201IntegrationLabError(
                        f"Migration {label} failed to apply: {exc.__class__.__name__}: {exc}"
                    ) from exc
                applied.append(label)
    finally:
        conn.close()
    return applied


# ---------------------------------------------------------------------------
# Post-install verification (schema / RPC / grants) -- read-only queries.
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = ("scenarios", "scenario_versions", "scenario_attempts", "scenario_decisions")
_EXPECTED_FUNCTIONS = (
    "publish_scenario_version_v1",
    "start_or_resume_scenario_attempt_v1",
    "get_scenario_attempt_v1",
    "submit_scenario_decision_v1",
    "abandon_scenario_attempt_v1",
)


def verify_schema_and_grants(db_url: str) -> Dict[str, Any]:
    """Read-only introspection: confirms the four V66/V68 tables and five
    RPCs exist, and confirms anon/authenticated cannot execute any scenario
    RPC while service_role can. Never queries any unrelated application
    table."""
    report: Dict[str, Any] = {"tables": {}, "functions": {}, "grants": {}}
    conn = connect_to_lab_database(db_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in _EXPECTED_TABLES:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
                report["tables"][table] = bool(cur.fetchone()[0])

            for fn in _EXPECTED_FUNCTIONS:
                cur.execute(
                    "SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname = %s",
                    (fn,),
                )
                report["functions"][fn] = cur.fetchone()[0] > 0

            for role in ("anon", "authenticated", "service_role"):
                per_role: Dict[str, bool] = {}
                for fn in _EXPECTED_FUNCTIONS:
                    cur.execute(
                        "SELECT bool_or(has_function_privilege(%s, p.oid, 'EXECUTE')) "
                        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'public' AND p.proname = %s",
                        (role, fn),
                    )
                    row = cur.fetchone()
                    per_role[fn] = bool(row[0]) if row and row[0] is not None else False
                report["grants"][role] = per_role
    finally:
        conn.close()
    return report


# ---------------------------------------------------------------------------
# Test-only commit-then-raise proxy client (requirement B).
# ---------------------------------------------------------------------------


class SimulatedLostResponseError(ConnectionError):
    """Test-only: models a real RPC call whose request committed on the
    server but whose HTTP response never reached the caller (e.g. a
    dropped connection / read timeout after the server already committed).
    A plain `ConnectionError` subclass so it is caught by exactly the same
    generic backend-exception handling `utils.scenario_learner_controller`
    already uses for any other unmapped persistence-layer exception."""


class _CommitThenRaiseRpcBuilder:
    """Wraps ONE real RPC builder returned by the real client's
    `.rpc(name, params)`. `.execute()` lets the REAL call reach the server
    and commit, captures the response for the test's own assertions, then
    raises `SimulatedLostResponseError` INSTEAD of returning it."""

    def __init__(self, real_builder: Any, owner: "CommitThenRaiseProxyClient") -> None:
        self._real_builder = real_builder
        self._owner = owner

    def execute(self) -> Any:
        response = self._real_builder.execute()  # Real commit happens here.
        self._owner.captured_committed_response = response
        raise SimulatedLostResponseError(
            "Simulated: the server committed this submit_scenario_decision_v1 "
            "request, but its HTTP response was lost in transit."
        )

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        return getattr(self._real_builder, name)


class CommitThenRaiseProxyClient:
    """A test-only proxy around ONE real local Supabase client.

    Forwards every `.table(...)` call, and every `.rpc(...)` call EXCEPT the
    single targeted one, straight to the real client unchanged. The very
    FIRST `.rpc("submit_scenario_decision_v1", params)` call whose
    `params["p_idempotency_key"]` equals `target_idempotency_key` is
    intercepted exactly once (see `_CommitThenRaiseRpcBuilder` above); every
    call thereafter -- including an identical retry with the same
    idempotency key -- is forwarded to the real client untouched.

    Never monkeypatches any global `postgrest-py`/`supabase-py` class or
    module; this is a plain, local, per-instance wrapper object. Never
    modifies `utils.scenario_persistence` or
    `utils.scenario_learner_controller`.
    """

    def __init__(self, real_client: Any, *, target_idempotency_key: str) -> None:
        self._real_client = real_client
        self._target_idempotency_key = target_idempotency_key
        self._intercepted = False
        self.captured_committed_response: Optional[Any] = None

    def table(self, name: str) -> Any:
        return self._real_client.table(name)

    def rpc(self, name: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        real_builder = self._real_client.rpc(name, params)
        if (
            not self._intercepted
            and name == "submit_scenario_decision_v1"
            and isinstance(params, Mapping)
            and str(params.get("p_idempotency_key")) == self._target_idempotency_key
        ):
            self._intercepted = True
            return _CommitThenRaiseRpcBuilder(real_builder, self)
        return real_builder

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        return getattr(self._real_client, name)


# ---------------------------------------------------------------------------
# SIM-INT-02B: read-only, post-publication row verification.
#
# `SeedResult` (below) intentionally echoes several of its OWN input
# parameters (`version`, `engine_version`, `canonical_content_sha256`) --
# it is a convenience summary of what was REQUESTED, not proof of what was
# actually PERSISTED. These two helpers instead re-fetch the row directly
# from the real local database, through the real client, so a caller (the
# live integration test) can assert against ACTUAL stored values rather
# than input echoes.
# ---------------------------------------------------------------------------


def fetch_scenario_version_row(client: Any, *, scenario_version_id: str) -> Dict[str, Any]:
    """Read-only: fetch the ONE `scenario_versions` row with this exact
    `id`, directly through the real client. Raises
    `Ba201IntegrationLabError` if it does not exist or is not unique."""
    result = client.table("scenario_versions").select("*").eq("id", scenario_version_id).execute()
    rows = list(result.data or [])
    if len(rows) != 1:
        raise Ba201IntegrationLabError(
            f"Expected exactly one scenario_versions row for the requested id, found {len(rows)}."
        )
    return dict(rows[0])


def fetch_scenario_row(client: Any, *, scenario_id: str) -> Dict[str, Any]:
    """Read-only: fetch the ONE `scenarios` row with this exact `id`,
    directly through the real client. Raises `Ba201IntegrationLabError` if
    it does not exist or is not unique."""
    result = client.table("scenarios").select("*").eq("id", scenario_id).execute()
    rows = list(result.data or [])
    if len(rows) != 1:
        raise Ba201IntegrationLabError(
            f"Expected exactly one scenarios row for the requested id, found {len(rows)}."
        )
    return dict(rows[0])


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedResult:
    scenario_id: str
    scenario_version_id: str
    version: str
    engine_version: str
    canonical_content_sha256: str
    became_current: bool
    current_published_version_id: str


def seed_ba201_scenario(
    client: Any,
    *,
    certification_exam_name: str,
    simulation_id: str,
    title: str,
    version: str,
    schema_version: str,
    engine_version: str,
    canonical_content_sha256: str,
    content_snapshot: Mapping[str, Any],
    source_repository_path: str,
) -> SeedResult:
    """Seed exactly one scenario + one published scenario_versions row,
    entirely through the real local service-role client (`.table(...)`
    inserts for the two definition tables, which V66 grants directly to
    service_role, then `publish_scenario_version_v1` for publication)."""
    scenario_insert = (
        client.table("scenarios")
        .insert(
            {
                "simulation_id": simulation_id,
                "certification_exam_name": certification_exam_name,
                "title": title,
                "is_active": True,
            }
        )
        .execute()
    )
    scenario_row = scenario_insert.data[0]
    scenario_id = scenario_row["id"]

    version_insert = (
        client.table("scenario_versions")
        .insert(
            {
                "scenario_id": scenario_id,
                "version": version,
                "schema_version": schema_version,
                "engine_version": engine_version,
                "source_repository_path": source_repository_path,
            }
        )
        .execute()
    )
    version_row = version_insert.data[0]
    scenario_version_id = version_row["id"]

    publish_result = (
        client.rpc(
            "publish_scenario_version_v1",
            {
                "p_scenario_version_id": scenario_version_id,
                "p_content_snapshot": dict(content_snapshot),
                "p_canonical_content_sha256": canonical_content_sha256,
            },
        )
        .execute()
    )
    published_row = publish_result.data[0]

    scenario_after = client.table("scenarios").select("current_published_version_id").eq("id", scenario_id).execute()
    current_pointer = scenario_after.data[0]["current_published_version_id"]

    return SeedResult(
        scenario_id=scenario_id,
        scenario_version_id=scenario_version_id,
        version=version,
        engine_version=engine_version,
        canonical_content_sha256=canonical_content_sha256,
        became_current=bool(published_row["became_current"]),
        current_published_version_id=current_pointer,
    )


def publish_second_version(
    client: Any,
    *,
    scenario_id: str,
    version: str,
    schema_version: str,
    engine_version: str,
    canonical_content_sha256: str,
    content_snapshot: Mapping[str, Any],
    source_repository_path: str,
) -> SeedResult:
    """Insert and publish a SECOND draft version for the SAME `scenario_id`
    (distinct `version` string, same underlying content). Does not touch or
    require the first version's row at all."""
    version_insert = (
        client.table("scenario_versions")
        .insert(
            {
                "scenario_id": scenario_id,
                "version": version,
                "schema_version": schema_version,
                "engine_version": engine_version,
                "source_repository_path": source_repository_path,
            }
        )
        .execute()
    )
    version_row = version_insert.data[0]
    scenario_version_id = version_row["id"]

    publish_result = (
        client.rpc(
            "publish_scenario_version_v1",
            {
                "p_scenario_version_id": scenario_version_id,
                "p_content_snapshot": dict(content_snapshot),
                "p_canonical_content_sha256": canonical_content_sha256,
            },
        )
        .execute()
    )
    published_row = publish_result.data[0]

    scenario_after = client.table("scenarios").select("current_published_version_id").eq("id", scenario_id).execute()
    current_pointer = scenario_after.data[0]["current_published_version_id"]

    return SeedResult(
        scenario_id=scenario_id,
        scenario_version_id=scenario_version_id,
        version=version,
        engine_version=engine_version,
        canonical_content_sha256=canonical_content_sha256,
        became_current=bool(published_row["became_current"]),
        current_published_version_id=current_pointer,
    )


# ---------------------------------------------------------------------------
# Top-level orchestration context manager
# ---------------------------------------------------------------------------


@dataclass(repr=False, eq=False)
class Ba201Lab:
    stack: LocalSupabaseStack
    real_client: Any
    lab_dir: Path
    log_path: Path
    migrations_applied: List[str] = field(default_factory=list)
    schema_report: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return f"Ba201Lab(stack={self.stack!r})"

    __str__ = __repr__


# ---------------------------------------------------------------------------
# SIM-INT-02A: last-cleanup-report accessor.
#
# The `with disposable_ba201_lab() as lab:` body's `lab` object is out of
# scope once the `with` block exits, but the LIVE TEST must still prove
# cleanup afterward -- so the context manager stashes the final
# `CleanupReport` here (module-level, single most-recent-run state; this lab
# only ever runs one attempt at a time, never concurrently) for the caller to
# read once the `with` block has returned/raised.
# ---------------------------------------------------------------------------

_last_cleanup_report: Optional[CleanupReport] = None


def get_last_cleanup_report() -> Optional[CleanupReport]:
    """The most recent `disposable_ba201_lab()` run's final `CleanupReport`,
    or `None` if no run has completed its `finally` block yet."""
    return _last_cleanup_report


def _set_last_cleanup_report(report: CleanupReport) -> None:
    global _last_cleanup_report
    _last_cleanup_report = report


@contextlib.contextmanager
def disposable_ba201_lab() -> Iterator[Ba201Lab]:
    """Full lifecycle: check the Docker daemon -> allocate ports ->
    `supabase init` -> patch config -> ONE deterministic `supabase start` ->
    isolation guards -> apply V66/V67/V68 -> verify schema/grants -> yield a
    ready `Ba201Lab` -> (in `finally`) stop + verify no residue + remove the
    temporary directory.

    Only ever imported by the gated integration test. Raises
    `Ba201IntegrationLabError` if any setup step fails; cleanup ALWAYS runs
    regardless, and the temporary directory is ALWAYS removed -- neither a
    setup failure, a body (test-assertion) failure, nor a cleanup failure can
    prevent that removal (see the nested `try/finally` below).

    Call `get_last_cleanup_report()` after the `with` block exits to inspect
    the final `CleanupReport` -- `cleanup_verified` must be checked by the
    caller; this function does not, itself, fail the test on unverified
    cleanup (see `run_disposable_lab_body_with_cleanup_verification(...)`).
    """
    # SIM-INT-02A/02B: deterministic orchestration order --
    #   1. Docker preflight (no directory, no ports, no ``supabase init``
    #      happen at all when the daemon is unreachable);
    #   2. generate the unique project identifier;
    #   3. allocate all ten unique ports;
    #   4. ONLY THEN create the temporary directory;
    #   5. immediately enter the cleanup-protected `try/finally` lifecycle.
    # A project-id-generation or port-allocation failure therefore can NEVER
    # leave a temporary directory behind -- no directory has been created
    # yet at that point -- and once the directory IS created, every later
    # exception (setup, body, or cleanup) passes through the unconditional
    # `finally` below that removes it.
    check_docker_daemon_available()
    project_id = generate_unique_project_id()
    ports = LabPorts.allocate()

    lab_dir = Path(tempfile.mkdtemp(prefix="ba201_supabase_lab_"))
    log_path = lab_dir / "lab.log"

    start_attempted = False
    try:
        try:
            # SIM-INT-02C: validated FIRST, before `supabase init` (or any
            # other command) ever touches this directory -- see
            # `_require_lab_dir_outside_repo(...)`'s own module-level
            # comment block for why this must run this early rather than
            # only as part of `enforce_production_isolation_guards(...)`
            # (which does not run until AFTER `supabase init`/`start` have
            # already executed).
            _require_lab_dir_outside_repo(lab_dir, repo_root=REPO_ROOT)
            init_disposable_project(lab_dir, project_id=project_id, ports=ports, log_path=log_path)
            start_attempted = True
            stack = start_disposable_stack(lab_dir, project_id=project_id, ports=ports, log_path=log_path)
            enforce_production_isolation_guards(lab_dir=lab_dir, repo_root=REPO_ROOT, stack=stack)

            migration_files = read_migration_files(REPO_ROOT)
            applied = apply_migration_files(stack.db_url, migration_files)
            schema_report = verify_schema_and_grants(stack.db_url)

            from supabase import create_client  # local import: avoid module-load-time dependency for callers that only need port/guard helpers

            real_client = create_client(stack.api_url, stack.service_role_key)

            lab = Ba201Lab(
                stack=stack,
                real_client=real_client,
                lab_dir=lab_dir,
                log_path=log_path,
                migrations_applied=applied,
                schema_report=schema_report,
            )
            yield lab
        finally:
            # Cleanup runs whenever `supabase start` was ever attempted --
            # even if it (or a later setup step, or the yielded body) failed
            # -- because Docker Compose can leave partially-created
            # containers/volumes behind even when the `supabase start` CLI
            # invocation itself returned a non-zero exit code. Only skip the
            # `supabase stop` call entirely when `start` was never even
            # attempted (e.g. `supabase init` itself failed first, in which
            # case no containers could possibly exist for this project_id).
            #
            # This inner `try/except Exception` is deliberate belt-and-
            # suspenders on top of `stop_and_cleanup_stack(...)` already
            # never raising by design: even an unforeseen bug inside cleanup
            # logic must never prevent the outer `finally` below from
            # removing the temporary directory.
            try:
                if start_attempted:
                    report = stop_and_cleanup_stack(lab_dir, project_id=project_id, log_path=log_path)
                else:
                    report = CleanupReport(
                        project_id=project_id,
                        stop_exit_code=None,
                        container_query_exit_code=None,
                        volume_query_exit_code=None,
                        remaining_containers=(),
                        remaining_volumes=(),
                        lab_dir_removed=False,
                        cleanup_verified=True,  # nothing was ever started -- nothing to clean up.
                        note="supabase start was never attempted; no containers could exist for this project_id.",
                    )
            except Exception:  # noqa: BLE001 - cleanup must never mask directory removal below.
                report = CleanupReport(
                    project_id=project_id,
                    stop_exit_code=None,
                    container_query_exit_code=None,
                    volume_query_exit_code=None,
                    remaining_containers=(),
                    remaining_volumes=(),
                    lab_dir_removed=False,
                    cleanup_verified=False,
                    note="cleanup raised an unexpected internal error; treated as unverified.",
                )
            _set_last_cleanup_report(report)
    finally:
        # This OUTER finally is the true, final guarantee: it runs whether
        # the try above succeeded, raised during setup, raised inside the
        # yielded body, or the inner cleanup step raised unexpectedly.
        shutil.rmtree(lab_dir, ignore_errors=True)
        # SIM-INT-02D: `lab_dir_removed` must ACTUALLY gate the final
        # `cleanup_verified` value -- a prior version recorded
        # `lab_dir_removed` here but left `cleanup_verified` unchanged from
        # the (container/volume-only) inner report, which could leave
        # `cleanup_verified == True` alongside `lab_dir_removed == False` if
        # `shutil.rmtree(...)` itself failed. The final verified state is
        # now explicitly the CONJUNCTION of the prior container/volume
        # cleanup outcome AND this directory-removal outcome.
        lab_dir_removed = not lab_dir.exists()
        final_report = get_last_cleanup_report()
        if final_report is not None and final_report.project_id == project_id:
            final_cleanup_verified = final_report.cleanup_verified and lab_dir_removed
            note = final_report.note
            if not lab_dir_removed:
                # Fixed, sanitized text only -- NEVER embed `lab_dir` (or any
                # other path) in this note.
                dir_removal_note = "temporary lab directory removal could not be verified"
                note = f"{note}; {dir_removal_note}" if note else dir_removal_note
            _set_last_cleanup_report(
                CleanupReport(
                    project_id=final_report.project_id,
                    stop_exit_code=final_report.stop_exit_code,
                    container_query_exit_code=final_report.container_query_exit_code,
                    volume_query_exit_code=final_report.volume_query_exit_code,
                    remaining_containers=final_report.remaining_containers,
                    remaining_volumes=final_report.remaining_volumes,
                    lab_dir_removed=lab_dir_removed,
                    cleanup_verified=final_cleanup_verified,
                    note=note,
                )
            )


def run_disposable_lab_body_with_cleanup_verification(body: Callable[[Ba201Lab], None]) -> None:
    """SIM-INT-02A/02D: the ONE helper the live integration test uses. Runs
    `body(lab)` inside `disposable_ba201_lab()`, then REQUIRES a verified
    cleanup report -- "verified" now means BOTH that the container/volume
    cleanup succeeded AND that the temporary lab directory was actually
    removed (see `CleanupReport`'s own docstring and the outer `finally`
    block inside `disposable_ba201_lab(...)`); this function needs no
    additional directory-specific branch of its own, since
    `report.cleanup_verified` already reflects that conjunction by the time
    this function ever reads it:

    * body raises -> the original exception is re-raised UNCHANGED (never
      masked); if cleanup was ALSO not verified (container/volume cleanup,
      directory removal, or both), a sanitized diagnostic note (exit codes /
      counts / fixed phrases ONLY -- never credential-bearing text, never a
      path) is attached via `BaseException.add_note(...)` before
      re-raising, so both failures are visible without either masking the
      other.
    * body succeeds but cleanup was NOT verified (including the case where
      ONLY temporary-directory removal failed) -> raises
      `Ba201IntegrationLabError` (so the test still fails) with the same
      sanitized diagnostic note.
    * body succeeds and cleanup was verified (container/volume cleanup AND
      directory removal) -> returns normally.
    """
    try:
        with disposable_ba201_lab() as lab:
            body(lab)
    except Exception as exc:
        report = get_last_cleanup_report()
        if report is not None and not report.cleanup_verified:
            note = f"[SIM-INT-02A] cleanup could not be verified after a primary failure: {report.note}"
            add_note = getattr(exc, "add_note", None)
            if callable(add_note):
                add_note(note)
        raise
    else:
        report = get_last_cleanup_report()
        if report is None or not report.cleanup_verified:
            raise Ba201IntegrationLabError(
                "The integration lab body completed successfully, but cleanup could not be "
                f"verified: {report.note if report is not None else 'no cleanup report was recorded'}"
            )
