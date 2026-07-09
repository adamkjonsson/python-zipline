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

Errors and diagnostics
^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: zpf.errors
   :members:
