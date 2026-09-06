#!/usr/bin/env python3
"""Resolve only repositories registered in workspace.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


sys.dont_write_bytecode = True


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workspace_model import (  # noqa: E402
    Repository,
    Workspace,
    WorkspaceError,
    clone_command,
    effective_branch_policy,
    load_workspace,
    render_branch_name,
    repository_path,
    resolve_repository,
)
from workspace_local import load_local_settings  # noqa: E402
from workspace_provider import bound_provider_ref, run_bound_provider  # noqa: E402


def repository_payload(workspace: Workspace, repository: Repository) -> dict[str, object]:
    path = repository_path(workspace, repository)
    return {
        "name": repository.path,
        "path": str(path),
        "aliases": list(repository.aliases),
        "remote": repository.remote,
        "category": repository.category,
        "description": repository.description,
        "instruction": repository.instruction,
        "present": path.is_dir() and not path.is_symlink(),
        "cloneCommand": clone_command(workspace, repository),
        "effectiveBranchPolicy": effective_branch_policy(
            workspace, repository
        ).as_dict(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="治理仓目录（默认脚本所在项目目录）",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="按名称或别名解析已登记仓库")
    resolve.add_argument("name")
    resolve.add_argument("--json", action="store_true")
    listing = commands.add_parser("list", help="列出已登记仓库")
    listing.add_argument("--json", action="store_true")
    branch = commands.add_parser("branch", help="生成已登记仓库的确定性分支名")
    branch.add_argument("name")
    branch.add_argument("--type", required=True)
    branch.add_argument("--slug", required=True)
    branch.add_argument("--owner")
    branch.add_argument("--provider-input", type=Path)
    branch.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workspace = load_workspace(args.root.resolve())
        if args.command in {"resolve", "branch"}:
            repository = resolve_repository(workspace.repositories, args.name)
        if args.command == "resolve":
            payload = repository_payload(workspace, repository)
            print(
                json.dumps(payload, ensure_ascii=False)
                if args.json
                else payload["path"]
            )
            return 0
        if args.command == "branch":
            policy = effective_branch_policy(workspace, repository)
            if args.type == "hotfix" and policy.hotfix_base is None:
                raise WorkspaceError(f"仓库 {repository.path} 未启用 hotfixBase")
            owner = args.owner
            if owner is None:
                owner = load_local_settings(workspace.root, required=True).branch_owner
                if owner is None:
                    raise WorkspaceError("本地配置缺少 branchOwner，无法生成分支名")
            payload: dict[str, object] = {
                "repository": repository.path,
                "type": args.type,
                "slug": args.slug,
                "owner": owner,
                "baseBranch": (
                    policy.hotfix_base if args.type == "hotfix" else policy.work_base
                ),
                "branch": render_branch_name(
                    policy, owner=owner, branch_type=args.type, slug=args.slug
                ),
            }
            provider_input: dict[str, object] = {}
            if args.provider_input is not None:
                from workspace_model import read_json

                provider_input = read_json(args.provider_input)
            if bound_provider_ref(workspace, "branch.naming", repository) is not None:
                result = run_bound_provider(
                    workspace.root,
                    capability="branch.naming",
                    operation="name",
                    repository=repository,
                    request={
                        "type": args.type,
                        "slug": args.slug,
                        "owner": owner,
                        "baseBranch": payload["baseBranch"],
                        "parameters": provider_input,
                    },
                )
                if result["status"] != "ok":
                    diagnostics = result.get("diagnostics")
                    if not isinstance(diagnostics, list) or not diagnostics:
                        raise WorkspaceError(
                            "PROVIDER_PROTOCOL_ERROR: Provider 失败结果缺少诊断"
                        )
                    errors = [
                        item
                        for item in diagnostics
                        if isinstance(item, dict) and item.get("level") == "error"
                    ]
                    if not errors:
                        raise WorkspaceError(
                            "PROVIDER_PROTOCOL_ERROR: Provider 失败结果诊断无效"
                        )
                    diagnostic = errors[0]
                    raise WorkspaceError(
                        f"{diagnostic['code']}: {diagnostic['message']}"
                    )
                result_data = result["result"]
                if (
                    not isinstance(result_data, dict)
                    or set(result_data) != {"branch"}
                    or not isinstance(result_data["branch"], str)
                ):
                    raise WorkspaceError("PROVIDER_PROTOCOL_ERROR: branch Provider 结果必须只包含 branch")
                from workspace_model import _git_branch_valid

                if not _git_branch_valid(result_data["branch"]):
                    raise WorkspaceError("PROVIDER_PROTOCOL_ERROR: branch Provider 返回了无效分支名")
                payload["branch"] = result_data["branch"]
            print(
                json.dumps(payload, ensure_ascii=False)
                if args.json
                else payload["branch"]
            )
            return 0
        payloads = [
            repository_payload(workspace, repository)
            for repository in workspace.repositories
        ]
        if args.json:
            print(json.dumps(payloads, ensure_ascii=False))
        else:
            for payload in payloads:
                print(f"{payload['name']}\t{payload['path']}")
        return 0
    except (OSError, RuntimeError, WorkspaceError) as exc:
        print(f"错误：{exc}")
        return 2 if "未登记仓库或别名" in str(exc) else 1


if __name__ == "__main__":
    raise SystemExit(main())
