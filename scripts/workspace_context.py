#!/usr/bin/env python3
"""Route a request only through explicit local workspace context terms."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_model import Workspace, WorkspaceError, load_workspace  # noqa: E402
from workspace_provider import bound_provider_ref, run_bound_provider  # noqa: E402


_TARGETS = {
    "products": "repository",
    "capabilities": "capability",
    "actions": "action",
    "repositories": "repository",
}
_ASCII_WORD = re.compile(r"^[A-Za-z0-9_]+$")


def _blocked(code: str, message: str) -> dict[str, object]:
    return {
        "status": "blocked",
        "route": None,
        "diagnostics": [{"level": "error", "code": code, "message": message}],
    }


def _single_line(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\r" not in value
        and "\n" not in value
    )


def _matches_term(text: str, term: str) -> bool:
    if _ASCII_WORD.fullmatch(term):
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text
        ) is not None
    return term in text


def _route_value(workspace: Workspace, value: object) -> tuple[bool, dict[str, str] | None]:
    if value is None:
        return True, None
    if not isinstance(value, dict):
        return False, None
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _TARGETS:
        return False, None
    target = _TARGETS[kind]
    if set(value) != {"kind", "term", target}:
        return False, None
    term = value["term"]
    target_value = value[target]
    if not _single_line(term) or not _single_line(target_value):
        return False, None
    if target == "repository" and target_value not in {
        repository.path for repository in workspace.repositories
    }:
        return False, None
    return True, {"kind": kind, "term": term, target: target_value}


def _provider_route(workspace: Workspace, text: str) -> dict[str, object]:
    response = run_bound_provider(
        workspace.root,
        capability="context.term-router",
        operation="route",
        request={"text": text},
    )
    diagnostics = response.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(
        isinstance(item, Mapping) for item in diagnostics
    ):
        return _blocked("CONTEXT_PROVIDER_RESULT_INVALID", "Context Provider diagnostics 无效")
    if response.get("status") != "ok":
        if not diagnostics:
            return _blocked("CONTEXT_PROVIDER_RESULT_INVALID", "Context Provider 缺少失败诊断")
        return {
            "status": "blocked",
            "route": None,
            "diagnostics": [dict(item) for item in diagnostics],
        }
    result = response.get("result")
    if not isinstance(result, dict) or set(result) != {"route"}:
        return _blocked("CONTEXT_PROVIDER_RESULT_INVALID", "Context Provider result 必须只包含 route")
    valid, route_value = _route_value(workspace, result["route"])
    if not valid:
        return _blocked("CONTEXT_PROVIDER_RESULT_INVALID", "Context Provider route 不符合契约")
    return {
        "status": "ok",
        "route": route_value,
        "diagnostics": [dict(item) for item in diagnostics],
    }


def _data_route(workspace: Workspace, text: str) -> dict[str, object]:
    router = workspace.context.get("termRouter")
    if not isinstance(router, Mapping):
        return _blocked("CONTEXT_NO_MATCH", "workspace.context 未声明 termRouter")
    matches: list[dict[str, str]] = []
    for kind, target in _TARGETS.items():
        entries = router.get(kind)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            term = entry.get("term")
            value = entry.get(target)
            if isinstance(term, str) and isinstance(value, str) and _matches_term(text, term):
                matches.append({"kind": kind, "term": term, target: value})
    if not matches:
        return _blocked("CONTEXT_NO_MATCH", "请求未匹配任何明确声明的术语")
    if len(matches) != 1:
        return _blocked("CONTEXT_AMBIGUOUS", "请求匹配了多个明确声明的术语")
    valid, route_value = _route_value(workspace, matches[0])
    assert valid and route_value is not None
    return {"status": "ok", "route": route_value, "diagnostics": []}


def route(root: Path, text: str) -> dict[str, object]:
    if not isinstance(text, str) or not text.strip() or "\r" in text or "\n" in text:
        return _blocked("CONTEXT_INPUT_INVALID", "路由请求必须是非空单行文本")
    try:
        workspace = load_workspace(Path(root))
    except WorkspaceError as exc:
        return _blocked("CONTEXT_WORKSPACE_INVALID", str(exc))
    if bound_provider_ref(workspace, "context.term-router") is not None:
        return _provider_route(workspace, text)
    return _data_route(workspace, text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("route", help="按显式 termRouter 路由请求")
    command.add_argument("--root", type=Path, default=Path("."))
    command.add_argument("--text", required=True)
    command.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    result = route(args.root, args.text)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["status"] == "ok":
        print(json.dumps(result["route"], ensure_ascii=False))
    else:
        diagnostics = result["diagnostics"]
        assert isinstance(diagnostics, list)
        diagnostic = diagnostics[0] if diagnostics else {
            "code": "CONTEXT_PROVIDER_RESULT_INVALID",
            "message": "路由失败但未提供诊断",
        }
        assert isinstance(diagnostic, Mapping)
        print(f"{diagnostic['code']}: {diagnostic['message']}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
