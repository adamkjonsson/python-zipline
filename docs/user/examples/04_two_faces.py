"""Tutorial stage 4: the two faces are lossless projections of each other.

Run ``01_write_first_file.py`` first, from the same directory, so
``session.zpf`` exists. This script converts it to the JSON-Lines face,
prints a line to inspect by eye, converts back, and checks the round trip
reproduced the original bytes exactly.

The ``zpf`` command line does the same conversion without writing any
Python: ``zpf convert session.zpf session.jsonl`` and
``zpf convert session.jsonl session2.zpf`` -- see the CLI reference.
"""

from pathlib import Path

import zpf

zpf.binary_to_jsonl("session.zpf", "session.jsonl")

lines = Path("session.jsonl").read_text().splitlines()
print(f"{len(lines)} lines; the Record line looks like:")
print(next(line for line in lines if '"type":"record"' in line))

zpf.jsonl_to_binary("session.jsonl", "session2.zpf")

original = Path("session.zpf").read_bytes()
roundtripped = Path("session2.zpf").read_bytes()
assert original == roundtripped
print("round trip: byte-identical")
