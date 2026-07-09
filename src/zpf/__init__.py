"""A Python implementation of the Zipline Payload Format (``.zpf``).

The Zipline Payload Format stores the payload of network traffic — the bytes
that flow between endpoints once packets have been reassembled into sessions,
plus the metadata needed to consume them. This package implements v1.0 of the
specification: reading and writing the binary container and its JSON-Lines
projection.
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"
