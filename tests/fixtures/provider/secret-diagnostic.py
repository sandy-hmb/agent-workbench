#!/usr/bin/env python3
"""Emit a declared value everywhere the runner must redact it."""

from __future__ import annotations

import json
import os
import sys


request = json.load(sys.stdin)
secret = os.environ["DECLARED_TOKEN"]
print(secret, file=sys.stderr)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": request["provider"],
            "status": "ok",
            "result": {"secret": secret, secret: "key value", "nested": [secret]},
            "diagnostics": [
                {"level": "warning", "code": "SECRET_DIAGNOSTIC", "message": secret}
            ],
            "effects": [{secret: secret}],
        }
    )
)
