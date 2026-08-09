"""Marks for tests the 0.14 → 0.16 port has temporarily invalidated.

Phase 0 re-vendored ``tests/vectors/`` at ``v0.16``. Two groups of tests read
those files without going through the ratchet in :mod:`tests.test_vectors`, so
each of them now hands a 0/16 file to a 0.14 reader and is refused at the
version gate:

* **Inside the harness** — the escape-contract test, the JSONL → binary
  direction and the canonical re-encode are parametrized over every readable
  case rather than over a tier, so :data:`~tests.test_vectors.KNOWN_PASSING`
  does not reach them. Neither does ``test_a_clean_pair_reports_nothing``,
  which names ``chain/`` directly.
* **Outside it** — five modules use vectors as ordinary fixtures. That reaches
  further than a grep for the vectors path shows: the golden file *is*
  ``raw-minimal.zpf``, ``test_golden`` exports its bytes as ``GOLDEN``, and the
  writer, the reader and the CLI all round-trip it from there.

Nothing about those tests is wrong, and they come back at the Phase 1 version
bump with nothing else needed. Of the 42 files vendored at ``v0.14``, 37 differ
from their ``v0.16`` selves at exactly one byte — ``version_minor`` — and three
more (``chain/annotated``, ``chain/decoded``, ``splice/http``) differ there and
in the input ``digest`` that byte changed. ``reject-unknown-major`` is identical,
having no minor to bump. The one file that really changed is
``reordered-decoded``, which gained a Discontinuity at its seam under 0.15's
origination duty, and no test outside the harness reads it.

That every Decoder-carrying vector is in the one-byte group is worth noticing
rather than passing over: it is the specification's ``output_layer``
byte-compatibility claim, verified against the shipped bytes. ``decoded = 0``
lands on ``_reserved`` bytes a conformant 0.14 writer already wrote 0.

**Phase 1 deletes this module.** Every mark it holds must be gone by then;
``strict=False`` keeps the suite green in the meantime, and an ``XPASS`` is the
signal that one is ready to come off.
"""

from __future__ import annotations

import pytest

#: Applied to a test whose only defect is that its fixture is now a 0.16 vector.
pending_version_gate: pytest.MarkDecorator = pytest.mark.xfail(
    reason="0.16 vector under a 0.14 reader — unblocks at Phase 1",
    strict=False,
)
