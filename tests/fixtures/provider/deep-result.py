#!/usr/bin/env python3
"""Return a syntactically valid but deeply nested Provider result."""

from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
result: object = None
for _ in range(500):
    result = [result]
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "ok",
            "result": result,
            "diagnostics": [],
            "effects": [],
        }
    )
)
