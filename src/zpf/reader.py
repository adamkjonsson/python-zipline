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
import hashlib
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
    Discontinuity,
    FileHeader,
    NameResolution,
    OutputLayer,
    Participant,
    Record,
    Session,
    SessionEnd,
    Source,
    SourceKind,
    Undecoded,
    parse_block,
)
from zpf.conformance import ConformanceChecker
from zpf.content import ContentRegistry, ContentType
from zpf.errors import AdvisoryError, Diagnostic, SemanticError, StructuralError, ZpfError
from zpf.jsonl import JsonlReader
from zpf.order import causal_merge, verify_sequenced
from zpf.reassembly import StreamView, record_ranges, stream_layer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import TracebackType
    from typing import Self

_DIGEST_CHUNK = 1 << 16

_MAGIC_BYTES = b"FPIZ"  # the little-endian ZIPF magic, at file offset 8


@dataclass
class _SessionIndex:
    """Everything the indexing pass gathered about one session."""

    descriptor: Session
    participants: list[Participant] = field(default_factory=list)
    names: list[NameResolution] = field(default_factory=list)
    end: SessionEnd | None = None
    locators: list[int | Record] = field(default_factory=list)
    # Per participant, its records **and** its Discontinuity blocks,
    # interleaved in stored order: stored order is what defines a decoded
    # stream's offsets, so a break's position among the records is what
    # makes its width a term (see :func:`zpf.reassembly.record_ranges`).
    by_pid: dict[int, list[int | Record | Discontinuity]] = field(default_factory=dict)
    # Per-participant offset ranges, built on first request and kept. The
    # index outlives the SessionReaders handed out for it, so the cost of a
    # first pass is paid once per file rather than once per view.
    ranges: dict[int, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    # Per-participant layer, resolved once. The specification licenses this
    # cache directly: every record of a participant must resolve to the same
    # layer, which is what makes "the stream's layer" well defined.
    layers: dict[int, OutputLayer | int] = field(default_factory=dict)


class SessionReader:
    """Read-only view of one session, handed out by :class:`FileReader`.

    Attributes:
        session_id: The session's id in the file.

    """

    def __init__(
        self,
        index: _SessionIndex,
        resolve: Callable[[int | Record], Record],
        decoders: Mapping[int, Decoder],
    ) -> None:
        self._index = index
        self._resolve = resolve
        # The reader's live mapping, not a copy: declare-on-first-use means
        # a Decoder is always in it before a record references it, but a
        # streaming reader may still be filling it as later sessions arrive.
        self._decoders = decoders
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

        Only records. A decoded stream may also carry
        :class:`~zpf.blocks.Discontinuity` blocks between them; those are a
        statement about the space *between* records rather than content, so
        they are excluded here and surfaced by :meth:`ranges` — which is
        where their widths change the answer. Use :meth:`stream_blocks` to
        walk both.

        Args:
            pid: The participant whose stream to read.

        Raises:
            KeyError: If the session has no such participant.

        """
        for block in self.stream_blocks(pid):
            if isinstance(block, Record):
                yield block

    def stream_blocks(self, pid: int) -> Iterator[Record | Discontinuity]:
        """Iterate one participant's records and breaks, interleaved in stored order.

        The sequence the specification defines a decoded stream's offset
        space by walking: stored order is what fixes the offsets, so a
        Discontinuity's position among the records is what makes its
        ``width`` a term.

        Args:
            pid: The participant whose stream to read.

        Raises:
            KeyError: If the session has no such participant.

        """
        self.participant(pid)  # raises KeyError for an unknown pid
        for locator in self._index.by_pid.get(pid, ()):
            yield locator if isinstance(locator, Discontinuity) else self._resolve(locator)

    def layer(self, pid: int) -> OutputLayer | int:
        """Return the layer a participant's stream is at.

        This replaced ``is_decoded_stream``, which answered from
        ``decoder_id`` alone. Since a reassembler is a decoder that declares
        ``output_layer = transport``, that question is now only half of the
        rule — see :func:`zpf.reassembly.stream_layer`, which states it.

        Resolved once per participant and kept, which the specification
        licenses because every record of a participant must resolve to the
        same layer.

        Args:
            pid: The participant to ask about.

        Returns:
            :attr:`~zpf.OutputLayer.DECODED` or
            :attr:`~zpf.OutputLayer.TRANSPORT`, or the raw ``int`` for a
            declared value this version does not define.

        Raises:
            KeyError: If the session has no such participant.
            SemanticError: If the participant's records mix layers, or
                reference an undeclared ``decoder_id``.

        """
        self.participant(pid)  # raises KeyError for an unknown pid
        cached = self._index.layers.get(pid)
        if cached is None:
            cached = stream_layer(list(self.stream(pid)), self._decoders)
            self._index.layers[pid] = cached
        return cached

    def ranges(self, pid: int) -> tuple[tuple[int, int], ...]:
        """Return the offset range each of a participant's records occupies.

        One ``(off_start, off_end)`` per record, positionally matching
        :meth:`stream`, in that participant's own offset space — positional
        for a decoded stream, hole-inclusive for a hinted transport one
        (see :func:`zpf.reassembly.record_ranges`).

        A **decoded** record carries no offset field, so this is the only
        way to know where it sits: record *k* occupies
        ``[Σ(preceding payload_len + preceding declared widths), + its own
        payload_len)``, counting the participant's records and its
        :class:`~zpf.blocks.Discontinuity` blocks in stored order. Resolving
        one record therefore costs O(k) on its face, which is why the whole
        participant's table is built on the first call and kept — forward
        reading pays nothing extra, and random access becomes O(1).

        The ranges are what a break moves: a Discontinuity declaring
        ``width = 25`` pushes every later record 25 bytes along, so a reader
        that skips the block computes a different range for each of them.

        Args:
            pid: The participant whose stream to measure.

        Returns:
            The ranges, in stored order.

        Raises:
            KeyError: If the session has no such participant.

        Example:
            >>> for record, (start, end) in zip(
            ...     session.stream(0), session.ranges(0), strict=True
            ... ):
            ...     print(start, end, record.content_type)

        """
        cached = self._index.ranges.get(pid)
        if cached is None:
            cached = record_ranges(
                self.participant(pid), list(self.stream_blocks(pid)), self.layer(pid)
            )
            self._index.ranges[pid] = cached
        return cached

    def reassemble(self) -> tuple[StreamView, ...]:
        """Return a reassembly view of each participant's stream.

        One :class:`~zpf.reassembly.StreamView` per participant, in
        declaration order. A view turns the participant's byte-run records
        into contiguous segments (with gaps made explicit) for a
        stream-oriented participant, or into whole datagrams for a
        packet-oriented one — the ergonomic input a decoder consumes,
        without touching ``seq_start`` arithmetic itself.

        Returns:
            The participants' stream views, in declaration order.

        """
        return tuple(
            StreamView(
                participant,
                lambda pid=participant.participant_id: self.stream_blocks(pid),
            )
            for participant in self._index.participants
        )

    def timeline(self) -> Iterator[Record]:
        """Iterate the session's records in causal order.

        For a SEQUENCED session the stored order *is* a valid causal
        linearization, so this is simply :meth:`records`. For an
        unsequenced session the streaming causal merge
        (:func:`zpf.order.causal_merge`) is run transparently: seq/ack
        happens-before edges for two-participant sessions, the
        ``(timestamp, pid)`` order otherwise — pulling records from disk
        on demand, so memory stays bounded by the in-flight window.

        Raises:
            SemanticError: If the file's ordering hints are inconsistent
                (a stall) or a hint-less stream steps backwards in time.

        """
        if self.sequenced:
            return self.records()
        streams = {
            participant.participant_id: self.stream(participant.participant_id)
            for participant in self._index.participants
        }
        return causal_merge(streams)

    def verify(self) -> None:
        """Verify this SEQUENCED session's stored order is causally valid.

        Walks every record of the session (opt-in cost) and checks the
        linearization the producer asserted with the SEQUENCED flag — see
        :func:`zpf.order.verify_sequenced`.

        Raises:
            ZpfError: If the session is not sequenced (it asserts nothing
                to verify).
            SemanticError: On the first ordering violation, naming the
                records involved.

        """
        if not self.sequenced:
            msg = f"session {self.session_id} is not sequenced; there is nothing to verify"
            raise ZpfError(msg)
        verify_sequenced(self.records(), participant_count=len(self._index.participants))


class FileReader:
    """Session-first view of a ``.zpf`` file; create with :func:`open`.

    Attributes:
        header: The File Header.
        complete: True iff the file ends with a valid End block.
        truncated: True if the file ends inside a block; complete prior
            blocks remain readable.
        diagnostics: Non-fatal conditions from the indexing pass, including
            ``"nonconformant"`` entries from the semantic checker in lenient
            mode — for blocks it isolated, and for advisory findings
            (:class:`~zpf.errors.AdvisoryError`) on blocks that were kept.
        strict: Whether semantic violations and truncation raised instead.
        path: The filesystem path this file was opened from, or None when
            it came from an already-open stream.

    The reader also interprets a record's payload per its ``content_type``:
    see :meth:`content`, and the ``content=`` registry :func:`open` takes for
    the advisory schemes.

    """

    def __init__(
        self,
        source: str | os.PathLike[str] | IO[bytes] | IO[str],
        *,
        strict: bool = False,
        face: Literal["auto", "binary", "jsonl"] = "auto",
        content: ContentRegistry | None = None,
    ) -> None:
        stream, owns = _as_stream(source)
        self._stream: IO[Any] = stream
        self._owns_stream = owns
        self.path = os.fspath(source) if isinstance(source, (str, os.PathLike)) else None
        self.strict = strict
        self._content = content
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

    def digest(self, algorithm: str = "sha256") -> str:
        """Hash this file's bytes, for a citing file's Source descriptor.

        A derived file records the digest of the input it was built from,
        so a consumer can tell whether the input still matches. Reads the
        whole file and restores the stream position afterwards.

        Args:
            algorithm: Any :mod:`hashlib` algorithm name.

        Returns:
            The digest in the spec's ``"<alg>:<hex>"`` form.

        Raises:
            ZpfError: If the reader is closed.

        """
        if self._closed:
            msg = "reader is closed"
            raise ZpfError(msg)
        stream = self._stream
        position = stream.tell()
        digest = hashlib.new(algorithm)
        try:
            stream.seek(0)
            while chunk := stream.read(_DIGEST_CHUNK):
                digest.update(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
        finally:
            stream.seek(position)
        return f"{algorithm}:{digest.hexdigest()}"

    @property
    def face(self) -> str:
        """The detected (or forced) face — ``"binary"`` or ``"jsonl"``."""
        return self._face

    def stream_kind(self, session_id: int, pid: int) -> tuple[SourceKind | int, OutputLayer | int]:
        """Return one stream's ``(provenance, layer)``.

        This replaced ``file_kind``, and the replacement is per stream
        because the question is. Provenance and layer are **independent
        axes** — where a stream's bytes came from, and what shape they have
        — and neither answers the other: a capture can be the direct source
        of a decoded stream (a TLS-terminating proxy), and a stage over a
        ``.zpf`` can emit a transport one (a sessionization stage). One file
        may hold streams at different positions on both axes and needs no
        syntax to say so, which is exactly what ``file_kind`` could not
        express: its three words named a property of a *file*, and there is
        no such property to name.

        Args:
            session_id: The session the stream belongs to.
            pid: The participant whose stream to classify.

        Returns:
            ``(provenance, layer)``. Provenance is the
            :class:`~zpf.SourceKind` of the Source its records reference,
            and layer is what :meth:`SessionReader.layer` resolves. Either
            may be a raw ``int`` for a value this version does not define; a
            stream with no records is ``(CAPTURE, TRANSPORT)``, both being
            the no-information answer.

        Raises:
            KeyError: If the file has no such session or participant.
            SemanticError: If the participant's records disagree about
                either axis — mixing layers, or referencing Sources of
                differing ``kind``. Neither has a single answer to give, and
                picking one would silently mis-resolve every offset or every
                provenance walk from that stream.

        Example:
            >>> provenance, layer = reader.stream_kind(7, 0)

        """
        session = self.session(session_id)
        records = list(session.stream(pid))
        kinds = {self._sources[r.source_id].kind for r in records if r.source_id in self._sources}
        if len(kinds) > 1:
            named = ", ".join(sorted(str(kind) for kind in kinds))
            msg = f"session {session_id} participant {pid} mixes source kinds ({named})"
            raise SemanticError(msg)
        provenance = kinds.pop() if kinds else SourceKind.CAPTURE
        return provenance, session.layer(pid)

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
        """The file's Undecoded markers.

        A decode stage emits them for the input regions it did not decode; a
        pass-through preserving a decoded layer re-emits its input's
        unchanged, which is what carries the input's coverage guarantee
        forward without the output carrying any ``spans`` of its own.
        """
        return tuple(self._undecoded)

    def sessions(self) -> tuple[SessionReader, ...]:
        """Return the file's sessions, in declaration order."""
        return tuple(
            SessionReader(index, self._resolve, self._decoders)
            for index in self._sessions.values()
        )

    def session(self, session_id: int) -> SessionReader:
        """Return one session by id.

        Raises:
            KeyError: If the file declares no such session.

        """
        return SessionReader(self._sessions[session_id], self._resolve, self._decoders)

    def content(self, record: Record, *, strict: bool = False) -> Any:
        """Return a record's payload interpreted as its ``content_type`` says.

        The file-aware counterpart of :meth:`zpf.Record.content`, and the
        only place a ``dec:`` label can be resolved: its token is namespaced
        by the producing decoder's **name**, which this reader can look up
        from the record's ``decoder_id``.

        Dispatch order:

        1. ``prim:`` — normative, built in, and never overridable.
        2. ``mime:``/``dec:`` — a handler from the
           :class:`~zpf.content.ContentRegistry` passed to :func:`open`, if
           one is registered for the media type or the
           (decoder name, token) pair. A handler's exceptions propagate.
        3. Otherwise the payload bytes, exactly as
           :meth:`zpf.Record.content` would return them.

        With no registry this *is* ``record.content()``.

        ⚠ **Beyond the standard** in step 2 only: the format calls ``mime:``
        and ``dec:`` advisory and defines no decoding for them, so a handler's
        answer is the handler's claim. Steps 1 and 3 are the format's own.

        Args:
            record: A record of this file (its ``decoder_id`` is resolved
                against this file's Decoder blocks).
            strict: Raise :class:`~zpf.errors.ContentError` instead of
                falling back to the payload — i.e. when neither ``prim:`` nor
                a registered handler could interpret the label.

        Returns:
            Whatever the matching handler returns, an :class:`int` for a
            ``prim:`` integer token, or the payload bytes.

        Raises:
            ContentError: With ``strict=True``, if nothing could interpret
                the label.

        Example:
            >>> registry = zpf.ContentRegistry()
            >>> registry.register_dec("http/1.1", "request", parse_request)
            >>> with zpf.open("decoded.zpf", content=registry) as reader:
            ...     for record in reader.session(7).records():
            ...         print(reader.content(record))

        """
        if record.content_type is not None and self._content is not None:
            label = ContentType.parse(record.content_type)
            if not label.is_prim:
                handler = self._content.handler(
                    label, decoder_name=self._decoder_name(record)
                )
                if handler is not None:
                    return handler(record.payload)
        return record.content(strict=strict)

    def _decoder_name(self, record: Record) -> str | None:
        """Resolve the record's ``decoder_id`` to the decoder's declared name."""
        if record.decoder_id is None:
            return None
        decoder = self._decoders.get(record.decoder_id)
        return None if decoder is None else decoder.name

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
        # End-of-stream closes every still-open session, which is where the
        # checks that only the whole stream can settle finally run.
        self._finish_checks(offset)
        self.header = flat.header
        self.complete = flat.complete
        self.truncated = flat.truncated

    def _finish_checks(self, offset: int) -> None:
        """Run the checker's end-of-stream pass, isolating in lenient mode."""
        try:
            self._checker.finish()
            self._check_self_derivation()
        except SemanticError as exc:
            if self.strict:
                raise
            self.diagnostics.append(Diagnostic(offset, "nonconformant", str(exc)))

    def _check_self_derivation(self) -> None:
        """Refuse a file that derives one of its own streams from another.

        A stage reads its input and then writes its output, so a file cannot
        be among its own inputs: the offsets its ``spans`` name would have to
        have been fixed before the file containing them existed. The reason
        is *not* the ``digest`` — that option is optional, so a file could
        omit it and the prohibition would still hold.

        **Detection is partial by design**, and the specification says so.
        There is no in-band self-identifier: the only signal is a
        ``zpf-input`` Source's ``uri``, so a reader handed a **path** can
        compare the two after normalisation, while one handed a file object
        — stdin, a socket, a tar member, all of which :func:`zpf.open`
        accepts — cannot, and is **not obliged to detect it**. The rule binds
        the writer either way. A reader that cannot check is conformant in
        accepting the file; what it must still not do is resolve the spans
        against the sibling session.
        """
        if self.path is None:
            return
        mine = os.path.realpath(self.path)
        here = os.path.dirname(mine)
        for source in self._sources.values():
            if source.kind != SourceKind.ZPF_INPUT or source.uri is None:
                continue
            named = os.path.realpath(os.path.join(here, source.uri))
            if named == mine:
                msg = (
                    f"source {source.source_id} names {source.uri!r}, which is this "
                    f"file: a file MUST NOT derive one of its own streams from "
                    f"another, and its spans MUST NOT be resolved against the sibling"
                )
                raise SemanticError(msg)

    def _admit(self, block: Block, offset: int) -> bool:
        """Run the semantic checker; in lenient mode isolate violations.

        An :class:`~zpf.errors.AdvisoryError` is reported like any other
        violation but does *not* cost the caller the block: the spec tells a
        reader meeting one to ignore the label and keep the bytes.
        """
        try:
            self._checker.observe(block)
        except AdvisoryError as exc:
            if self.strict:
                raise
            self.diagnostics.append(Diagnostic(offset, "nonconformant", str(exc)))
            return True
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
        elif isinstance(block, Discontinuity):
            index = self._sessions[block.session_id]
            index.by_pid.setdefault(block.participant_id, []).append(block)
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


def detect_face(source: str | os.PathLike[str] | IO[bytes] | IO[str]) -> str:
    """Detect a file's face: ``"binary"`` (ZIPF magic) or ``"jsonl"``.

    Args:
        source: A path, or a seekable stream (its position is restored).

    Returns:
        ``"binary"`` or ``"jsonl"``.

    Raises:
        StructuralError: If the content matches neither face.

    """
    stream, owns = _as_stream(source)
    try:
        return _resolve_face(stream, "auto")
    finally:
        if owns:
            stream.close()


def open(
    source: str | os.PathLike[str] | IO[bytes] | IO[str],
    *,
    strict: bool = False,
    face: Literal["auto", "binary", "jsonl"] = "auto",
    content: ContentRegistry | None = None,
) -> FileReader:
    """Open a ``.zpf`` file (either face) for session-first reading.

    Runs one indexing pass immediately: structural corruption raises
    :class:`~zpf.errors.StructuralError`; semantic violations are isolated
    per block with a ``"nonconformant"`` diagnostic — except advisory ones
    (:class:`~zpf.errors.AdvisoryError`), where the block is kept and only
    the diagnostic recorded — or raise under ``strict=True``; truncation
    sets status attributes (or raises under ``strict=True``).

    Args:
        source: A path, or a *seekable* stream (bytes for the binary face;
            text or bytes for JSONL).
        strict: Escalate semantic violations and truncation to exceptions.
        face: ``"auto"`` (sniff — the default), ``"binary"``, or ``"jsonl"``.
        content: Handlers for the advisory ``mime:``/``dec:`` content types,
            used by :meth:`FileReader.content`. Without one, only the
            spec-defined ``prim:`` scheme is interpreted.

    Returns:
        A :class:`FileReader` (context manager).

    """
    return FileReader(source, strict=strict, face=face, content=content)
