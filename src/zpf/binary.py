"""Flat reader and writer for the ZPF binary container.

:class:`BlockReader` iterates a file (or any binary stream, seekable or not)
as typed blocks, enforcing the specification's *structural* tier — corruption
that poisons the byte stream always raises :class:`~zpf.errors.StructuralError`.
Expected imperfections are reported, not raised: a truncated tail (a live or
crashed writer) ends iteration and sets :attr:`BlockReader.truncated`, and
trailing bytes after a valid End block are reported as a diagnostic. Pass
``strict=True`` to escalate those to exceptions.

:class:`BlockWriter` writes blocks and guarantees well-formed bytes (header
first, exactly one header, nothing after an End block, 4-byte alignment).
Semantic conformance — declare-before-use, per-participant record order,
file-kind purity — is not checked at this layer.
"""

from __future__ import annotations

import os
from typing import IO, TYPE_CHECKING

from zpf import _frame
from zpf.blocks import Block, End, FileHeader, UnknownBlock, parse_block
from zpf.errors import (
    Diagnostic,
    EncodeError,
    SemanticError,
    StructuralError,
    TruncatedError,
    ZpfError,
)

if TYPE_CHECKING:
    from types import TracebackType
    from typing import Self


def _read_up_to(stream: IO[bytes], size: int) -> bytes:
    """Read up to ``size`` bytes, looping over short reads until EOF."""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class BlockReader:
    """Iterate the blocks of a ZPF file or binary stream.

    The reader is single-pass and works on non-seekable streams. Structural
    corruption always raises; truncation and trailing bytes are reported via
    the status attributes below unless ``strict`` is set.

    Attributes:
        strict: Whether truncation/trailing-bytes escalate to exceptions.
        complete: True once a valid End block was read — the writer finished
            cleanly. False for a live, still-growing, or crashed file.
        truncated: True if the stream ended inside a block; the partial tail
            was discarded and all complete prior blocks remain valid.
        diagnostics: Non-fatal conditions noticed while reading.
        header: The File Header, available once the first block was read.

    Example:
        >>> with BlockReader(path) as reader:
        ...     for block in reader:
        ...         ...
        ...     if not reader.complete:
        ...         ...  # live, growing, or crashed writer

    """

    def __init__(
        self, source: str | os.PathLike[str] | IO[bytes], *, strict: bool = False
    ) -> None:
        if isinstance(source, (str, os.PathLike)):
            self._stream: IO[bytes] = open(source, "rb")  # noqa: SIM115 -- closed by close()
            self._owns_stream = True
        else:
            self._stream = source
            self._owns_stream = False
        self.strict = strict
        self.complete = False
        self.truncated = False
        self.diagnostics: list[Diagnostic] = []
        self.header: FileHeader | None = None
        self._offset = 0
        self._finished = False
        self._after_end = False

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
        if self._owns_stream:
            self._stream.close()

    def __iter__(self) -> BlockReader:
        return self

    def __next__(self) -> Block:
        if self._finished:
            raise StopIteration
        if self._after_end:
            self._check_trailing()
            raise StopIteration
        frame_offset = self._offset
        frame = _read_up_to(self._stream, _frame.FRAME_SIZE)
        if not frame:
            if frame_offset == 0:
                msg = "empty file: missing File Header"
                raise StructuralError(msg)
            self._finished = True
            raise StopIteration
        if len(frame) < _frame.FRAME_SIZE:
            self._truncate(frame_offset, "stream ends inside a block frame")
            raise StopIteration
        block_type, _reserved, length = _frame.FRAME.unpack(frame)
        if length % 4:
            msg = f"block length {length} at offset {frame_offset} is not a multiple of 4"
            raise StructuralError(msg)
        content = _read_up_to(self._stream, length)
        if len(content) < length:
            self._truncate(frame_offset, "stream ends inside a block's content")
            raise StopIteration
        self._offset += _frame.FRAME_SIZE + length
        return self._to_block(block_type, content, frame_offset)

    def _to_block(self, block_type: int, content: bytes, offset: int) -> Block:
        """Parse content into a block, applying header-position rules."""
        if self.header is None:
            if block_type != _frame.BT_FILE_HEADER:
                msg = f"first block has type 0x{block_type:02X}; a File Header must come first"
                raise StructuralError(msg)
        elif block_type == _frame.BT_FILE_HEADER:
            msg = f"second File Header at offset {offset}; a file has exactly one"
            raise StructuralError(msg)
        try:
            block = parse_block(block_type, content)
        except (SemanticError, EncodeError) as exc:
            if self.strict:
                raise
            self.diagnostics.append(Diagnostic(offset, "invalid-block", str(exc)))
            block = UnknownBlock(block_type=block_type, content=content)
        if isinstance(block, FileHeader):
            self.header = block
        elif isinstance(block, End):
            self.complete = True
            self._after_end = True
        return block

    def _truncate(self, offset: int, message: str) -> None:
        """Record a truncated tail; raise instead when strict."""
        self.truncated = True
        self._finished = True
        self.diagnostics.append(Diagnostic(offset, "truncated", message))
        if self.strict:
            raise TruncatedError(f"{message} (offset {offset})")

    def _check_trailing(self) -> None:
        """Report bytes appearing after a valid End block."""
        self._finished = True
        if self._stream.read(1):
            message = f"bytes after the End block at offset {self._offset} are not blocks"
            self.diagnostics.append(Diagnostic(self._offset, "trailing-bytes", message))
            if self.strict:
                raise SemanticError(message)


class BlockWriter:
    """Write blocks to a ZPF file or binary stream.

    Guarantees well-formed bytes: the first block must be a
    :class:`~zpf.blocks.FileHeader`, only one header is allowed, nothing can
    follow an :class:`~zpf.blocks.End` block, and framing/alignment are
    handled here. Semantic conformance is the caller's responsibility at
    this layer.

    Example:
        >>> with BlockWriter(path) as writer:
        ...     writer.write(FileHeader(tick_hz=1_000_000))
        ...     writer.write(End())

    """

    def __init__(self, sink: str | os.PathLike[str] | IO[bytes]) -> None:
        if isinstance(sink, (str, os.PathLike)):
            self._stream: IO[bytes] = open(sink, "wb")  # noqa: SIM115 -- closed by close()
            self._owns_stream = True
        else:
            self._stream = sink
            self._owns_stream = False
        self._offset = 0
        self._closed = False
        self._ended = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def offset(self) -> int:
        """The file offset at which the next block will be written."""
        return self._offset

    def write(self, block: Block) -> int:
        """Write one block.

        Args:
            block: The block to write.

        Returns:
            The file offset of the written block's frame.

        Raises:
            ZpfError: If the writer is closed.
            StructuralError: If the block would make the file ill-formed
                (missing/duplicate File Header, block after End).
            EncodeError: If the block cannot be represented (e.g. content
                exceeding the u32 frame length).

        """
        if self._closed:
            msg = "writer is closed"
            raise ZpfError(msg)
        if self._ended:
            msg = "cannot write a block after the End block"
            raise StructuralError(msg)
        is_header = isinstance(block, FileHeader)
        if self._offset == 0 and not is_header:
            msg = f"first block must be a File Header, not {type(block).__name__}"
            raise StructuralError(msg)
        if self._offset > 0 and is_header:
            msg = "a file has exactly one File Header, as its first block"
            raise StructuralError(msg)
        content = block.to_bytes()
        if len(content) > _frame.MAX_BLOCK_LENGTH:
            msg = f"block content of {len(content)} bytes exceeds the u32 frame length"
            raise EncodeError(msg)
        offset = self._offset
        self._stream.write(_frame.FRAME.pack(block.block_type, 0, len(content)))
        self._stream.write(content)
        self._offset += _frame.FRAME_SIZE + len(content)
        if isinstance(block, End):
            self._ended = True
        return offset

    def close(self) -> None:
        """Flush and close the underlying stream if this writer opened it."""
        if self._closed:
            return
        self._closed = True
        self._stream.flush()
        if self._owns_stream:
            self._stream.close()
