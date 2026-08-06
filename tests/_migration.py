"""Marks for tests the 0.12 → 0.14 port has temporarily invalidated.

Phase 0 re-vendored ``tests/vectors/`` at ``v0.14``. Five modules outside the
conformance harness use vectors as ordinary fixtures — the golden file *is*
``raw-minimal.zpf``, and the provenance walk reads ``chain/*.zpf`` — so each of
them now hands a 0/14 file to a 0.12 reader and is refused at the version gate.

``raw-minimal.zpf`` reaches further than a grep for the vectors path shows:
``test_golden`` exports its bytes as ``GOLDEN`` and four other modules import
that, so the writer, the CLI and the reader all round-trip it.

Nothing about those tests is wrong, and they come back at the Phase 1 version
bump with nothing else needed: ``raw-minimal.zpf`` and ``chain/raw.zpf`` differ
from their 0.12 selves at exactly one byte, ``version_minor``, and the other two
``chain`` files differ there and in the ``input_digest`` that byte changed.
Neither carries a block or an option new in 0.13 or 0.14.

**Phase 1 deletes this module.** Every mark it holds must be gone by then;
``strict=False`` keeps the suite green in the meantime, and an ``XPASS`` is the
signal that one is ready to come off.
"""

from __future__ import annotations

import pytest

#: Applied to a test whose only defect is that its fixture is now a 0.14 vector.
pending_version_gate: pytest.MarkDecorator = pytest.mark.xfail(
    reason="0.14 vector under a 0.12 reader — unblocks at Phase 1",
    strict=False,
)
