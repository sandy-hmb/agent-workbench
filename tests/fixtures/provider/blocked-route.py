#!/usr/bin/env python3
"""Return a valid blocked context Provider result."""

from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "blocked",
            "result": None,
            "diagnostics": [
                {"level": "error", "code": "ROUTE_BLOCKED", "message": "blocked"}
            ],
            "effects": [],
        }
    )
)
