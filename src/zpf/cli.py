"""The ``zpf`` command-line tool.

Subcommands::

    zpf info FILE                     summarize a file's sessions
    zpf cat FILE                      dump a file as JSONL on stdout
    zpf convert IN OUT [--to FACE]    convert between the two faces
    zpf validate FILE [--strict] [--verify] [--input RAW]
    zpf merge SIDE_A SIDE_B -o OUT    merge two captured directions

Exit codes: 0 = success/clean, 1 = validation findings, 2 = error
(unreadable file, bad usage).
"""

from __future__ import annotations

import argparse
import enum
import sys
from typing import IO, TYPE_CHECKING

import zpf
from zpf.binary import BlockReader, BlockWriter
from zpf.errors import SemanticError, ZpfError
from zpf.jsonl import JsonlReader, JsonlWriter
from zpf.reader import detect_face
from zpf.transform import check_coverage, merge_files

if TYPE_CHECKING:
    from collections.abc import Sequence

    from zpf.reader import FileReader, SessionReader


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``zpf`` command line; return the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except (ZpfError, OSError) as exc:
        print(f"zpf: {exc}", file=sys.stderr)
        return 2
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zpf", description="Inspect, convert, validate, and merge .zpf files."
    )
    parser.add_argument("--version", action="version", version=f"zpf {zpf.__version__}")
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    info = commands.add_parser("info", help="summarize a file's sessions")
    info.add_argument("file")
    info.set_defaults(func=_cmd_info)

    cat = commands.add_parser("cat", help="dump a file as JSONL on stdout")
    cat.add_argument("file")
    cat.set_defaults(func=_cmd_cat)

    convert = commands.add_parser("convert", help="convert between the two faces")
    convert.add_argument("input")
    convert.add_argument("output")
    convert.add_argument(
        "--to", choices=("binary", "jsonl"), default=None,
        help="target face (default: the opposite of the input's)",
    )
    convert.set_defaults(func=_cmd_convert)

    validate = commands.add_parser("validate", help="check a file's conformance")
    validate.add_argument("file")
    validate.add_argument(
        "--strict", action="store_true", help="fail hard on the first violation"
    )
    validate.add_argument(
        "--verify", action="store_true",
        help="also verify every SEQUENCED session's stored order",
    )
    validate.add_argument(
        "--input", metavar="RAW", default=None,
        help="also check the decode-stage coverage guarantee against this input file",
    )
    validate.set_defaults(func=_cmd_validate)

    merge = commands.add_parser("merge", help="merge two captured directions")
    merge.add_argument("side_a")
    merge.add_argument("side_b")
    merge.add_argument("-o", "--output", required=True)
    merge.add_argument(
        "--produced-by", default=f"zpf {zpf.__version__}",
        help="transform provenance written to the output header",
    )
    merge.set_defaults(func=_cmd_merge)
    return parser


def _cmd_info(args: argparse.Namespace) -> int:
    with zpf.open(args.file) as reader:
        header = reader.header
        print(f"face:      {reader.face}")
        print(f"complete:  {reader.complete}" + ("  (truncated)" if reader.truncated else ""))
        ticks = f"{header.tick_hz} ticks/s"
        print(f"clock:     {ticks}" + ("  single-clock" if header.single_clock else ""))
        for label, value in (
            ("creator", header.creator),
            ("produced_by", header.produced_by),
            ("produced_at", header.produced_at),
        ):
            if value is not None:
                print(f"{label}: {value}")
        for source in reader.sources.values():
            kind = (
                source.kind.name.lower().replace("_", "-")
                if isinstance(source.kind, zpf.SourceKind)
                else source.kind
            )
            print(f"source {source.source_id}: {kind} {source.uri or ''}".rstrip())
        for decoder in reader.decoders.values():
            print(f"decoder {decoder.decoder_id}: {decoder.name} {decoder.version or ''}".rstrip())
        for session in reader.sessions():
            _print_session(session, reader)
        for diagnostic in reader.diagnostics:
            print(f"note: {diagnostic.category}: {diagnostic.message}", file=sys.stderr)
    return 0


def _print_session(session: SessionReader, reader: zpf.FileReader) -> None:
    endpoints = ", ".join(
        participant.endpoint or f"pid {participant.participant_id}"
        for participant in session.participants
    )
    flags = " sequenced" if session.sequenced else ""
    ended = f" end={session.end.reason or 'yes'}" if session.end else ""
    key = f" key={session.key!r}" if session.key else ""
    print(
        f"session {session.session_id}: proto={session.proto or '?'}{key} "
        f"participants=[{endpoints}] records={session.record_count}{flags}{ended}"
    )
    # One line per stream, because that is the unit both axes are about.
    # There is no file-wide answer to print: one file may hold a decoded
    # stream beside a transport one, and a captured stream beside a derived
    # one.
    for participant in session.participants:
        pid = participant.participant_id
        try:
            provenance, layer = reader.stream_kind(session.session_id, pid)
        except zpf.SemanticError as exc:
            print(f"  stream {pid}: unresolvable — {exc}")
            continue
        print(f"  stream {pid}: {_axis_name(provenance)} {_axis_name(layer)}")


def _axis_name(value: object) -> str:
    """Name a provenance or layer, falling back to the raw number."""
    return value.name.lower().replace("_", "-") if isinstance(value, enum.Enum) else str(value)


def _cmd_cat(args: argparse.Namespace) -> int:
    _convert(args.file, sys.stdout, detect_face(args.file), "jsonl")
    return 0


def _cmd_convert(args: argparse.Namespace) -> int:
    in_face = detect_face(args.input)
    out_face = args.to or ("jsonl" if in_face == "binary" else "binary")
    _convert(args.input, args.output, in_face, out_face)
    return 0


def _convert(
    source: str, sink: str | IO[str] | IO[bytes], in_face: str, out_face: str
) -> None:
    """Stream every block from one face to the other (or normalize)."""
    reader = BlockReader(source) if in_face == "binary" else JsonlReader(source)
    writer = BlockWriter(sink) if out_face == "binary" else JsonlWriter(sink)
    with reader, writer:
        for block in reader:
            writer.write(block)


def _cmd_validate(args: argparse.Namespace) -> int:
    findings = 0
    with zpf.open(args.file, strict=args.strict) as reader:
        for diagnostic in reader.diagnostics:
            print(f"{args.file}: {diagnostic.category}: {diagnostic.message}")
            findings += 1
        if args.verify:
            findings += _verify_sessions(args.file, reader)
    if args.input is not None:
        for diagnostic in check_coverage(args.file, args.input):
            print(f"{args.file}: {diagnostic.category}: {diagnostic.message}")
            findings += 1
    if findings:
        print(f"{args.file}: {findings} finding(s)", file=sys.stderr)
        return 1
    print(f"{args.file}: OK", file=sys.stderr)
    return 0


def _verify_sessions(path: str, reader: FileReader) -> int:
    findings = 0
    for session in reader.sessions():
        if not session.sequenced:
            continue
        try:
            session.verify()
        except SemanticError as exc:
            print(f"{path}: sequenced-order: {exc}")
            findings += 1
    return findings


def _cmd_merge(args: argparse.Namespace) -> int:
    merge_files(args.side_a, args.side_b, args.output, produced_by=args.produced_by)
    return 0
