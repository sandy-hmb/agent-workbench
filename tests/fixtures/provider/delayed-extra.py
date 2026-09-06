#!/usr/bin/env python3
"""Write a valid result then an invalid trailing payload before exit."""

from __future__ import annotations

import json
import sys
import time


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "ok",
            "result": {},
            "diagnostics": [],
            "effects": [],
        }
    ),
    end="",
)
sys.stdout.flush()
time.sleep(0.1)
sys.stdout.write("extra")
sys.stdout.flush()
