#!/usr/bin/env python3
"""Write one Provider result in two chunks before exiting."""

from __future__ import annotations

import json
import sys
import time


request = json.load(sys.stdin)
result = json.dumps(
    {
        "apiVersion": 1,
        "provider": request["provider"],
        "status": "ok",
        "result": {},
        "diagnostics": [],
        "effects": [],
    }
)
middle = len(result) // 2
sys.stdout.write(result[:middle])
sys.stdout.flush()
time.sleep(0.1)
sys.stdout.write(result[middle:])
sys.stdout.flush()
