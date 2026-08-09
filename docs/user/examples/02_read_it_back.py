"""Tutorial stage 2: read a ``.zpf`` file back.

Run ``01_write_first_file.py`` first, from the same directory, so
``session.zpf`` exists. This script opens it, walks its sessions,
participants, and records, and prints what it finds.
"""

import zpf

with zpf.open("session.zpf") as reader:
    print(f"face: {reader.face}, complete: {reader.complete}")

    for session in reader.sessions():  # one Session block declared -> one entry
        print(f"session {session.session_id}: {session.proto} {session.key!r}")

        for participant in session.participants:
            print(f"  participant {participant.participant_id}: {participant.endpoint}")

        # records() reads each Record from disk as the loop asks for it: this
        # doesn't load the whole file into memory, only the current one.
        for record in session.records():
            sender = session.participant(record.sender_pid).endpoint
            print(f"  ts={record.timestamp} {sender} -> {record.payload!r}")
