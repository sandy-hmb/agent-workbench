#!/usr/bin/env python3
"""Return one valid context route."""

from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "ok",
            "result": {
                "route": {"kind": "actions", "term": "review", "action": "review"}
            },
            "diagnostics": [],
            "effects": [],
        }
    )
)
