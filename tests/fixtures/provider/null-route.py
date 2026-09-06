#!/usr/bin/env python3
"""Return a valid empty context route."""

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
            "result": {"route": None},
            "diagnostics": [],
            "effects": [],
        }
    )
)
