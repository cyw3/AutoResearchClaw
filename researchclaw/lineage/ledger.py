"""Number lineage ledger (R2: every number links to an artifact + log hash).

The ledger is the auditable half of anti-fabrication:

* ``VerifiedRegistry`` answers "is this number allowed in the paper?" in memory;
* ``LineageLedger`` answers "which file, on which iteration, produced it, and is
  that file still byte-identical?" on disk (``artifacts/<run>/lineage.jsonl``).

Rejection itself is delegated to :func:`researchclaw.pipeline.paper_verifier.verify_paper`
so there is exactly one rule for what counts as an unverified number.

Known limit (documented, not hidden): a REFINE cycle rewrites
``stage-14*/experiment_summary.json``.  Entries carry the iteration number and
the artifact hash so the rewrite is *detectable* via :meth:`LineageLedger.check_hashes`;
making it impossible would require per-iteration immutable copies, which this MVP
does not create.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from researchclaw.pipeline.verified_registry import VerifiedRegistry

logger = logging.getLogger(__name__)

LINEAGE_FILENAME = "lineage.jsonl"
_SHA_CHUNK = 64 * 1024
_ITERATION_RE = re.compile(r"_v(\d+)$")
_SUMMARY_STATS = ("min", "max", "mean")


def hash_file(path: Path) -> str:
    """Return ``sha256:<hex>`` for *path* (empty string if unreadable)."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(_SHA_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return "sha256:" + digest.hexdigest()


def _iteration_of(path: Path) -> int:
    """Parse the REFINE iteration from a ``stage-14_v3``-style path."""
    for name in (path.parent.name, path.stem):
        match = _ITERATION_RE.search(name)
        if match:
            return int(match.group(1))
    return 0


def _numeric(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


@dataclass(frozen=True)
class LineageEntry:
    """One reportable number and the artifact that backs it."""

    claim: str
    value: float
    metric_key: str
    source_stage: int
    artifact: str
    hash: str
    run_id: str = ""
    iteration: int = 0
    cited_norms: tuple[str, ...] = ("R2",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "value": self.value,
            "metric_key": self.metric_key,
            "source_stage": self.source_stage,
            "artifact": self.artifact,
            "hash": self.hash,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "cited_norms": list(self.cited_norms),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LineageEntry":
        return cls(
            claim=str(payload.get("claim", "")),
            value=float(payload.get("value", 0.0) or 0.0),
            metric_key=str(payload.get("metric_key", "")),
            source_stage=int(payload.get("source_stage", 14) or 14),
            artifact=str(payload.get("artifact", "")),
            hash=str(payload.get("hash", "")),
            run_id=str(payload.get("run_id", "")),
            iteration=int(payload.get("iteration", 0) or 0),
            cited_norms=tuple(payload.get("cited_norms") or ("R2",)),
        )


class LineageLedger:
    """Ordered set of :class:`LineageEntry` with JSONL persistence."""

    def __init__(
        self,
        entries: list[LineageEntry] | None = None,
        *,
        run_id: str = "",
    ) -> None:
        self.entries: list[LineageEntry] = list(entries or [])
        self.run_id = run_id

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(entry.to_dict(), ensure_ascii=False) for entry in self.entries
        ) + ("\n" if self.entries else "")

    def write(self, path: Path) -> Path:
        """Atomically write the ledger as JSONL."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(self.to_jsonl())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> "LineageLedger":
        """Load a JSONL ledger; malformed lines are skipped, never fatal."""
        path = Path(path)
        entries: list[LineageEntry] = []
        run_id = ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return cls(run_id=path.parent.name)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("LineageLedger: skipping malformed line in %s", path)
                continue
            if not isinstance(payload, dict):
                continue
            entry = LineageEntry.from_dict(payload)
            entries.append(entry)
            run_id = run_id or entry.run_id
        return cls(entries, run_id=run_id or path.parent.name)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def add(self, entry: LineageEntry) -> None:
        self.entries.append(entry)

    @classmethod
    def from_run_dir(cls, run_dir: Path, *, run_id: str = "") -> "LineageLedger":
        """Build the ledger from the promoted experiment summary of *run_dir*.

        ``best_run.metrics`` entries point at ``stage-12*/runs/run-1.json`` when
        that raw artifact exists (source stage 12); aggregated numbers point at
        the summary itself (stage 14).  Every entry carries the hash of the file
        it points to.
        """
        run_dir = Path(run_dir)
        ledger = cls(run_id=run_id or run_dir.name)
        summary_path = _summary_path(run_dir)
        if summary_path is None:
            return ledger
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("LineageLedger: unreadable %s", summary_path)
            return ledger
        if not isinstance(summary, dict):
            return ledger

        summary_hash = hash_file(summary_path)
        rel_summary = _rel(summary_path, run_dir)
        raw_path = _raw_run_path(run_dir)
        raw_hash = hash_file(raw_path) if raw_path else summary_hash
        rel_raw = _rel(raw_path, run_dir) if raw_path else rel_summary
        iteration = _iteration_of(summary_path)

        def _entry(
            claim: str,
            value: float,
            metric_key: str,
            artifact: str,
            digest: str,
            stage: int,
        ) -> LineageEntry:
            return LineageEntry(
                claim=claim,
                value=float(value),
                metric_key=metric_key,
                source_stage=stage,
                artifact=artifact,
                hash=digest,
                run_id=ledger.run_id,
                iteration=iteration,
            )

        for key, value in ((summary.get("best_run") or {}).get("metrics") or {}).items():
            if not _numeric(value):
                continue
            ledger.add(
                _entry(
                    f"best_run.metrics.{key}",
                    value,
                    key,
                    f"{rel_raw}#metrics.{key}",
                    raw_hash,
                    12 if raw_path else 14,
                )
            )

        for cond, cond_data in (summary.get("condition_summaries") or {}).items():
            if not isinstance(cond_data, dict):
                continue
            for metric, value in (cond_data.get("metrics") or {}).items():
                if not _numeric(value):
                    continue
                ledger.add(
                    _entry(
                        f"condition_summaries.{cond}.metrics.{metric}",
                        value,
                        metric,
                        f"{rel_summary}#condition_summaries.{cond}.metrics.{metric}",
                        summary_hash,
                        14,
                    )
                )

        for key, stats in (summary.get("metrics_summary") or {}).items():
            if not isinstance(stats, dict):
                continue
            for stat in _SUMMARY_STATS:
                value = stats.get(stat)
                if not _numeric(value):
                    continue
                ledger.add(
                    _entry(
                        f"metrics_summary.{key}.{stat}",
                        value,
                        key,
                        f"{rel_summary}#metrics_summary.{key}.{stat}",
                        summary_hash,
                        14,
                    )
                )
        return ledger

    # ------------------------------------------------------------------
    # Consumption
    # ------------------------------------------------------------------
    def to_registry(self) -> VerifiedRegistry:
        """Materialise a :class:`VerifiedRegistry` whose sources are artifact refs."""
        registry = VerifiedRegistry()
        for entry in self.entries:
            registry.add_value(entry.value, f"{entry.claim} [{entry.artifact}]")
        return registry

    def verify(self, tex_text: str, *, tolerance: float = 0.01):
        """Run the shared paper verifier against this ledger.

        Returns :class:`researchclaw.pipeline.paper_verifier.VerificationResult`.
        """
        from researchclaw.pipeline.paper_verifier import verify_paper

        return verify_paper(tex_text, self.to_registry(), tolerance=tolerance)

    def provenance(self, number: float, tolerance: float = 0.01) -> LineageEntry | None:
        """Return the entry backing *number* (same tolerance semantics as the registry)."""
        for entry in self.entries:
            if entry.value == 0.0:
                if abs(number) < 1e-6:
                    return entry
            elif abs(number - entry.value) / max(abs(entry.value), 1e-9) <= tolerance:
                return entry
        return None

    def check_hashes(self, run_dir: Path) -> list[str]:
        """Return ``metric_key: reason`` for artifacts that changed or vanished.

        A non-empty result means the numbers were produced by an artifact that
        has since been rewritten (typically a REFINE rerun); the paper must not
        cite them without re-deriving the ledger.
        """
        run_dir = Path(run_dir)
        stale: list[str] = []
        cache: dict[str, str] = {}
        for entry in self.entries:
            if not entry.hash or "#" not in entry.artifact:
                continue
            rel = entry.artifact.split("#", 1)[0]
            if not rel:
                continue
            if rel not in cache:
                path = run_dir / rel
                cache[rel] = hash_file(path) if path.is_file() else ""
            actual = cache[rel]
            if not actual:
                stale.append(f"{entry.metric_key}: artifact missing ({rel})")
            elif actual != entry.hash:
                stale.append(f"{entry.metric_key}: hash mismatch ({rel})")
        return stale

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self.entries),
            "run_id": self.run_id,
            "iterations": sorted({entry.iteration for entry in self.entries}),
            "artifacts": len({entry.artifact.split('#', 1)[0] for entry in self.entries}),
        }


def _rel(path: Path, run_dir: Path) -> str:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


def _summary_path(run_dir: Path) -> Path | None:
    """Promoted best summary first, then the newest ``stage-14*`` summary."""
    best = run_dir / "experiment_summary_best.json"
    if best.is_file():
        return best
    candidates = sorted(run_dir.glob("stage-14*/experiment_summary.json"))
    return candidates[-1] if candidates else None


def _raw_run_path(run_dir: Path) -> Path | None:
    """Newest raw stage-12 run payload (source of ``best_run.metrics``)."""
    for name in ("run-1.json", "run_1.json"):
        candidates = sorted(run_dir.glob(f"stage-12*/runs/{name}"))
        if candidates:
            return candidates[-1]
    return None
