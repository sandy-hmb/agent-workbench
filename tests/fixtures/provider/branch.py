#!/usr/bin/env python3
"""Produce a branch from only the canonical request fields."""

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
            "result": {"branch": f"{request['owner']}/provider/{request['slug']}"},
            "diagnostics": [],
            "effects": [],
        }
    )
)
