#!/usr/bin/env python3
"""通用 webhook 进度通知 Action 示例。

接入某个具体通知渠道需要复制本示例，只改 build_payload() 一个函数为该
渠道要求的报文格式；其余部分（读取请求、发起 POST、返回结果契约）保持
不变。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def build_payload(request: dict) -> dict:
    """唯一的定制点：把 Kit 的固定字段翻译成目标 webhook 期望的报文格式。"""
    return {
        "workflow": request.get("workflow"),
        "run": request.get("run"),
        "stage": request.get("stage"),
        "featureSlug": request.get("featureSlug"),
        "repository": request.get("repository"),
        "branch": request.get("branch"),
    }


def main() -> int:
    request = json.load(sys.stdin)
    url = os.environ["WEBHOOK_URL"]
    body = json.dumps(build_payload(request)).encode("utf-8")
    http_request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(http_request, timeout=10) as response:
        response.read()
    print(
        json.dumps(
            {
                "apiVersion": 1,
                "provider": "example-webhook-notify/notify",
                "status": "ok",
                "result": {"posted": True},
                "diagnostics": [],
                "effects": [{"kind": "network"}],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
