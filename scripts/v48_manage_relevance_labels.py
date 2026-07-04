#!/usr/bin/env python3
"""
Manage local relevance label sidecars for immutable V48 review packets.

Default mode is dry-run (no external calls). Human labels are stored separately
from the immutable source review packet under .local/v48/relevance_review/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping, Optional, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.v48_build_relevance_review_packet import (  # noqa: E402
    ALLOWED_RELEVANCE_LABELS,
    LOCAL_REVIEW_ROOT,
    compute_packet_hash,
)

LABEL_SIDECAR_SCHEMA_VERSION = "v48_relevance_labels_v1"
EXPECTED_PAIR_COUNT = 14


class RelevanceLabelSidecarError(RuntimeError):
    """Base error for relevance label sidecar management."""


class RelevanceLabelSidecarConfigError(RelevanceLabelSidecarError):
    """Raised when CLI configuration is invalid."""


class RelevanceLabelSidecarPathError(RelevanceLabelSidecarError):
    """Raised when a path is outside the permitted local directory."""


class RelevanceLabelSidecarIntegrityError(RelevanceLabelSidecarError):
    """Raised when source packet or sidecar integrity checks fail."""


class RelevanceLabelSidecarStateError(RelevanceLabelSidecarError):
    """Raised when sidecar lifecycle state prevents an operation."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_local_path(path: str, *, must_exist: bool = False) -> str:
    normalized = os.path.normpath(os.path.abspath(str(path)))
    allowed_root = os.path.normpath(os.path.abspath(LOCAL_REVIEW_ROOT))
    try:
        common = os.path.commonpath([normalized, allowed_root])
    except ValueError as exc:
        raise RelevanceLabelSidecarPathError(
            "path must be located under the local relevance review directory"
        ) from exc
    if common != allowed_root:
        raise RelevanceLabelSidecarPathError(
            "path must be located under the local relevance review directory"
        )
    if must_exist and not os.path.isfile(normalized):
        raise RelevanceLabelSidecarPathError(f"file not found: {normalized}")
    return normalized


def default_sidecar_path(*, source_packet_hash: str, directory: Optional[str] = None) -> str:
    prefix = str(source_packet_hash).strip().lower()[:16]
    root = directory or LOCAL_REVIEW_ROOT
    return os.path.join(root, f"v48_relevance_labels_{prefix}.json")


def load_json_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RelevanceLabelSidecarIntegrityError("JSON root must be an object")
    return payload


def verify_source_packet_integrity(packet: Mapping[str, Any]) -> str:
    stored_hash = str(packet.get("packet_hash") or "").strip().lower()
    if len(stored_hash) != 64:
        raise RelevanceLabelSidecarIntegrityError(
            "source packet packet_hash must be a 64-character SHA-256 hex digest"
        )
    hash_input = {
        key: value
        for key, value in packet.items()
        if key not in {"generated_at", "packet_hash"}
    }
    computed_hash = compute_packet_hash(hash_input)
    if computed_hash != stored_hash:
        raise RelevanceLabelSidecarIntegrityError(
            "source packet canonical hash does not match stored packet_hash"
        )
    pair_count = int(packet.get("pair_count") or 0)
    pairs = packet.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != pair_count:
        raise RelevanceLabelSidecarIntegrityError(
            "source packet pair_count does not match pairs list length"
        )
    return computed_hash


def load_verified_source_packet(packet_path: str) -> tuple[str, dict[str, Any]]:
    normalized = validate_local_path(packet_path, must_exist=True)
    packet = load_json_file(normalized)
    packet_hash = verify_source_packet_integrity(packet)
    return normalized, packet


def build_sidecar_labels_from_packet(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    for pair in packet["pairs"]:
        labels.append(
            {
                "pair_id": str(pair["pair_id"]),
                "question_version_id": str(pair["question_version_id"]),
                "candidate_identity": str(pair["candidate_identity"]),
                "resource_title": str(pair.get("resource_title") or ""),
                "semantic_similarity": float(pair["semantic_similarity"]),
                "relevance_label": None,
                "reviewer_notes": "",
            }
        )
    labels.sort(key=lambda item: (item["pair_id"], item["candidate_identity"]))
    return labels


def build_sidecar_payload(
    *,
    source_packet: Mapping[str, Any],
    source_packet_path: str,
    source_packet_hash: str,
) -> dict[str, Any]:
    labels = build_sidecar_labels_from_packet(source_packet)
    return {
        "schema_version": LABEL_SIDECAR_SCHEMA_VERSION,
        "source_packet_hash": source_packet_hash,
        "replay_content_set_hash": str(source_packet["replay_content_set_hash"]),
        "source_packet_filename": os.path.basename(source_packet_path),
        "pair_count": int(source_packet["pair_count"]),
        "allowed_relevance_labels": list(ALLOWED_RELEVANCE_LABELS),
        "created_at": utc_now_iso(),
        "finalized_at": None,
        "label_set_hash": None,
        "labels": labels,
    }


def compute_label_set_hash(labels: Sequence[Mapping[str, Any]]) -> str:
    canonical_labels = []
    for entry in labels:
        canonical_labels.append(
            {
                "pair_id": str(entry["pair_id"]),
                "relevance_label": str(entry["relevance_label"]),
                "reviewer_notes": str(entry.get("reviewer_notes") or ""),
            }
        )
    canonical_labels.sort(key=lambda item: item["pair_id"])
    payload = {"labels": canonical_labels}
    return compute_packet_hash(payload)


def atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".tmp_relevance_labels_",
        suffix=".json",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=True)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def resolve_source_packet_path(sidecar: Mapping[str, Any], *, sidecar_path: str) -> str:
    filename = str(sidecar.get("source_packet_filename") or "").strip()
    if not filename:
        raise RelevanceLabelSidecarIntegrityError(
            "sidecar source_packet_filename is required"
        )
    sidecar_dir = os.path.dirname(validate_local_path(sidecar_path, must_exist=True))
    return validate_local_path(os.path.join(sidecar_dir, filename), must_exist=True)


def analyze_sidecar_labels(
    sidecar: Mapping[str, Any],
    *,
    source_packet: Mapping[str, Any],
    source_packet_hash: str,
) -> dict[str, Any]:
    labels = sidecar.get("labels")
    if not isinstance(labels, list):
        raise RelevanceLabelSidecarIntegrityError("sidecar labels must be a list")

    expected_pairs = {
        str(pair["pair_id"]): pair for pair in source_packet["pairs"]
    }
    seen_pair_ids: set[str] = set()
    labeled_count = 0
    unlabeled_count = 0
    invalid_label_count = 0
    duplicate_pair_count = 0
    missing_or_unknown_pair_count = 0

    for entry in labels:
        if not isinstance(entry, dict):
            missing_or_unknown_pair_count += 1
            continue
        pair_id = str(entry.get("pair_id") or "")
        if not pair_id:
            missing_or_unknown_pair_count += 1
            continue
        if pair_id in seen_pair_ids:
            duplicate_pair_count += 1
        else:
            seen_pair_ids.add(pair_id)

        if pair_id not in expected_pairs:
            missing_or_unknown_pair_count += 1
            continue

        label_value = entry.get("relevance_label")
        if label_value is None or str(label_value).strip() == "":
            unlabeled_count += 1
        elif str(label_value) not in ALLOWED_RELEVANCE_LABELS:
            invalid_label_count += 1
        else:
            labeled_count += 1

    for expected_pair_id in expected_pairs:
        if expected_pair_id not in seen_pair_ids:
            missing_or_unknown_pair_count += 1

    stored_source_hash = str(sidecar.get("source_packet_hash") or "").strip().lower()
    source_packet_hash_matches = stored_source_hash == source_packet_hash
    all_expected_pairs_present = len(seen_pair_ids.intersection(expected_pairs)) == len(
        expected_pairs
    ) and len(seen_pair_ids) == len(expected_pairs)

    return {
        "total_pairs": len(labels),
        "labeled_count": labeled_count,
        "unlabeled_count": unlabeled_count,
        "invalid_label_count": invalid_label_count,
        "duplicate_pair_count": duplicate_pair_count,
        "missing_or_unknown_pair_count": missing_or_unknown_pair_count,
        "source_packet_hash_matches": source_packet_hash_matches,
        "all_expected_pairs_present": all_expected_pairs_present,
        "expected_pair_count": EXPECTED_PAIR_COUNT,
    }


def format_validation_report(report: Mapping[str, Any]) -> str:
    return json.dumps(dict(report), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def format_operation_summary(result: Mapping[str, Any]) -> str:
    return json.dumps(dict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def format_dry_run_summary(operation: str, details: Mapping[str, Any]) -> str:
    payload = {"operation": operation, "execute": False, **details}
    return format_operation_summary(payload)


def initialize_label_sidecar(
    *,
    packet_path: str,
    sidecar_path: Optional[str],
    overwrite: bool,
    execute: bool,
) -> dict[str, Any]:
    normalized_packet_path, packet = load_verified_source_packet(packet_path)
    source_packet_hash = verify_source_packet_integrity(packet)
    if int(packet.get("pair_count") or 0) != EXPECTED_PAIR_COUNT:
        raise RelevanceLabelSidecarIntegrityError(
            f"source packet must contain exactly {EXPECTED_PAIR_COUNT} pairs"
        )

    output_path = sidecar_path or default_sidecar_path(
        source_packet_hash=source_packet_hash,
        directory=os.path.dirname(normalized_packet_path),
    )
    output_path = validate_local_path(output_path)
    if os.path.exists(output_path) and not overwrite:
        raise RelevanceLabelSidecarStateError(
            "label sidecar already exists; pass --overwrite to replace it"
        )

    if not execute:
        return {
            "final_status": "planned",
            "operation": "initialize",
            "source_packet_path": normalized_packet_path,
            "sidecar_path": output_path,
            "pair_count": EXPECTED_PAIR_COUNT,
            "source_packet_hash": source_packet_hash,
        }

    sidecar = build_sidecar_payload(
        source_packet=packet,
        source_packet_path=normalized_packet_path,
        source_packet_hash=source_packet_hash,
    )
    atomic_write_json(output_path, sidecar)
    return {
        "final_status": "success",
        "operation": "initialize",
        "sidecar_path": output_path,
        "pair_count": len(sidecar["labels"]),
        "source_packet_hash": source_packet_hash,
        "replay_content_set_hash": sidecar["replay_content_set_hash"],
    }


def validate_label_sidecar(*, labels_path: str) -> dict[str, Any]:
    normalized_labels_path = validate_local_path(labels_path, must_exist=True)
    sidecar = load_json_file(normalized_labels_path)
    source_packet_path = resolve_source_packet_path(sidecar, sidecar_path=normalized_labels_path)
    _, source_packet = load_verified_source_packet(source_packet_path)
    source_packet_hash = verify_source_packet_integrity(source_packet)

    if str(sidecar.get("schema_version")) != LABEL_SIDECAR_SCHEMA_VERSION:
        raise RelevanceLabelSidecarIntegrityError("unsupported sidecar schema_version")

    report = analyze_sidecar_labels(
        sidecar,
        source_packet=source_packet,
        source_packet_hash=source_packet_hash,
    )
    report["final_status"] = "validated"
    report["operation"] = "validate"
    report["labels_path"] = normalized_labels_path
    report["source_packet_hash"] = source_packet_hash
    report["finalized"] = sidecar.get("finalized_at") is not None
    return report


def finalize_label_sidecar(*, labels_path: str, execute: bool) -> dict[str, Any]:
    normalized_labels_path = validate_local_path(labels_path, must_exist=True)
    sidecar = load_json_file(normalized_labels_path)

    if sidecar.get("finalized_at") is not None:
        raise RelevanceLabelSidecarStateError(
            "label sidecar is already finalized and cannot be modified"
        )

    source_packet_path = resolve_source_packet_path(sidecar, sidecar_path=normalized_labels_path)
    _, source_packet = load_verified_source_packet(source_packet_path)
    source_packet_hash = verify_source_packet_integrity(source_packet)

    report = analyze_sidecar_labels(
        sidecar,
        source_packet=source_packet,
        source_packet_hash=source_packet_hash,
    )
    if not report["source_packet_hash_matches"]:
        raise RelevanceLabelSidecarIntegrityError(
            "sidecar source_packet_hash does not match verified source packet"
        )
    if not report["all_expected_pairs_present"]:
        raise RelevanceLabelSidecarIntegrityError(
            "sidecar must contain exactly the expected review pairs"
        )
    if report["duplicate_pair_count"] > 0:
        raise RelevanceLabelSidecarIntegrityError("sidecar contains duplicate pair IDs")
    if report["missing_or_unknown_pair_count"] > 0:
        raise RelevanceLabelSidecarIntegrityError(
            "sidecar contains missing or unknown pair IDs"
        )
    if report["invalid_label_count"] > 0 or report["unlabeled_count"] > 0:
        raise RelevanceLabelSidecarIntegrityError(
            "all pairs must have one allowed non-empty relevance_label before finalization"
        )
    if int(sidecar.get("pair_count") or 0) != EXPECTED_PAIR_COUNT:
        raise RelevanceLabelSidecarIntegrityError(
            f"sidecar pair_count must be exactly {EXPECTED_PAIR_COUNT}"
        )

    label_set_hash = compute_label_set_hash(sidecar["labels"])
    if not execute:
        return {
            "final_status": "planned",
            "operation": "finalize",
            "labels_path": normalized_labels_path,
            "pair_count": EXPECTED_PAIR_COUNT,
            "label_set_hash": label_set_hash,
            "source_packet_hash": source_packet_hash,
        }

    finalized = dict(sidecar)
    finalized["finalized_at"] = utc_now_iso()
    finalized["label_set_hash"] = label_set_hash
    atomic_write_json(normalized_labels_path, finalized)
    return {
        "final_status": "success",
        "operation": "finalize",
        "labels_path": normalized_labels_path,
        "pair_count": EXPECTED_PAIR_COUNT,
        "label_set_hash": label_set_hash,
        "source_packet_hash": source_packet_hash,
        "finalized_at": finalized["finalized_at"],
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Initialize, validate, or finalize local relevance label sidecars "
            "for immutable V48 review packets."
        ),
    )
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform initialize or finalize writes (validate is always read-only)",
    )
    parser.add_argument(
        "--packet",
        default=None,
        help="Immutable source review packet path (required for --initialize)",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help="Label sidecar path (required for --validate and --finalize)",
    )
    parser.add_argument(
        "--sidecar",
        default=None,
        help="Optional sidecar output path for --initialize",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing sidecar during --initialize --execute",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    selected = int(args.initialize) + int(args.validate) + int(args.finalize)
    if selected != 1:
        print(
            format_dry_run_summary(
                "help",
                {
                    "message": (
                        "Specify exactly one of --initialize, --validate, or --finalize"
                    ),
                    "local_root": LOCAL_REVIEW_ROOT,
                    "expected_pair_count": EXPECTED_PAIR_COUNT,
                },
            )
        )
        return 0

    try:
        if args.initialize:
            if not args.packet:
                raise RelevanceLabelSidecarConfigError(
                    "--packet is required with --initialize"
                )
            result = initialize_label_sidecar(
                packet_path=args.packet,
                sidecar_path=args.sidecar,
                overwrite=bool(args.overwrite),
                execute=bool(args.execute),
            )
        elif args.validate:
            if not args.labels:
                raise RelevanceLabelSidecarConfigError(
                    "--labels is required with --validate"
                )
            result = validate_label_sidecar(labels_path=args.labels)
        else:
            if not args.labels:
                raise RelevanceLabelSidecarConfigError(
                    "--labels is required with --finalize"
                )
            result = finalize_label_sidecar(
                labels_path=args.labels,
                execute=bool(args.execute),
            )
    except RelevanceLabelSidecarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.validate:
        print(format_validation_report(result))
    else:
        print(format_operation_summary(result))
    return 0 if result.get("final_status") in {"success", "validated", "planned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
