#!/usr/bin/env python3
import json
import sys


request = json.load(sys.stdin)
print(
    json.dumps(
        {
            "apiVersion": 1,
            "provider": "action-extension/deploy-test",
            "status": "ok",
            "result": {"summary": "test deployment completed", "request": request},
            "diagnostics": [],
            "effects": [{"kind": "network"}],
        }
    )
)
