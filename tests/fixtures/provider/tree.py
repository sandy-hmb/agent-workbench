#!/usr/bin/env python3
"""Spawn a delayed child to prove the runner cleans an entire process group."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


mode, sentinel = sys.argv[1:]
subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import sys,time; from pathlib import Path; time.sleep(.75); Path(sys.argv[1]).write_text('alive')",
        sentinel,
    ]
)
if mode == "limit":
    sys.stdout.buffer.write(b"x" * (1024 * 1024 + 1))
    sys.stdout.buffer.flush()
    time.sleep(10)
elif mode == "normal":
    print(
        json.dumps(
            {
                "apiVersion": 1,
                "provider": "example-extension/team",
                "status": "ok",
                "result": {},
                "diagnostics": [],
                "effects": [],
            }
        )
    )
else:
    time.sleep(10)
