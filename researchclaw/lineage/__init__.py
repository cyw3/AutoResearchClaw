"""Number lineage: every reportable paper number traces to an artifact.

M6/A3 MVP.  The rejection rule itself stays in
:mod:`researchclaw.pipeline.paper_verifier` — this package only records *where*
each number came from and whether that artifact is still byte-identical.
"""

from __future__ import annotations

from researchclaw.lineage.ledger import (
    LINEAGE_FILENAME,
    LineageEntry,
    LineageLedger,
    hash_file,
)

__all__ = ["LINEAGE_FILENAME", "LineageEntry", "LineageLedger", "hash_file"]
