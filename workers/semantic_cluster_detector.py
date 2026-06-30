"""
CertBound semantic concept-cluster detector (Phase 1 — pure logic).

Compares immutable question-version snapshots within one certification using
injected embedding vectors. Builds complete cosine-similarity matrices for
three text views, forms preliminary connected components, then refines
clusters with a medoid cohesion rule to avoid chain-merging errors.

No database access, network calls, or live question mutation.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from workers.duplicate_question_detector import (
    detect_duplicate_question_stems,
    normalize_question_stem,
)
from workers.finding_policy import normalize_deterministic_finding

SIGNAL_STEM = "stem"
SIGNAL_FULL = "full"
SIGNAL_CORRECT = "correct"

SIGNAL_NAMES = (SIGNAL_STEM, SIGNAL_FULL, SIGNAL_CORRECT)

EmbeddingFn = Callable[[Sequence[str]], Sequence[Sequence[float]]]


@dataclass(frozen=True)
class SignalSimilarityStats:
    min: float
    max: float
    average: float


@dataclass(frozen=True)
class SemanticClusterThresholds:
    """Configurable thresholds — callers supply production values explicitly."""

    stem_edge_threshold: float
    full_edge_threshold: float
    correct_edge_threshold: float
    cohesion_min_similarity: float
    cohesion_signal: str = SIGNAL_FULL

    def __post_init__(self) -> None:
        if self.cohesion_signal not in SIGNAL_NAMES:
            raise ValueError(
                f"cohesion_signal must be one of {SIGNAL_NAMES!r}, "
                f"got {self.cohesion_signal!r}"
            )
        for name, value in (
            ("stem_edge_threshold", self.stem_edge_threshold),
            ("full_edge_threshold", self.full_edge_threshold),
            ("correct_edge_threshold", self.correct_edge_threshold),
            ("cohesion_min_similarity", self.cohesion_min_similarity),
        ):
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be in [0, 1], got {value}")


@dataclass(frozen=True)
class SemanticCluster:
    cluster_id: str
    certification_exam_name: str
    question_version_ids: Tuple[str, ...]
    question_ids: Tuple[int, ...]
    cluster_size: int
    stem_similarity: SignalSimilarityStats
    full_similarity: SignalSimilarityStats
    correct_similarity: SignalSimilarityStats
    categories: Tuple[str, ...]
    concept_keys: Tuple[str, ...]
    is_review_candidate: bool


@dataclass
class SemanticClusterDetectionResult:
    certification_exam_name: str
    clusters: List[SemanticCluster] = field(default_factory=list)
    allowed_clusters: List[SemanticCluster] = field(default_factory=list)
    review_candidates: List[SemanticCluster] = field(default_factory=list)
    lexical_findings: List[dict] = field(default_factory=list)


def derive_cluster_id(question_version_ids: Sequence[str]) -> str:
    """Deterministic cluster id from sorted immutable version ids."""
    ordered = tuple(sorted(str(value).strip() for value in question_version_ids if value))
    if not ordered:
        raise ValueError("question_version_ids must not be empty")
    digest = hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()
    return digest


def build_stem_text(entry: Mapping[str, object]) -> str:
    """Stem-only view from an immutable snapshot entry."""
    snapshot = _snapshot_from_entry(entry)
    return str(snapshot.get("question_text") or "")


def build_full_question_text(entry: Mapping[str, object]) -> str:
    """Stem plus all answer options in stable display order."""
    snapshot = _snapshot_from_entry(entry)
    parts = [str(snapshot.get("question_text") or "")]
    for option in _ordered_options(snapshot):
        label = str(option.get("option_label") or "").strip()
        text = str(option.get("option_text") or "").strip()
        if label and text:
            parts.append(f"{label}. {text}")
        elif text:
            parts.append(text)
    return "\n".join(part for part in parts if part)


def build_correct_answer_text(entry: Mapping[str, object]) -> str:
    """Correct-answer text only (supports multiple correct options)."""
    snapshot = _snapshot_from_entry(entry)
    correct_texts = [
        str(option.get("option_text") or "").strip()
        for option in _ordered_options(snapshot)
        if bool(option.get("is_correct"))
    ]
    return " | ".join(text for text in correct_texts if text)


def cosine_similarity_matrix(
    vectors: Sequence[Sequence[float]],
) -> List[List[float]]:
    """Return a complete N×N cosine-similarity matrix."""
    count = len(vectors)
    if count == 0:
        return []
    matrix = [[0.0] * count for _ in range(count)]
    norms = [math.sqrt(sum(value * value for value in vector)) for vector in vectors]
    for row_index in range(count):
        matrix[row_index][row_index] = 1.0
        for col_index in range(row_index + 1, count):
            if norms[row_index] == 0.0 or norms[col_index] == 0.0:
                similarity = 0.0
            else:
                dot = sum(
                    left * right
                    for left, right in zip(vectors[row_index], vectors[col_index])
                )
                similarity = dot / (norms[row_index] * norms[col_index])
            matrix[row_index][col_index] = similarity
            matrix[col_index][row_index] = similarity
    return matrix


def detect_semantic_clusters_for_certification(
    entries: Sequence[Mapping[str, object]],
    *,
    embed_fn: EmbeddingFn,
    thresholds: SemanticClusterThresholds,
    ruleset_version: str = "1.0.0",
    near_exact_threshold: Optional[float] = None,
) -> SemanticClusterDetectionResult:
    """Detect semantic clusters for one certification from preloaded snapshots."""
    normalized_entries = [_normalize_entry(entry) for entry in entries]
    if not normalized_entries:
        raise ValueError("entries must not be empty")

    certification = normalized_entries[0]["certification_exam_name"]
    for entry in normalized_entries[1:]:
        if entry["certification_exam_name"] != certification:
            raise ValueError("all entries must belong to the same certification")

    ordered_entries = sorted(
        normalized_entries,
        key=lambda item: (item["question_version_id"], item["question_id"]),
    )

    stem_texts = [build_stem_text(entry) for entry in ordered_entries]
    full_texts = [build_full_question_text(entry) for entry in ordered_entries]
    correct_texts = [build_correct_answer_text(entry) for entry in ordered_entries]

    stem_vectors = list(embed_fn(stem_texts))
    full_vectors = list(embed_fn(full_texts))
    correct_vectors = list(embed_fn(correct_texts))
    _validate_vector_batch(stem_vectors, len(ordered_entries), "stem")
    _validate_vector_batch(full_vectors, len(ordered_entries), "full")
    _validate_vector_batch(correct_vectors, len(ordered_entries), "correct")

    matrices = {
        SIGNAL_STEM: cosine_similarity_matrix(stem_vectors),
        SIGNAL_FULL: cosine_similarity_matrix(full_vectors),
        SIGNAL_CORRECT: cosine_similarity_matrix(correct_vectors),
    }

    preliminary_components = _connected_components(len(ordered_entries), matrices, thresholds)
    refined_components = [
        _refine_component_with_cohesion(component, matrices, thresholds)
        for component in preliminary_components
    ]

    clusters: List[SemanticCluster] = []
    for component in refined_components:
        if len(component) < 2:
            continue
        cluster = _build_cluster(ordered_entries, component, matrices, certification)
        clusters.append(cluster)

    clusters.sort(key=lambda item: item.cluster_id)
    allowed_clusters = [cluster for cluster in clusters if 2 <= cluster.cluster_size <= 3]
    review_candidates = [cluster for cluster in clusters if cluster.is_review_candidate]

    lexical_rows = [
        {
            "question_version_id": entry["question_version_id"],
            "certification_exam_name": entry["certification_exam_name"],
            "question_text": build_stem_text(entry),
        }
        for entry in ordered_entries
    ]
    lexical_kwargs = {"ruleset_version": ruleset_version}
    if near_exact_threshold is not None:
        lexical_kwargs["near_exact_threshold"] = near_exact_threshold
    lexical_findings = detect_duplicate_question_stems(lexical_rows, **lexical_kwargs)

    return SemanticClusterDetectionResult(
        certification_exam_name=certification,
        clusters=clusters,
        allowed_clusters=allowed_clusters,
        review_candidates=review_candidates,
        lexical_findings=lexical_findings,
    )


def detect_semantic_clusters(
    entries: Sequence[Mapping[str, object]],
    *,
    embed_fn: EmbeddingFn,
    thresholds: SemanticClusterThresholds,
    ruleset_version: str = "1.0.0",
    near_exact_threshold: Optional[float] = None,
) -> List[SemanticClusterDetectionResult]:
    """Detect semantic clusters grouped by certification exam name."""
    grouped: Dict[str, List[Mapping[str, object]]] = {}
    for entry in entries:
        normalized = _normalize_entry(entry)
        grouped.setdefault(normalized["certification_exam_name"], []).append(normalized)

    results: List[SemanticClusterDetectionResult] = []
    for certification in sorted(grouped):
        results.append(
            detect_semantic_clusters_for_certification(
                grouped[certification],
                embed_fn=embed_fn,
                thresholds=thresholds,
                ruleset_version=ruleset_version,
                near_exact_threshold=near_exact_threshold,
            )
        )
    return results


def _snapshot_from_entry(entry: Mapping[str, object]) -> Mapping[str, object]:
    snapshot = entry.get("snapshot")
    if isinstance(snapshot, Mapping):
        return snapshot
    return entry


def _ordered_options(snapshot: Mapping[str, object]) -> List[Mapping[str, object]]:
    options = snapshot.get("options") or []
    if not isinstance(options, list):
        return []
    typed = [option for option in options if isinstance(option, Mapping)]
    return sorted(
        typed,
        key=lambda option: (
            option.get("display_order")
            if option.get("display_order") is not None
            else 0,
            str(option.get("option_label") or ""),
        ),
    )


def _normalize_entry(entry: Mapping[str, object]) -> Dict[str, object]:
    qvid = str(entry.get("question_version_id") or "").strip()
    cert = str(
        entry.get("certification_exam_name")
        or entry.get("certification_id")
        or entry.get("exam_name")
        or ""
    ).strip()
    question_id = entry.get("question_id")
    if not qvid:
        raise ValueError("each entry must include question_version_id")
    if not cert:
        raise ValueError("each entry must include certification_exam_name")
    if question_id is None:
        raise ValueError("each entry must include question_id")

    category = entry.get("category")
    if category is None:
        snapshot = _snapshot_from_entry(entry)
        category = snapshot.get("category")
    concept_key = entry.get("concept_key")
    if concept_key is None:
        snapshot = _snapshot_from_entry(entry)
        concept_key = snapshot.get("concept_key")

    return {
        "question_version_id": qvid,
        "certification_exam_name": cert,
        "question_id": int(question_id),
        "category": None if category in (None, "") else str(category),
        "concept_key": None if concept_key in (None, "") else str(concept_key),
        "snapshot": dict(_snapshot_from_entry(entry)),
    }


def _validate_vector_batch(
    vectors: Sequence[Sequence[float]],
    expected_count: int,
    label: str,
) -> None:
    if len(vectors) != expected_count:
        raise ValueError(
            f"embed_fn returned {len(vectors)} {label} vectors, expected {expected_count}"
        )
    for index, vector in enumerate(vectors):
        if not vector:
            raise ValueError(f"embed_fn returned empty {label} vector at index {index}")


def _edge_exists(
    left_index: int,
    right_index: int,
    matrices: Mapping[str, List[List[float]]],
    thresholds: SemanticClusterThresholds,
) -> bool:
    if matrices[SIGNAL_STEM][left_index][right_index] >= thresholds.stem_edge_threshold:
        return True
    if matrices[SIGNAL_FULL][left_index][right_index] >= thresholds.full_edge_threshold:
        return True
    return False


def _connected_components(
    size: int,
    matrices: Mapping[str, List[List[float]]],
    thresholds: SemanticClusterThresholds,
) -> List[List[int]]:
    parent = list(range(size))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(size):
        for right in range(left + 1, size):
            if _edge_exists(left, right, matrices, thresholds):
                union(left, right)

    grouped: Dict[int, List[int]] = {}
    for index in range(size):
        grouped.setdefault(find(index), []).append(index)

    return [sorted(members) for members in grouped.values()]


def _select_medoid(
    member_indices: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> int:
    if len(member_indices) == 1:
        return member_indices[0]
    best_index = member_indices[0]
    best_score = float("-inf")
    for candidate in member_indices:
        others = [index for index in member_indices if index != candidate]
        if not others:
            return candidate
        average = sum(matrix[candidate][other] for other in others) / len(others)
        if average > best_score:
            best_score = average
            best_index = candidate
    return best_index


def _minimum_pairwise_similarity(
    member_indices: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> float:
    if len(member_indices) < 2:
        return 1.0
    return min(
        matrix[left][right]
        for left in member_indices
        for right in member_indices
        if left < right
    )


def _refine_component_with_cohesion(
    member_indices: Sequence[int],
    matrices: Mapping[str, List[List[float]]],
    thresholds: SemanticClusterThresholds,
) -> List[int]:
    """Apply medoid cohesion and split chain-merged members."""
    members = list(member_indices)
    if len(members) < 2:
        return members

    cohesion_matrix = matrices[thresholds.cohesion_signal]

    while len(members) >= 2:
        medoid = _select_medoid(members, cohesion_matrix)
        cohesive = [
            index
            for index in members
            if index == medoid
            or cohesion_matrix[index][medoid] >= thresholds.cohesion_min_similarity
        ]
        if len(cohesive) < 2:
            return [medoid]

        if _minimum_pairwise_similarity(cohesive, cohesion_matrix) >= thresholds.cohesion_min_similarity:
            return sorted(cohesive)

        member_to_remove = _select_member_to_split(
            cohesive,
            medoid,
            cohesion_matrix,
            thresholds.cohesion_min_similarity,
        )
        members = [index for index in cohesive if index != member_to_remove]
        if len(members) < 2:
            return sorted(members) if members else [medoid]

    return members


def _select_member_to_split(
    member_indices: Sequence[int],
    medoid: int,
    matrix: Sequence[Sequence[float]],
    cohesion_min: float,
) -> int:
    """Remove the failing endpoint with lowest similarity to medoid."""
    failing_members = set()
    for left in member_indices:
        for right in member_indices:
            if left >= right:
                continue
            if matrix[left][right] < cohesion_min:
                failing_members.add(left)
                failing_members.add(right)

    candidates = [index for index in failing_members if index != medoid]
    if not candidates:
        candidates = [index for index in member_indices if index != medoid]
    if not candidates:
        return medoid
    return min(candidates, key=lambda index: (matrix[index][medoid], index))


def _pairwise_stats(
    member_indices: Sequence[int],
    matrix: Sequence[Sequence[float]],
) -> SignalSimilarityStats:
    if len(member_indices) < 2:
        return SignalSimilarityStats(min=1.0, max=1.0, average=1.0)
    values = [
        matrix[left][right]
        for left in member_indices
        for right in member_indices
        if left < right
    ]
    return SignalSimilarityStats(
        min=min(values),
        max=max(values),
        average=sum(values) / len(values),
    )


def _build_cluster(
    entries: Sequence[Mapping[str, object]],
    member_indices: Sequence[int],
    matrices: Mapping[str, List[List[float]]],
    certification_exam_name: str,
) -> SemanticCluster:
    selected = [entries[index] for index in member_indices]
    paired = sorted(
        (
            (str(entry["question_version_id"]), int(entry["question_id"]))
            for entry in selected
        ),
        key=lambda item: item[0],
    )
    question_version_ids = tuple(qvid for qvid, _qid in paired)
    question_ids = tuple(qid for _qvid, qid in paired)
    categories = tuple(
        sorted(
            {
                str(entry["category"])
                for entry in selected
                if entry.get("category")
            }
        )
    )
    concept_keys = tuple(
        sorted(
            {
                str(entry["concept_key"])
                for entry in selected
                if entry.get("concept_key")
            }
        )
    )
    cluster_size = len(question_version_ids)
    return SemanticCluster(
        cluster_id=derive_cluster_id(question_version_ids),
        certification_exam_name=certification_exam_name,
        question_version_ids=question_version_ids,
        question_ids=question_ids,
        cluster_size=cluster_size,
        stem_similarity=_pairwise_stats(member_indices, matrices[SIGNAL_STEM]),
        full_similarity=_pairwise_stats(member_indices, matrices[SIGNAL_FULL]),
        correct_similarity=_pairwise_stats(member_indices, matrices[SIGNAL_CORRECT]),
        categories=categories,
        concept_keys=concept_keys,
        is_review_candidate=cluster_size > 3,
    )


def count_pairwise_edges(
    size: int,
    matrices: Mapping[str, List[List[float]]],
    thresholds: SemanticClusterThresholds,
) -> int:
    """Return the number of undirected candidate edges (testing helper)."""
    count = 0
    for left in range(size):
        for right in range(left + 1, size):
            if _edge_exists(left, right, matrices, thresholds):
                count += 1
    return count


def normalized_stem(entry: Mapping[str, object]) -> str:
    """Expose normalized stem via shared duplicate-detector helper."""
    return normalize_question_stem(build_stem_text(entry))


FINDING_CODE_SEMANTIC_OVERSIZE = "SEMANTIC_CONCEPT_CLUSTER_OVERSIZE"
SCAN_TYPE_SEMANTIC_CLUSTER = "semantic_concept_cluster"
DETECTOR_NAME = "semantic_cluster_detector"
DETECTOR_VERSION = "1.0.0"
DEFAULT_RULESET_VERSION = "1.0.0"
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"
MAX_ALLOWED_CLUSTER_SIZE = 3

DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS = SemanticClusterThresholds(
    stem_edge_threshold=0.82,
    full_edge_threshold=0.80,
    correct_edge_threshold=0.85,
    cohesion_min_similarity=0.77,
    cohesion_signal=SIGNAL_FULL,
)

_MODEL_CACHE: Dict[str, object] = {}


def build_semantic_cluster_thresholds(
    *,
    stem_edge_threshold: Optional[float] = None,
    full_edge_threshold: Optional[float] = None,
    correct_edge_threshold: Optional[float] = None,
    cohesion_min_similarity: Optional[float] = None,
    cohesion_signal: Optional[str] = None,
) -> SemanticClusterThresholds:
    """Build thresholds, falling back to production defaults for omitted values."""
    defaults = DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS
    return SemanticClusterThresholds(
        stem_edge_threshold=(
            defaults.stem_edge_threshold
            if stem_edge_threshold is None
            else stem_edge_threshold
        ),
        full_edge_threshold=(
            defaults.full_edge_threshold
            if full_edge_threshold is None
            else full_edge_threshold
        ),
        correct_edge_threshold=(
            defaults.correct_edge_threshold
            if correct_edge_threshold is None
            else correct_edge_threshold
        ),
        cohesion_min_similarity=(
            defaults.cohesion_min_similarity
            if cohesion_min_similarity is None
            else cohesion_min_similarity
        ),
        cohesion_signal=cohesion_signal or defaults.cohesion_signal,
    )


def merge_certification_entries_with_snapshots(
    rows: Sequence[dict],
    snapshots: Mapping[str, dict],
) -> List[dict]:
    """Attach immutable snapshots to certification loader rows."""
    merged: List[dict] = []
    for row in rows:
        qvid = str(row.get("question_version_id") or "").strip()
        snapshot = snapshots.get(qvid)
        if snapshot is None:
            raise ValueError(f"missing snapshot for question_version_id {qvid!r}")
        merged.append({**row, "snapshot": dict(snapshot)})
    return merged


def _stats_to_metadata(stats: SignalSimilarityStats) -> dict:
    return {
        "min": round(stats.min, 6),
        "max": round(stats.max, 6),
        "average": round(stats.average, 6),
    }


def cluster_dedupe_key(
    certification_exam_name: str,
    cluster_id: str,
    model_name: str,
    ruleset_version: str,
) -> Tuple[str, str, str, str]:
    cert = str(certification_exam_name or "").strip()
    cluster = str(cluster_id or "").strip()
    model = str(model_name or "").strip()
    ruleset = str(ruleset_version or DEFAULT_RULESET_VERSION).strip()
    if not cert or not cluster or not model:
        raise ValueError("cluster dedupe key requires certification, cluster_id, and model_name")
    return cert, cluster, model, ruleset


def cluster_key_from_finding(finding: dict) -> Optional[Tuple[str, str, str, str]]:
    metadata = finding.get("metadata") or {}
    try:
        return cluster_dedupe_key(
            str(metadata.get("certification_exam_name") or ""),
            str(metadata.get("cluster_id") or ""),
            str(metadata.get("model_name") or ""),
            str(metadata.get("ruleset_version") or DEFAULT_RULESET_VERSION),
        )
    except ValueError:
        return None


def resolve_sentence_transformers_package_version() -> str:
    try:
        from importlib.metadata import version

        return str(version("sentence-transformers"))
    except Exception:
        return "unknown"


def _resolve_model_revision(model: object) -> Optional[str]:
    card = getattr(model, "model_card_data", None)
    if card is not None:
        for attr in ("base_model_revision", "revision"):
            value = getattr(card, attr, None)
            if value:
                return str(value)
    return None


def get_model_provenance(model_name: str) -> dict:
    """Return model and package provenance, loading the model when needed."""
    model = _get_sentence_transformer(model_name)
    return {
        "model_name": model_name,
        "model_revision": _resolve_model_revision(model),
        "sentence_transformers_version": resolve_sentence_transformers_package_version(),
    }


def _get_sentence_transformer(model_name: str):
    if model_name not in _MODEL_CACHE:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


def create_sentence_transformer_embed_fn(model_name: str) -> EmbeddingFn:
    """Return an embed_fn that lazily loads SentenceTransformer on first use."""

    def embed(texts: Sequence[str]) -> List[List[float]]:
        model = _get_sentence_transformer(model_name)
        encoded = model.encode(list(texts), normalize_embeddings=True)
        return [list(row) for row in encoded]

    return embed


def build_semantic_cluster_oversize_finding(
    cluster: SemanticCluster,
    *,
    ruleset_version: str,
    model_name: str,
    model_revision: Optional[str],
    sentence_transformers_version: str,
    thresholds: SemanticClusterThresholds,
    question_count: int,
) -> dict:
    """Build one warning-level oversize semantic cluster finding."""
    metadata = {
        "scan_type": SCAN_TYPE_SEMANTIC_CLUSTER,
        "certification_exam_name": cluster.certification_exam_name,
        "certification_id": cluster.certification_exam_name,
        "model_name": model_name,
        "model_revision": model_revision,
        "sentence_transformers_version": sentence_transformers_version,
        "ruleset_version": ruleset_version,
        "stem_threshold": thresholds.stem_edge_threshold,
        "full_question_threshold": thresholds.full_edge_threshold,
        "cohesion_signal": thresholds.cohesion_signal,
        "cohesion_threshold": thresholds.cohesion_min_similarity,
        "maximum_allowed_cluster_size": MAX_ALLOWED_CLUSTER_SIZE,
        "question_count": question_count,
        "cluster_id": cluster.cluster_id,
        "question_version_ids": list(cluster.question_version_ids),
        "question_ids": list(cluster.question_ids),
        "pairwise_similarity_stats": {
            "stem": _stats_to_metadata(cluster.stem_similarity),
            "full": _stats_to_metadata(cluster.full_similarity),
            "correct": _stats_to_metadata(cluster.correct_similarity),
        },
        "cluster_size": cluster.cluster_size,
        "categories": list(cluster.categories),
        "concept_keys": list(cluster.concept_keys),
    }
    finding = {
        "finding_code": FINDING_CODE_SEMANTIC_OVERSIZE,
        "finding_type": "duplication",
        "severity": "medium",
        "title": "Semantic concept cluster exceeds allowed size",
        "description": (
            f"Certification {cluster.certification_exam_name!r} contains a semantic "
            f"concept cluster of {cluster.cluster_size} related question versions "
            f"(cluster_id={cluster.cluster_id}). Allowed cluster size is "
            f"{MAX_ALLOWED_CLUSTER_SIZE}; clusters with more members require review."
        ),
        "field_path": "question_text",
        "confidence": cluster.full_similarity.average,
        "detector_name": DETECTOR_NAME,
        "detector_version": DETECTOR_VERSION,
        "metadata": metadata,
        "evidence": [],
    }
    return normalize_deterministic_finding(finding)


def _call_rpc_list(client, name: str, params: dict) -> List[dict]:
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {name!r} failed: {result.error}")
    return result.data or []


def fetch_persisted_semantic_cluster_keys(
    client,
    *,
    certification_exam_name: str,
    ruleset_version: str,
    model_name: str,
) -> set[Tuple[str, str, str, str]]:
    """Load durable semantic cluster keys already stored in audit_findings."""
    rows = _call_rpc_list(
        client,
        "list_semantic_concept_cluster_keys_v1",
        {
            "p_certification_exam_name": certification_exam_name,
            "p_ruleset_version": ruleset_version,
            "p_model_name": model_name,
        },
    )
    keys: set[Tuple[str, str, str, str]] = set()
    for row in rows:
        cluster_id = str(row.get("cluster_id") or "").strip()
        model = str(row.get("model_name") or "").strip()
        ruleset = str(row.get("ruleset_version") or "").strip() or ruleset_version
        if not cluster_id or not model:
            continue
        try:
            keys.add(
                cluster_dedupe_key(
                    certification_exam_name,
                    cluster_id,
                    model,
                    ruleset,
                )
            )
        except ValueError:
            continue
    return keys


def filter_unpersisted_semantic_cluster_findings(
    findings: Sequence[dict],
    persisted_keys: Iterable[Tuple[str, str, str, str]],
) -> List[dict]:
    """Remove findings whose durable cluster keys already exist in audit_findings."""
    persisted = set(persisted_keys)
    filtered: List[dict] = []
    for finding in findings:
        key = cluster_key_from_finding(finding)
        if key is not None and key in persisted:
            continue
        filtered.append(finding)
    return filtered


def build_oversize_cluster_findings(
    detection: SemanticClusterDetectionResult,
    *,
    ruleset_version: str,
    model_name: str,
    model_revision: Optional[str],
    sentence_transformers_version: str,
    thresholds: SemanticClusterThresholds,
    question_count: int,
) -> List[dict]:
    """Convert oversize review clusters into normalized finding dicts."""
    findings: List[dict] = []
    for cluster in detection.review_candidates:
        if cluster.cluster_size <= MAX_ALLOWED_CLUSTER_SIZE:
            continue
        findings.append(
            build_semantic_cluster_oversize_finding(
                cluster,
                ruleset_version=ruleset_version,
                model_name=model_name,
                model_revision=model_revision,
                sentence_transformers_version=sentence_transformers_version,
                thresholds=thresholds,
                question_count=question_count,
            )
        )
    return findings


def _select_audit_anchor_question_version_id(rows: Sequence[dict]) -> str:
    ids = sorted(str(row["question_version_id"]).strip() for row in rows)
    if not ids:
        raise ValueError("cannot orchestrate semantic cluster audit with zero rows")
    return ids[0]


def orchestrate_certification_semantic_cluster_audit(
    client,
    *,
    entries: Sequence[dict],
    created_by: str,
    ruleset_version: str = DEFAULT_RULESET_VERSION,
    thresholds: Optional[SemanticClusterThresholds] = None,
    model_name: str = DEFAULT_MODEL_NAME,
    embed_fn: Optional[EmbeddingFn] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Run semantic cluster detection and persist oversize cluster findings."""
    if not entries:
        raise ValueError("entries must not be empty")

    active_thresholds = thresholds or DEFAULT_SEMANTIC_CLUSTER_THRESHOLDS
    certification_exam_name = str(entries[0].get("certification_exam_name") or "").strip()
    for entry in entries[1:]:
        cert = str(entry.get("certification_exam_name") or "").strip()
        if cert != certification_exam_name:
            raise ValueError(
                "orchestrate_certification_semantic_cluster_audit requires one certification"
            )

    provenance_holder: Dict[str, object] = {}

    def _check_fn() -> List[dict]:
        resolved_embed_fn = embed_fn or create_sentence_transformer_embed_fn(model_name)
        if embed_fn is None:
            provenance = get_model_provenance(model_name)
        else:
            provenance = {
                "model_name": model_name,
                "model_revision": None,
                "sentence_transformers_version": resolve_sentence_transformers_package_version(),
            }
        provenance_holder.update(provenance)

        detection = detect_semantic_clusters_for_certification(
            entries,
            embed_fn=resolved_embed_fn,
            thresholds=active_thresholds,
            ruleset_version=ruleset_version,
        )
        findings = build_oversize_cluster_findings(
            detection,
            ruleset_version=ruleset_version,
            model_name=str(provenance["model_name"]),
            model_revision=provenance.get("model_revision"),  # type: ignore[arg-type]
            sentence_transformers_version=str(
                provenance["sentence_transformers_version"]
            ),
            thresholds=active_thresholds,
            question_count=len(entries),
        )
        persisted_keys = fetch_persisted_semantic_cluster_keys(
            client,
            certification_exam_name=certification_exam_name,
            ruleset_version=ruleset_version,
            model_name=model_name,
        )
        return filter_unpersisted_semantic_cluster_findings(findings, persisted_keys)

    run_metadata = {
        "scan_type": SCAN_TYPE_SEMANTIC_CLUSTER,
        "certification_exam_name": certification_exam_name,
        "certification_id": certification_exam_name,
        "question_count": len(entries),
        "model_name": model_name,
        "ruleset_version": ruleset_version,
        "stem_threshold": active_thresholds.stem_edge_threshold,
        "full_question_threshold": active_thresholds.full_edge_threshold,
        "cohesion_signal": active_thresholds.cohesion_signal,
        "cohesion_threshold": active_thresholds.cohesion_min_similarity,
        "maximum_allowed_cluster_size": MAX_ALLOWED_CLUSTER_SIZE,
    }
    if metadata:
        run_metadata.update(metadata)

    from workers.audit_orchestration import orchestrate_audit  # noqa: PLC0415

    result = orchestrate_audit(
        client,
        audit_type="deterministic",
        target_question_version_id=_select_audit_anchor_question_version_id(entries),
        target_candidate_id=None,
        created_by=created_by,
        ruleset_version=ruleset_version,
        resource_snapshot={},
        metadata=run_metadata,
        check_fn=_check_fn,
    )
    if provenance_holder:
        run_metadata.update(
            {
                "model_revision": provenance_holder.get("model_revision"),
                "sentence_transformers_version": provenance_holder.get(
                    "sentence_transformers_version"
                ),
            }
        )
    return {
        **result,
        "certification_exam_name": certification_exam_name,
        "model_name": model_name,
    }
