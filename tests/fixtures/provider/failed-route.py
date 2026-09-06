#!/usr/bin/env python3
"""Return a valid failed context Provider result."""

from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "failed",
            "result": None,
            "diagnostics": [
                {"level": "error", "code": "ROUTE_FAILED", "message": "failed"}
            ],
            "effects": [],
        }
    )
)
