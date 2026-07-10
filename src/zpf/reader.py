"""The session-first reader: :func:`open` and its views.

This is the front door for consuming ``.zpf`` files at rest. :func:`open`
runs one indexing pass over the whole file — structural corruption raises,
semantic violations are isolated per block with a diagnostic (or raise
under ``strict=True``), truncation is reported via status attributes — and
returns a :class:`FileReader` whose :class:`SessionReader` views hand out
each session's participants and records without the caller doing any
bookkeeping.

On the binary face the index stores only file offsets, and records are
re-read (seek + parse) on demand, so memory stays proportional to the
number of records, not their payloads. The JSONL face — the debugging
projection for small captures — is loaded eagerly instead. The face is
sniffed automatically: a binary file carries the ZIPF magic at offset 8, a
JSONL file's first non-whitespace byte is ``{``.

:func:`open` needs a *seekable* source; live tails and pipes remain the
domain of the flat, bounded-memory :class:`~zpf.binary.BlockReader` /
:class:`~zpf.jsonl.JsonlReader`.

Example:
    >>> with zpf.open("cap.zpf") as f:
    ...     for session in f.sessions():
    ...         print(session.proto, session.key)
    ...         for record in session.records():
    ...             ...

"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal

from zpf import _frame
from zpf.binary import BlockReader
from zpf.blocks import (
    Block,
    Decoder,
    FileHeader,
    NameResolution,
    Participant,
    Record,
    Session,
    SessionEnd,
    Source,
    Undecoded,
    parse_block,
)
from zpf.conformance import ConformanceChecker
from zpf.errors import Diagnostic, SemanticError, StructuralError, ZpfError
from zpf.jsonl import JsonlReader

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import TracebackType
    from typing import Self

_MAGIC_BYTES = b"FPIZ"  # the little-endian ZIPF magic, at file offset 8


@dataclass
class _SessionIndex:
    """Everything the indexing pass gathered about one session."""

    descriptor: Session
    participants: list[Participant] = field(default_factory=list)
    names: list[NameResolution] = field(default_factory=list)
    end: SessionEnd | None = None
    locators: list[int | Record] = field(default_factory=list)
    by_pid: dict[int, list[int | Record]] = field(default_factory=dict)


class SessionReader:
    """Read-only view of one session, handed out by :class:`FileReader`.

    Attributes:
        session_id: The session's id in the file.

    """

    def __init__(
        self, index: _SessionIndex, resolve: Callable[[int | Record], Record]
    ) -> None:
        self._index = index
        self._resolve = resolve
        self.session_id = index.descriptor.session_id

    @property
    def descriptor(self) -> Session:
        """The Session Descriptor block."""
        return self._index.descriptor

    @property
    def proto(self) -> str | None:
        """The session's protocol (e.g. ``"tcp"``), if declared."""
        return self._index.descriptor.proto

    @property
    def key(self) -> str | None:
        """The session's human-readable flow key, if declared."""
        return self._index.descriptor.flow_key

    @property
    def sequenced(self) -> bool:
        """Whether the producer committed a causal order for this session."""
        return self._index.descriptor.sequenced

    @property
    def participants(self) -> tuple[Participant, ...]:
        """The session's participants, in declaration order."""
        return tuple(self._index.participants)

    def participant(self, pid: int) -> Participant:
        """Return the participant with the given id.

        Raises:
            KeyError: If the session has no such participant.

        """
        for candidate in self._index.participants:
            if candidate.participant_id == pid:
                return candidate
        raise KeyError(pid)

    @property
    def names(self) -> tuple[NameResolution, ...]:
        """Name/Identity Resolution labels attached to this session."""
        return tuple(self._index.names)

    @property
    def end(self) -> SessionEnd | None:
        """The Session End block, or ``None`` if the session was never ended."""
        return self._index.end

    @property
    def record_count(self) -> int:
        """How many records the session holds."""
        return len(self._index.locators)

    def records(self) -> Iterator[Record]:
        """Iterate the session's records in stored order.

        On the binary face each record is read from disk on demand, so
        iterators over different sessions may be interleaved freely
        (single-threaded use).
        """
        for locator in self._index.locators:
            yield self._resolve(locator)

    def stream(self, pid: int) -> Iterator[Record]:
        """Iterate one participant's records, in stored (``seq_start``) order.

        Args:
            pid: The participant whose stream to read.

        Raises:
            KeyError: If the session has no such participant.

        """
        self.participant(pid)  # raises KeyError for an unknown pid
        for locator in self._index.by_pid.get(pid, ()):
            yield self._resolve(locator)

    def timeline(self) -> Iterator[Record]:
        """Iterate the session's records in causal order.

        For a SEQUENCED session the stored order *is* a valid causal
        linearization, so this is simply :meth:`records`. For an
        unsequenced session the causal merge is not implemented yet.

        Raises:
            ZpfError: If the session is not sequenced (use :meth:`records`
                for stored order or :meth:`stream` for one participant).

        """
        if not self.sequenced:
            msg = (
                f"session {self.session_id} is not sequenced and the causal merge "
                "is not implemented yet; use records() for stored order or "
                "stream(pid) for a single participant"
            )
            raise ZpfError(msg)
        return self.records()


class FileReader:
    """Session-first view of a ``.zpf`` file; create with :func:`open`.

    Attributes:
        header: The File Header.
        complete: True iff the file ends with a valid End block.
        truncated: True if the file ends inside a block; complete prior
            blocks remain readable.
        diagnostics: Non-fatal conditions from the indexing pass, including
            ``"nonconformant"`` entries for blocks isolated by the
            semantic checker in lenient mode.
        strict: Whether semantic violations and truncation raised instead.

    """

    def __init__(
        self,
        source: str | os.PathLike[str] | IO[bytes] | IO[str],
        *,
        strict: bool = False,
        face: Literal["auto", "binary", "jsonl"] = "auto",
    ) -> None:
        stream, owns = _as_stream(source)
        self._stream: IO[Any] = stream
        self._owns_stream = owns
        self.strict = strict
        self.complete = False
        self.truncated = False
        self.diagnostics: list[Diagnostic] = []
        self.header: FileHeader | None = None
        self._closed = False
        self._checker = ConformanceChecker()
        self._sources: dict[int, Source] = {}
        self._decoders: dict[int, Decoder] = {}
        self._sessions: dict[int, _SessionIndex] = {}
        self._undecoded: list[Undecoded] = []
        self._jsonl_blocks: list[Block] | None = None  # eager storage, JSONL face
        self._wrapper: io.TextIOWrapper | None = None
        try:
            self._face = _resolve_face(stream, face)
            self._index()
        except (ZpfError, ValueError, OSError):
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying stream if this reader opened it."""
        self._closed = True
        if self._wrapper is not None and not self._owns_stream:
            with contextlib.suppress(ValueError):
                self._wrapper.detach()
            self._wrapper = None
        if self._owns_stream:
            self._stream.close()

    @property
    def face(self) -> str:
        """The detected (or forced) face: ``"binary"`` or ``"jsonl"``."""
        return self._face

    @property
    def file_kind(self) -> str | None:
        """The file's inferred kind: raw, decode-stage, pass-through, or None."""
        return self._checker.file_kind

    @property
    def sources(self) -> dict[int, Source]:
        """The declared Sources, keyed by ``source_id``."""
        return dict(self._sources)

    @property
    def decoders(self) -> dict[int, Decoder]:
        """The declared Decoders, keyed by ``decoder_id``."""
        return dict(self._decoders)

    @property
    def undecoded(self) -> tuple[Undecoded, ...]:
        """The file's Undecoded markers (decode-stage files)."""
        return tuple(self._undecoded)

    def sessions(self) -> tuple[SessionReader, ...]:
        """Return the file's sessions, in declaration order."""
        return tuple(
            SessionReader(index, self._resolve) for index in self._sessions.values()
        )

    def session(self, session_id: int) -> SessionReader:
        """Return one session by id.

        Raises:
            KeyError: If the file declares no such session.

        """
        return SessionReader(self._sessions[session_id], self._resolve)

    def blocks(self) -> Iterator[Block]:
        """Re-walk every block of the file, in file order.

        This is the raw stream — it includes blocks the semantic checker
        isolated from the session views.
        """
        if self._jsonl_blocks is not None:
            yield from self._jsonl_blocks
            return
        if self._closed:
            msg = "reader is closed"
            raise ZpfError(msg)
        self._stream.seek(0)
        reader = BlockReader(self._stream)
        yield from reader

    # --- Indexing pass -------------------------------------------------------

    def _index(self) -> None:
        if self._face == "binary":
            flat: BlockReader | JsonlReader = BlockReader(self._stream, strict=self.strict)
        else:
            if _is_text(self._stream):
                text: IO[str] = self._stream
            else:
                self._wrapper = io.TextIOWrapper(self._stream, encoding="utf-8")
                text = self._wrapper
            flat = JsonlReader(text, strict=self.strict)
            self._jsonl_blocks = []
        # Share our diagnostics list so the flat reader's entries and the
        # checker's interleave chronologically.
        flat.diagnostics = self.diagnostics
        offset = 0
        for block in flat:
            if self._jsonl_blocks is not None:
                self._jsonl_blocks.append(block)  # blocks() shows isolated ones too
                position = flat.line_no if isinstance(flat, JsonlReader) else 0
            else:
                position = offset
                offset += _frame.FRAME_SIZE + len(block.to_bytes())
            if self._admit(block, position):
                self._file_into_index(block, position)
        self.header = flat.header
        self.complete = flat.complete
        self.truncated = flat.truncated

    def _admit(self, block: Block, offset: int) -> bool:
        """Run the semantic checker; in lenient mode isolate violations."""
        try:
            self._checker.observe(block)
        except SemanticError as exc:
            if self.strict:
                raise
            self.diagnostics.append(Diagnostic(offset, "nonconformant", str(exc)))
            return False
        return True

    def _file_into_index(self, block: Block, offset: int) -> None:
        if isinstance(block, Source):
            self._sources[block.source_id] = block
        elif isinstance(block, Decoder):
            self._decoders[block.decoder_id] = block
        elif isinstance(block, Session):
            self._sessions[block.session_id] = _SessionIndex(descriptor=block)
        elif isinstance(block, Participant):
            self._sessions[block.session_id].participants.append(block)
        elif isinstance(block, NameResolution):
            self._sessions[block.session_id].names.append(block)
        elif isinstance(block, SessionEnd):
            self._sessions[block.session_id].end = block
        elif isinstance(block, Undecoded):
            self._undecoded.append(block)
        elif isinstance(block, Record):
            index = self._sessions[block.session_id]
            locator: int | Record = block if self._jsonl_blocks is not None else offset
            index.locators.append(locator)
            index.by_pid.setdefault(block.sender_pid, []).append(locator)

    def _resolve(self, locator: int | Record) -> Record:
        """Materialize an indexed record: in-memory, or seek-and-parse."""
        if isinstance(locator, Record):
            return locator
        if self._closed:
            msg = "reader is closed"
            raise ZpfError(msg)
        self._stream.seek(locator)
        frame = self._stream.read(_frame.FRAME_SIZE)
        if len(frame) < _frame.FRAME_SIZE:
            msg = f"offset {locator} no longer holds a block; did the file change?"
            raise ZpfError(msg)
        block_type, _reserved, length = _frame.FRAME.unpack(frame)
        block = parse_block(block_type, self._stream.read(length))
        if not isinstance(block, Record):
            msg = f"offset {locator} no longer holds a Record; did the file change?"
            raise ZpfError(msg)
        return block


def _as_stream(
    source: str | os.PathLike[str] | IO[bytes] | IO[str],
) -> tuple[IO[Any], bool]:
    """Return a seekable stream for the source, and whether we own it."""
    if isinstance(source, (str, os.PathLike)):
        return Path(source).open("rb"), True
    if not source.seekable():
        msg = (
            "zpf.open() needs a seekable source; use BlockReader/JsonlReader "
            "for pipes and live tails"
        )
        raise ZpfError(msg)
    return source, False


def _is_text(stream: IO[Any]) -> bool:
    """Return whether the stream yields ``str`` (vs ``bytes``)."""
    return isinstance(stream.read(0), str)


def _resolve_face(stream: IO[Any], face: str) -> str:
    """Validate or sniff the file face on a seekable stream."""
    if face not in ("auto", "binary", "jsonl"):
        msg = f"face must be 'auto', 'binary' or 'jsonl', got {face!r}"
        raise ValueError(msg)
    text = _is_text(stream)
    if face == "binary" and text:
        msg = "the binary face needs a bytes stream, not a text stream"
        raise ZpfError(msg)
    if face != "auto":
        return face
    if text:
        return "jsonl"
    position = stream.tell()
    head = stream.read(12)
    stream.seek(position)
    if head[8:12] == _MAGIC_BYTES:
        return "binary"
    if head.lstrip()[:1] == b"{":
        return "jsonl"
    msg = "cannot detect the file face: neither the ZIPF magic nor a JSONL object"
    raise StructuralError(msg)


def open(
    source: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    strict: bool = False,
    face: Literal["auto", "binary", "jsonl"] = "auto",
) -> FileReader:
    """Open a ``.zpf`` file (either face) for session-first reading.

    Runs one indexing pass immediately: structural corruption raises
    :class:`~zpf.errors.StructuralError`; semantic violations are isolated
    per block with a ``"nonconformant"`` diagnostic (or raise under
    ``strict=True``); truncation sets status attributes (or raises under
    ``strict=True``).

    Args:
        source: A path, or a *seekable* stream (bytes for the binary face;
            text or bytes for JSONL).
        strict: Escalate semantic violations and truncation to exceptions.
        face: ``"auto"`` (sniff — the default), ``"binary"``, or ``"jsonl"``.

    Returns:
        A :class:`FileReader` (context manager).

    """
    return FileReader(source, strict=strict, face=face)
