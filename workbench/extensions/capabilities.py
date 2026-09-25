#!/usr/bin/env python3
"""Stable Core capability identifiers and protocol versions."""
from __future__ import annotations


API_VERSION = 1
CORE_CAPABILITIES = frozenset({"branch.naming", "context.term-router"})
REMOVED_CAPABILITIES = frozenset(
    {
        "requirements.source",
        "api.contract-extractor",
        "test.end-to-end",
        "scm.review",
        "policy.pack",
    }
)


def is_core_capability(value: object) -> bool:
    return isinstance(value, str) and value in CORE_CAPABILITIES
