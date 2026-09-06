#!/usr/bin/env python3
"""替换 Core 默认命名规则的最小 Provider 示例。

命名规则：{type}/{owner}-{slug}（Core 默认是 {owner}/{type}/{slug}）。
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    request = json.load(sys.stdin)
    branch = f"{request['type']}/{request['owner']}-{request['slug']}"
    print(
        json.dumps(
            {
                "apiVersion": 1,
                "provider": "example-branch-naming/naming",
                "status": "ok",
                "result": {"branch": branch},
                "diagnostics": [],
                "effects": [],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
