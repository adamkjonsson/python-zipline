zpf — Zipline Payload Format
============================

A Python implementation of v1.0 of the `Zipline Payload Format
<https://github.com/adamkjonsson/zipline>`_ (``.zpf``): a file format for the
payload of network traffic — the bytes exchanged between endpoints once
packets have been reassembled into sessions, plus the metadata needed to
consume them.

.. automodule:: zpf

Everything below is re-exported at the package top level; ``import zpf`` is
all a consumer needs.

Quickstart
----------

Writing a capture, reading it back in causal order, and using the CLI::

    import zpf

    with zpf.create("out.zpf", tick_hz=1_000_000) as w:
        w.add_source("capture", uri="tap.pcap")
        with w.begin_session(proto="tcp") as s:
            alice = s.participant("10.0.0.1:51000", isn=1000)
            s.record(alice, ts=1000, payload=b"GET / HTTP/1.1\r\n\r\n",
                     seq_start=1001, ack=5001)
            s.end(reason="fin")

    with zpf.open("out.zpf") as f:
        for session in f.sessions():
            for record in session.timeline():
                ...

.. code-block:: shell

    zpf info cap.zpf
    zpf cat cap.zpf
    zpf validate cap.zpf --verify
    zpf merge sideA.zpf sideB.zpf -o merged.zpf

API reference
-------------

Typed blocks
^^^^^^^^^^^^

.. automodule:: zpf.blocks
   :members:

.. autoclass:: zpf.RawOption

Binary container I/O
^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.binary
   :members:

Reading files (the ergonomic API)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.reader
   :members:

Writing files (the ergonomic API)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.writer
   :members:

Transforms
^^^^^^^^^^

.. automodule:: zpf.transform
   :members:

Conformance checking
^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.conformance
   :members:

Sequence-number ordering
^^^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.order
   :members:

JSON-Lines projection
^^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.jsonl
   :members:

Errors and diagnostics
^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.errors
   :members:
