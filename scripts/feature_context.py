#!/usr/bin/env python3
"""Resolve, list, and update workspace feature metadata."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import tempfile
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from workspace_local import LocalSettings, canonical_local_json, load_local_settings
from workspace_model import Workspace, WorkspaceError, load_workspace, resolve_repository
from workspace_model import atomic_write_many, effective_branch_policy, render_branch_name
from workspace_paths import cache_root, features_root, local_file, state_root, workspace_file


sys.dont_write_bytecode = True


FEATURE_STATUSES = frozenset({"planning", "development", "testing", "done", "paused"})
FIELD_RE = re.compile(r"^\s*-\s*([^:：]+)\s*[:：]\s*(.*?)\s*$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class FeatureMatch:
    slug: str
    path: Path
    status: str
    repository: str
    base_branch: str


@dataclass(frozen=True)
class FeatureSummary:
    slug: str
    path: Path
    status: str
    repositories: tuple[str, ...]
    branches: tuple[tuple[str, str], ...]
    base_branches: tuple[tuple[str, str], ...]
    updated: str


def feature_metadata(readme: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = FIELD_RE.match(line)
        if match:
            name = match.group(1).strip()
            if name in fields:
                raise ValueError(f"{readme} 的元数据字段重复：{name}")
            fields[name] = match.group(2).strip().strip("`")
    return fields


def branch_mappings(value: str) -> list[tuple[str, str]]:
    mappings = []
    for item in re.split(r"[;；]", value.replace("`", "")):
        if "->" not in item:
            continue
        repository, branch = item.split("->", 1)
        branch = re.split(r"[（(]", branch, maxsplit=1)[0]
        repository = repository.strip()
        branch = branch.strip()
        if repository and branch:
            mappings.append((repository, branch))
    return mappings


def _strict_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("最后更新日期必须为 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("最后更新日期必须为 YYYY-MM-DD")
    return value


def _canonical_mappings(
    workspace: Workspace, readme: Path, label: str, value: str
) -> tuple[tuple[str, str], ...]:
    result = []
    seen = set()
    for configured, branch in branch_mappings(value):
        repository = resolve_repository(workspace.repositories, configured)
        if configured != repository.path:
            raise ValueError(
                f"{readme} 的 {label} 必须使用 canonical 仓路径：{repository.path}"
            )
        if repository.path in seen:
            raise ValueError(f"{readme} 的 {label} 仓库重复：{repository.path}")
        seen.add(repository.path)
        result.append((repository.path, branch))
    return tuple(result)


def _canonical_repositories(
    workspace: Workspace, readme: Path, value: str
) -> tuple[str, ...]:
    result = []
    for configured in (
        item.strip()
        for item in re.split(r"[、,，]", value.replace("`", ""))
        if item.strip()
    ):
        repository = resolve_repository(workspace.repositories, configured)
        if configured != repository.path:
            raise ValueError(
                f"{readme} 的涉及仓库必须使用 canonical 仓路径：{repository.path}"
            )
        if repository.path in result:
            raise ValueError(f"{readme} 的涉及仓库重复：{repository.path}")
        result.append(repository.path)
    return tuple(result)


def feature_summary(workspace: Workspace, feature: Path) -> FeatureSummary:
    features = features_root(workspace.root)
    readme = feature / "README.md"
    try:
        if features.parent.is_symlink() or features.is_symlink() or not features.is_dir():
            raise ValueError(f"需求目录不存在或不安全：{features}")
        if feature.is_symlink() or not feature.is_dir():
            raise ValueError(f"需求目录不存在或不安全：{feature}")
        if feature.resolve().parent != features.resolve():
            raise ValueError(f"需求目录必须是 {features} 的直接子目录：{feature}")
        if not SLUG_RE.fullmatch(feature.name):
            raise ValueError(f"需求目录名必须使用小写 kebab-case：{feature.name}")
        if readme.is_symlink() or not readme.is_file():
            raise ValueError(f"需求 README 不存在或不安全：{readme}")
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"需求目录路径不可解析：{feature}") from exc
    metadata = feature_metadata(readme)
    feature_status = metadata.get("状态", "")
    if feature_status not in FEATURE_STATUSES:
        raise ValueError(f"{readme} 的状态无效：{feature_status}")
    updated = _strict_date(metadata.get("最后更新", ""))
    declared = _canonical_repositories(
        workspace, readme, metadata.get("涉及仓库", "")
    )
    branches = _canonical_mappings(
        workspace, readme, "工作分支", metadata.get("工作分支", "")
    )
    bases = _canonical_mappings(
        workspace, readme, "基线分支", metadata.get("基线分支", "")
    )
    if not declared:
        raise ValueError(f"{readme} 的涉及仓库不能为空")
    if not branches:
        raise ValueError(f"{readme} 的工作分支不能为空")
    if not bases:
        raise ValueError(f"{readme} 的基线分支不能为空")
    if not (
        set(declared)
        == {repository for repository, _ in branches}
        == {repository for repository, _ in bases}
    ):
        raise ValueError(
            f"{readme} 的涉及仓库、工作分支和基线分支仓库必须完全一致"
        )
    return FeatureSummary(
        feature.name,
        feature,
        feature_status,
        declared,
        branches,
        bases,
        updated,
    )


def _features_root(root: Path) -> Path:
    try:
        workspace_root = root.resolve()
        registry = workspace_file(workspace_root)
        features = features_root(workspace_root)
        for path in (features.parent.parent, features.parent, features):
            if path.is_symlink() or not path.is_dir():
                raise ValueError(f"需求目录不存在或不安全：{path}")
            if not path.resolve().is_relative_to(workspace_root):
                raise ValueError(f"需求目录越出治理根：{path}")
        return features
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"需求目录路径不可解析：{root}") from exc


def _feature_directories(root: Path):
    features = _features_root(root)
    try:
        children = sorted(features.iterdir())
    except OSError as exc:
        raise ValueError(f"无法读取需求目录：{features}") from exc
    for path in children:
        if path.is_symlink():
            raise ValueError(f"需求目录不允许符号链接：{path}")
        if path.is_dir():
            try:
                if not path.resolve().is_relative_to(features.resolve()):
                    raise ValueError(f"需求目录越出治理根：{path}")
            except (OSError, RuntimeError) as exc:
                raise ValueError(f"需求目录路径不可解析：{path}") from exc
            yield path


def resolve_features(root: Path, repository: str, branch: str) -> list[FeatureMatch]:
    _features_root(root)
    workspace = load_workspace(root.resolve())
    canonical = resolve_repository(workspace.repositories, repository).path
    matches = []
    for feature in _feature_directories(root):
        summary = feature_summary(workspace, feature)
        if (canonical, branch) not in summary.branches:
            continue
        bases = dict(summary.base_branches)
        matches.append(
            FeatureMatch(
                feature.name,
                feature,
                summary.status,
                canonical,
                bases[canonical],
            )
        )
    return matches


def list_features(root: Path, status: str | None = None) -> list[FeatureSummary]:
    if status is not None and status not in FEATURE_STATUSES:
        raise ValueError(f"无效状态：{status}")
    _features_root(root)
    workspace = load_workspace(root.resolve())
    result = []
    for feature in _feature_directories(root):
        summary = feature_summary(workspace, feature)
        if status is not None and summary.status != status:
            continue
        result.append(summary)
    return result


def list_features_lenient(root: Path) -> tuple[list[FeatureSummary], list[dict[str, str]]]:
    """像 list_features 一样列出需求，但单个需求解析失败时收集而不是整体报错。

    路径安全检查（符号链接、越出治理根）仍在 _feature_directories 内整体抛出，
    不降级；只有单个需求内容解析（feature_summary）失败时才降级收集。
    """
    root = root.resolve()
    _features_root(root)
    workspace = load_workspace(root)
    directories = list(_feature_directories(root))
    result: list[FeatureSummary] = []
    degraded: list[dict[str, str]] = []
    for feature in directories:
        try:
            result.append(feature_summary(workspace, feature))
        except (ValueError, OSError, RuntimeError) as exc:
            degraded.append({"path": feature.relative_to(root).as_posix(), "error": str(exc)})
    return result, degraded


def summary_payload(item: FeatureSummary) -> dict[str, object]:
    return {
        "featureSlug": item.slug,
        "path": str(item.path),
        "status": item.status,
        "repositories": list(item.repositories),
        "branches": [list(mapping) for mapping in item.branches],
        "baseBranches": [list(mapping) for mapping in item.base_branches],
        "lastUpdated": item.updated,
    }


def update_status(root: Path, slug: str, status: str, updated: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("feature-slug 必须使用小写 kebab-case")
    if status not in FEATURE_STATUSES:
        raise ValueError(f"无效状态：{status}")
    _strict_date(updated)
    readme = _features_root(root) / slug / "README.md"
    feature_dir = readme.parent
    features_root = readme.parent.parent
    try:
        safe_feature_dir = (
            not feature_dir.is_symlink()
            and feature_dir.resolve().is_relative_to(features_root.resolve())
        )
    except (OSError, RuntimeError):
        safe_feature_dir = False
    if not safe_feature_dir or not readme.is_file() or readme.is_symlink():
        raise ValueError(f"需求 README 不存在或不安全：{readme}")
    mode = stat.S_IMODE(readme.stat().st_mode)
    lines = readme.read_text(encoding="utf-8").splitlines()
    found_status = False
    found_updated = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*-\s*状态\s*[:：]", line):
            lines[index] = f"- 状态：{status}"
            found_status = True
        elif re.match(r"^\s*-\s*最后更新\s*[:：]", line):
            lines[index] = f"- 最后更新：{updated}"
            found_updated = True
    if not found_status or not found_updated:
        raise ValueError("需求 README 缺少状态或最后更新字段")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=readme.parent, delete=False
        ) as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, mode)
        os.replace(temporary, readme)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return readme


def create_feature(
    root: Path,
    slug: str,
    repositories: Sequence[str],
    *,
    branch_type: str = "feature",
    owner: str | None = None,
    title: str | None = None,
    summary: str = "",
    updated: str | None = None,
) -> Path:
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("feature-slug 必须使用小写 kebab-case")
    if not repositories:
        raise ValueError("至少需要一个 --repo")
    root = root.resolve()
    features = _features_root(root)
    feature = features / slug
    if feature.exists() or feature.is_symlink():
        raise ValueError(f"需求目录已存在：{feature}")

    workspace = load_workspace(root)
    local = load_local_settings(root, required=True)
    branch_owner = owner if owner is not None else local.branch_owner
    if branch_owner is None:
        raise WorkspaceError("本地配置缺少 branchOwner，无法生成分支名")

    canonical_repositories: list[str] = []
    branches: list[str] = []
    bases: list[str] = []
    for configured in repositories:
        repository = resolve_repository(workspace.repositories, configured)
        if repository.path in canonical_repositories:
            raise ValueError(f"需求仓库重复：{repository.path}")
        policy = effective_branch_policy(workspace, repository)
        if branch_type == "hotfix" and policy.hotfix_base is None:
            raise WorkspaceError(f"仓库 {repository.path} 未启用 hotfixBase")
        base = policy.hotfix_base if branch_type == "hotfix" else policy.work_base
        canonical_repositories.append(repository.path)
        bases.append(f"`{repository.path}` -> `{base}`")
        branches.append(
            f"`{repository.path}` -> `"
            + render_branch_name(
                policy,
                owner=branch_owner,
                branch_type=branch_type,
                slug=slug,
            )
            + "`"
        )

    updated = updated or date.today().isoformat()
    _strict_date(updated)
    title = title or slug
    template = (Path(__file__).resolve().parents[1] / "templates/feature/README.md").read_text(
        encoding="utf-8"
    )
    readme = template.format(
        title=title,
        repositories=", ".join(f"`{item}`" for item in canonical_repositories),
        branches="；".join(branches),
        base_branches="；".join(bases),
        updated=updated,
    )
    if summary.strip():
        readme = readme.rstrip() + f"\n\n## 摘要\n\n{summary.strip()}\n"
    outputs = {
        feature / "README.md": readme,
        feature / "requirements/requirements.md": (
            "# Requirements\n\n" + (summary.strip() + "\n" if summary.strip() else "")
        ),
        feature / "design/design.md": "# Design\n",
        feature / "plans/implementation.md": "- [ ] 实现需求\n- [ ] 完成验证\n",
        feature / "testing/verification.md": "# Verification\n\n尚未执行验证。\n",
    }
    feature.mkdir()
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_many(tuple(outputs.items()))
    except BaseException:
        for path in outputs:
            try:
                path.unlink()
            except OSError:
                pass
        for path in sorted(
            (feature / name for name in ("requirements", "design", "plans", "testing")),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                path.rmdir()
            except OSError:
                pass
        try:
            feature.rmdir()
        except OSError:
            pass
        raise
    return feature


def set_active_feature(root: Path, slug: str | None) -> dict[str, object]:
    """切换或清除本机活跃需求指针；只改 workspace.local.json，不改任何需求记录。"""
    root = root.resolve()
    if slug is not None and not SLUG_RE.fullmatch(slug):
        raise ValueError("需求短名必须使用小写 kebab-case")
    cache = cache_root(root)
    if cache.is_symlink() or not cache.is_dir():
        raise ValueError(f"缺少本地缓存目录，先完成初始化：{cache}")
    lock_path = cache / "active-feature.lock"
    if lock_path.is_symlink():
        raise ValueError(f"活跃指针锁不安全：{lock_path}")
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if slug is not None:
                matches = [item for item in list_features(root) if item.slug == slug]
                if not matches or matches[0].status == "done":
                    raise ValueError(f"活跃需求必须是当前未完成的需求：{slug}")
            settings = load_local_settings(root, required=True)
            updated = LocalSettings(
                settings.branch_owner,
                settings.primary_role,
                settings.extensions,
                slug,
                settings.extension_sources,
            )
            atomic_write_many(((local_file(root), canonical_local_json(updated)),))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return {"activeFeature": slug}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="治理仓目录（默认脚本所在项目目录）",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="创建标准需求骨架")
    create.add_argument("slug")
    create.add_argument("--repo", action="append", dest="repositories", required=True)
    create.add_argument("--type", choices=("feature", "fix", "hotfix", "refactor", "docs", "chore"), default="feature")
    create.add_argument("--owner")
    create.add_argument("--title")
    create.add_argument("--summary", default="")
    create.add_argument("--date", default=date.today().isoformat())
    create.add_argument("--json", action="store_true")
    resolve = commands.add_parser("resolve", help="按仓库和工作分支解析需求")
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--branch", required=True)
    resolve.add_argument("--json", action="store_true")
    listing = commands.add_parser("list", help="列出需求")
    listing.add_argument("--status", choices=sorted(FEATURE_STATUSES))
    listing.add_argument("--json", action="store_true")
    set_status = commands.add_parser("set-status", help="更新需求状态")
    set_status.add_argument("slug")
    set_status.add_argument("status", choices=sorted(FEATURE_STATUSES))
    set_status.add_argument("--date", default=date.today().isoformat())
    set_active = commands.add_parser("set-active", help="切换或清除本机活跃需求指针")
    set_active.add_argument("slug", nargs="?", help="要设为活跃的需求短名")
    set_active.add_argument("--clear", action="store_true", help="清除活跃指针")
    set_active.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve()
        if args.command == "create":
            path = create_feature(
                root,
                args.slug,
                args.repositories,
                branch_type=args.type,
                owner=args.owner,
                title=args.title,
                summary=args.summary,
                updated=args.date,
            )
            payload = {"featureSlug": args.slug, "path": str(path)}
            print(json.dumps(payload, ensure_ascii=False) if args.json else path)
            return 0
        if args.command == "resolve":
            matches = resolve_features(root, args.repo, args.branch)
            if not matches:
                print("未匹配到需求目录")
                return 2
            if len(matches) > 1:
                print("匹配到多个需求目录：" + ", ".join(item.slug for item in matches))
                return 3
            match = matches[0]
            payload = {
                "featureSlug": match.slug,
                "path": str(match.path),
                "status": match.status,
                "repository": match.repository,
                "branch": args.branch,
                "baseBranch": match.base_branch,
            }
            print(json.dumps(payload, ensure_ascii=False) if args.json else match.slug)
            return 0
        if args.command == "list":
            summaries = list_features(root, args.status)
            if args.json:
                print(
                    json.dumps(
                        [summary_payload(summary) for summary in summaries],
                        ensure_ascii=False,
                    )
                )
            else:
                for summary in summaries:
                    print(
                        "\t".join(
                            (
                                summary.slug,
                                summary.status,
                                ",".join(summary.repositories),
                                summary.updated,
                            )
                        )
                    )
            return 0
        if args.command == "set-active":
            if args.clear == (args.slug is not None):
                print("错误：必须恰好提供 slug 或 --clear 之一", file=sys.stderr)
                return 2
            payload = set_active_feature(root, None if args.clear else args.slug)
            print(json.dumps(payload, ensure_ascii=False) if args.json else str(payload["activeFeature"]))
            return 0
        path = update_status(root, args.slug, args.status, args.date)
        print(f"已更新需求状态：{path}")
        return 0
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
