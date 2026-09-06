#!/usr/bin/env python3
"""Return the request and selected environment visibility for protocol tests."""

from __future__ import annotations

import json
import os
import sys


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "ok",
            "result": {
                "request": request,
                "undeclared": os.environ.get("UNDECLARED_PROVIDER_TEST"),
            },
            "diagnostics": [],
            "effects": [],
        }
    )
)
