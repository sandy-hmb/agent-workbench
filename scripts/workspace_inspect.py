#!/usr/bin/env python3
"""Read-only, versioned data surface for a local workbench consumer."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import io
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
import time
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Sequence

sys.dont_write_bytecode = True
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from feature_context import FEATURE_STATUSES, feature_metadata, list_features_lenient, summary_payload
from extension_registry import discover_extensions
from extension_model import extension_digest
from context_measure import estimate_tokens
from kit_feature_brief import brief_result
from workspace_local import load_local_settings
from workspace_model import WorkspaceError, effective_branch_policy, load_workspace, parse_json_bytes, parse_workspace, read_json, repository_path, resolve_repository
from workspace_model import VERSION
from workspace_extension import _read_lock, extension_status
from workspace_paths import features_root, state_root, workflow_file, workflow_runs_root
from workspace_status import _single_feature_progress, _task_evidence_state, artifact_summary, document_reviews, plan_analysis, plan_progress, verification_record
from workspace_verification import describe_verification_document, feature_code_state, verification_passed
from workspace_workflow import CORE_WORKFLOW, FINGERPRINT_RE, RUN_FIELDS, RUN_STATUSES, STAGE_RECORD_FIELDS, _resolve, _stage_fingerprint, _valid_run_id
from workflow_model import load_core_workflow, load_overlay, resolve_stages

API_MAJOR = 1
API_MINOR = 1
MAX_FILE = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_PAGE = 200
MAX_SCAN = 10_000
MAX_SEARCH_BYTES = 16 * 1024 * 1024
NORMAL_TIMEOUT = 10.0
CODE_TIMEOUT = 30.0
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".sql", ".csv"}
STANDARD_FILES = {"README.md", "requirements/requirements.md", "design/design.md", "plans/implementation.md", "testing/verification.md"}
H1 = re.compile(r"^#\s+(.+?)\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
LINK = re.compile(r"\]\(([^)#]+)(?:#[^)]+)?\)")
REFERENCE_ID = re.compile(r"(?<![A-Za-z0-9_])([RD]\d+(?:\.\d+)?)(?![A-Za-z0-9_])", re.IGNORECASE)


class InspectError(ValueError):
    def __init__(self, code: str, message: str, *, source: str | None = None):
        super().__init__(message)
        self.code, self.source = code, source


class Deadline:
    def __init__(self, seconds: float): self.ends = time.monotonic() + seconds
    def check(self) -> None:
        if time.monotonic() >= self.ends: raise InspectError("INSPECT_TIMEOUT", "读取超过 operation 时限")
    def remaining(self) -> float:
        self.check(); return max(0.01, self.ends - time.monotonic())


def _diag(code: str, message: str, source: str | None = None, severity: str = "error") -> dict[str, object]:
    value: dict[str, object] = {"code": code, "severity": severity, "message": message, "scope": "inspect", "subject": None, "path": source, "line": None, "retryable": code in {"INSPECT_INPUT_CHANGED", "INSPECT_TIMEOUT"}}
    return value


def _revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git_read(arguments: list[str], deadline: Deadline) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=deadline.remaining(), env={**dict(), "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})


def _read(path: Path, root: Path, *, limit: int = MAX_FILE, deadline: Deadline | None = None) -> bytes:
    if deadline: deadline.check()
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise InspectError("INSPECT_UNSAFE_PATH", f"路径包含符号链接：{current}", source=str(path))
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise InspectError("INSPECT_UNSAFE_PATH", f"路径不可安全读取：{path}", source=str(path)) from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise InspectError("INSPECT_UNSAFE_PATH", f"必须是普通文件：{path}", source=str(path))
    if info.st_size > limit:
        raise InspectError("INSPECT_LIMIT_EXCEEDED", f"文件超过 {limit} 字节限制：{path}", source=str(path))
    try:
        chunks = []
        total = 0
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                raise InspectError("INSPECT_INPUT_CHANGED", f"读取前文件发生变化：{path}", source=str(path))
            while True:
                if deadline: deadline.check()
                chunk = handle.read(min(64 * 1024, limit + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise InspectError("INSPECT_LIMIT_EXCEEDED", f"文件超过 {limit} 字节限制：{path}", source=str(path))
                chunks.append(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise InspectError("INSPECT_NOT_FOUND", f"无法读取：{path}", source=str(path)) from exc
    after = path.lstat()
    if len(data) > limit or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
        raise InspectError("INSPECT_INPUT_CHANGED", f"读取期间文件发生变化：{path}", source=str(path))
    return data


def _inspect_run(path: Path, root: Path, deadline: Deadline) -> tuple[dict[str, object], bytes]:
    data = _read(path, root, deadline=deadline)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InspectError("INSPECT_INVALID_DATA", "Run JSON 无效", source=str(path)) from exc
    run_id = path.stem
    if not _valid_run_id(run_id) or not isinstance(value, dict) or set(value) != RUN_FIELDS or value.get("schemaVersion") != 1 or value.get("id") != run_id:
        raise InspectError("INSPECT_INVALID_DATA", "Run 结构无效", source=str(path))
    if not isinstance(value.get("workflow"), str) or not isinstance(value.get("stages"), dict):
        raise InspectError("INSPECT_INVALID_DATA", "Run 字段无效", source=str(path))
    for field in ("featureSlug", "repository", "branch"):
        if value[field] is not None and not isinstance(value[field], str):
            raise InspectError("INSPECT_INVALID_DATA", "Run 上下文字段无效", source=str(path))
    for stage, record in value["stages"].items():
        if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", stage):
            raise InspectError("INSPECT_INVALID_DATA", "Run stage id 无效", source=str(path))
        if not isinstance(record, dict) or set(record) != STAGE_RECORD_FIELDS:
            raise InspectError("INSPECT_INVALID_DATA", "Run stage 结构无效", source=str(path))
        if not isinstance(record["fingerprint"], str) or not FINGERPRINT_RE.fullmatch(record["fingerprint"]) or record["status"] not in RUN_STATUSES or not isinstance(record["updatedAt"], str) or not record["updatedAt"].endswith("Z") or not isinstance(record["summary"], str):
            raise InspectError("INSPECT_INVALID_DATA", "Run stage 字段无效", source=str(path))
    return value, data


def _feature_root(root: Path) -> Path:
    return features_root(root) if (state_root(root) / "workspace.json").is_file() else root / "docs" / "development" / "features"


def _feature_dir(root: Path, slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise InspectError("INSPECT_INVALID_ARGUMENT", "feature slug 无效")
    path = _feature_root(root) / slug
    if path.is_symlink() or not path.is_dir():
        raise InspectError("INSPECT_NOT_FOUND", f"需求不存在：{slug}", source=slug)
    return path


def _mode(root: Path) -> str:
    registry = state_root(root) / "workspace.json"
    if registry.exists() or registry.is_symlink():
        if registry.is_symlink() or not registry.is_file():
            raise InspectError("INSPECT_INVALID_DATA", "workspace.json 必须是普通文件", source=".workspace/workspace.json")
        return "workspace"
    return "maintenance"


def _title_description(readme: Path, root: Path, text: str | None = None, deadline: Deadline | None = None) -> tuple[str, str, dict[str, object] | None]:
    text = _read(readme, root, deadline=deadline).decode("utf-8") if text is None else text
    fenced = False
    title = None
    description = None
    for line_no, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = H1.match(line)
        if title is None and match:
            title = match.group(1)
            continue
        candidate = line.strip()
        if description is None and candidate and not candidate.startswith(("#", "-", "*", ">")):
            description = {"content": candidate[:500], "line": line_no, "truncated": len(candidate) > 500}
    return title or readme.parent.name, "heading" if title else "slug", description


def _file_info(feature: Path, relative: str, root: Path, deadline: Deadline | None = None) -> dict[str, object]:
    path = feature / relative
    value: dict[str, object] = {"path": relative, "exists": False, "type": Path(relative).suffix.removeprefix(".") or "markdown"}
    if path.exists() and not path.is_symlink() and path.is_file():
        try:
            data = _read(path, root, deadline=deadline)
            value.update({"exists": True, "bytes": len(data), "revision": _revision(data)})
        except InspectError as exc:
            value["diagnostic"] = _diag(exc.code, str(exc), relative)
    return value


def _linked_files(feature: Path, root: Path, deadline: Deadline | None = None) -> set[str]:
    result = set(STANDARD_FILES)
    for relative in STANDARD_FILES:
        path = feature / relative
        if not path.is_file() or path.is_symlink(): continue
        try:
            if deadline: deadline.check()
            for target in LINK.findall(_read(path, root, deadline=deadline).decode("utf-8")):
                resolved = (path.parent / target).resolve()
                if resolved.is_relative_to(feature.resolve()) and resolved.is_file() and not resolved.is_symlink():
                    result.add(resolved.relative_to(feature.resolve()).as_posix())
        except (InspectError, UnicodeError): pass
    return result


def _feature_revision(feature: Path, root: Path, deadline: Deadline | None = None) -> str:
    entries: list[tuple[str, str]] = []
    for rel in sorted(STANDARD_FILES):
        path = feature / rel
        if path.is_file() and not path.is_symlink():
            entries.append((rel, _revision(_read(path, root, deadline=deadline))))
        else:
            entries.append((rel, "missing"))
    return _revision(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode())


def _scan(paths, deadline: Deadline | None = None) -> list[Path]:
    result = []
    for path in paths:
        if deadline:
            deadline.check()
        if len(result) >= MAX_SCAN:
            raise InspectError("INSPECT_LIMIT_EXCEEDED", "目录项超过读取限制")
        result.append(path)
    return sorted(result)


def _inspect_artifacts(feature: Path, root: Path, deadline: Deadline | None = None) -> list[dict[str, object]]:
    base = feature / "artifacts"
    if not base.exists(): return []
    if base.is_symlink() or not base.is_dir(): raise InspectError("INSPECT_UNSAFE_PATH", "artifacts 目录不安全", source=str(base))
    paths = _scan(base.rglob("*"), deadline)
    result = []
    for path in sorted(paths):
        if deadline: deadline.check()
        if path.is_symlink(): raise InspectError("INSPECT_UNSAFE_PATH", "artifact 不允许符号链接", source=str(path))
        if path.is_file():
            result.append({"path": path.relative_to(feature).as_posix(), "type": path.suffix.removeprefix(".") or "other", "bytes": path.stat().st_size})
    return result


def _maintenance_all(root: Path, deadline: Deadline | None = None) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    # status deliberately hides done; Inspect needs the same parser plus all lifecycle records.
    base = root / "docs" / "development" / "features"
    values, bad = [], []
    if not base.is_dir() or base.is_symlink():
        return values, bad
    children = _scan(base.iterdir(), deadline)
    for feature in children:
        try:
            if deadline: deadline.check()
            readme = feature / "README.md"
            meta = feature_metadata(readme, _read(readme, root, deadline=deadline).decode("utf-8"))
            if not feature.is_dir() or feature.is_symlink() or meta.get("需求短名") != feature.name or meta.get("状态") not in FEATURE_STATUSES:
                raise ValueError("维护需求元数据无效")
            values.append({"featureSlug": feature.name, "path": feature.relative_to(root).as_posix(), "status": meta["状态"], "repositories": [root.name], "branches": [[root.name, meta.get("工作分支", "")]], "baseBranches": [[root.name, meta.get("基线分支", "")]], "lastUpdated": meta.get("最后更新", "")})
        except (OSError, UnicodeError, ValueError) as exc:
            bad.append({"path": feature.relative_to(root).as_posix(), "error": str(exc)})
    return values, bad


def _features(root: Path, deadline: Deadline | None = None) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    if _mode(root) == "maintenance":
        return _maintenance_all(root, deadline)
    summaries, bad = list_features_lenient(
        root, max_entries=MAX_SCAN, check=deadline.check if deadline else None,
        readme_reader=lambda path: _read(path, root, deadline=deadline).decode("utf-8"),
    )
    values = []
    for summary in summaries:
        item = summary_payload(summary)
        item["path"] = summary.path.relative_to(root).as_posix()
        values.append(item)
    return values, bad


def _summary(root: Path, item: dict[str, object], deadline: Deadline | None = None) -> dict[str, object]:
    feature = root / str(item["path"])
    readme_text = _read(feature / "README.md", root, deadline=deadline).decode("utf-8")
    title, title_source, _ = _title_description(feature / "README.md", root, readme_text, deadline)
    branches, bases = dict(item.get("branches", [])), dict(item.get("baseBranches", []))
    plan_path = feature / "plans" / "implementation.md"
    plan_text = _read(plan_path, root, deadline=deadline).decode("utf-8") if plan_path.is_file() else None
    plan = plan_analysis(
        plan_path, plan_text, repositories=set(item["repositories"])
    )
    reviews, _, _ = document_reviews(feature, readme_text)
    verification_path = feature / "testing" / "verification.md"
    verification_text = _read(verification_path, root, deadline=deadline).decode("utf-8") if verification_path.is_file() else None
    verification = verification_record(feature, verification_text)
    progress = {"completed": sum(bool(task["completed"]) for task in plan["tasks"]), "total": len(plan["tasks"])}
    trusted, _, evidence_diagnostics = _task_evidence_state(
        root, _mode(root), feature, item, plan, verification_path
    )
    plan_summary = {
        "exists": plan_path.is_file(),
        **progress,
        "diagnostics": [*plan["diagnostics"], *evidence_diagnostics],
    }
    if plan["completionPolicy"] == "task-evidence-v1":
        plan_summary.update(
            {
                "completionPolicy": plan["completionPolicy"],
                "trustedProgress": trusted,
            }
        )
    return {"slug": item["featureSlug"], "title": title, "titleSource": title_source, "status": item["status"], "path": item["path"], "lastUpdated": item["lastUpdated"], "repositoryBindings": [{"repository": repo, "workBranch": branches.get(repo), "baseBranch": bases.get(repo), "source": "README.md"} for repo in item["repositories"]], "planSummary": plan_summary, "documentReviews": reviews, "verificationSummary": {"exists": verification is not None, "codeState": "not_checked"}}


def workspace(root: Path, deadline: Deadline | None = None) -> dict[str, object]:
    mode = _mode(root)
    version_file = root / "VERSION"
    kit_version = _read(version_file, root, deadline=deadline).decode("utf-8").strip() if version_file.is_file() else f"{VERSION}.0"
    protocol = {"operations": ["workspace", "features", "feature", "document", "verification", "handoff", "search", "workflow", "runs", "run"], "apiVersion": {"major": API_MAJOR, "minor": API_MINOR}, "limits": {"maxFileBytes": MAX_FILE, "maxResponseBytes": MAX_RESPONSE, "maxPageSize": MAX_PAGE, "maxDirectoryEntries": MAX_SCAN}, "kitVersion": kit_version}
    if mode == "maintenance":
        return {"mode": mode, "identity": {"name": root.name}, "repositories": [{"id": root.name, "role": "kit", "absolutePath": str(root), "aliases": [], "category": "kit", "description": "agent-workbench Kit", "availability": "present", "effectiveBranchPolicy": None, "policySources": {}}], "localContext": {"activeFeature": None, "branchOwner": None, "primaryRole": None, "sources": {}}, "configuration": {"providers": {}, "repositoryOverrides": {}, "unknownExtensionConfig": {"keys": [], "valuesOmitted": True}}, "protocol": protocol}
    # Inspect may show unknown extension configuration, but must never echo its values.
    raw_workspace = parse_json_bytes(_read(state_root(root) / "workspace.json", root, deadline=deadline), "workspace.json")
    try:
        model = parse_workspace(raw_workspace, root)
    except WorkspaceError:
        safe_workspace = dict(raw_workspace)
        extensions = safe_workspace.get("extensions")
        if not isinstance(extensions, dict):
            raise
        safe_workspace["extensions"] = {"providers": extensions.get("providers", {}), "config": {}}
        model = parse_workspace(safe_workspace, root, validate_extension_refs=False)
    local = load_local_settings(root, required=False)
    repos = []
    for index, repo in enumerate(model.repositories):
        policy = effective_branch_policy(model, repo)
        path = repository_path(model, repo)
        sources = {}
        raw_policy = raw_workspace.get("repositories", [])[index].get("branchPolicy", {})
        raw_workspace_policy = raw_workspace.get("branchPolicy", {})
        for key in policy.as_dict():
            if isinstance(raw_policy, dict) and key in raw_policy: source, pointer = "repository", f"/repositories/{index}/branchPolicy/{key}"
            elif isinstance(raw_workspace_policy, dict) and key in raw_workspace_policy: source, pointer = "workspace", f"/branchPolicy/{key}"
            else: source, pointer = "kit-default", None
            sources[key] = {"source": source, "path": ".workspace/workspace.json" if pointer else "VERSION", "pointer": pointer}
        repos.append({"id": repo.path, "role": "business", "absolutePath": str(path), "aliases": list(repo.aliases), "category": repo.category, "description": repo.description, "availability": "present" if path.is_dir() else "missing", "effectiveBranchPolicy": policy.as_dict(), "policySources": sources})
    try: ext = extension_status(root)
    except (WorkspaceError, ValueError): ext = {"activeIds": [], "providers": {}, "repositoryOverrides": {}, "blockedCodes": ["EXTENSION_CONFIG_UNKNOWN"]}
    raw_config = raw_workspace.get("extensions", {}).get("config", {}) if isinstance(raw_workspace.get("extensions"), dict) else {}
    unknown = {name: {key: type(value).__name__ for key, value in config.items()} for name, config in raw_config.items() if isinstance(name, str) and isinstance(config, dict)}
    kit = {"id": root.name, "role": "kit", "absolutePath": str(root), "aliases": [], "category": "kit", "description": "agent-workbench Kit", "availability": "present", "effectiveBranchPolicy": None, "policySources": {}}
    return {"mode": mode, "identity": model.identity.as_dict(), "repositories": [kit, *repos], "localContext": {"activeFeature": local.active_feature if local else None, "branchOwner": local.branch_owner if local else None, "primaryRole": local.primary_role if local else None, "sources": {"activeFeature": ".workspace/workspace.local.json", "branchOwner": ".workspace/workspace.local.json", "primaryRole": ".workspace/workspace.local.json"}}, "configuration": {"providers": ext["providers"], "repositoryOverrides": ext["repositoryOverrides"], "extensionStatus": {"activeIds": ext["activeIds"], "blockedCodes": ext["blockedCodes"]}, "unknownExtensionConfig": {"keys": unknown, "valuesOmitted": True}}, "protocol": protocol}


def features(root: Path, status: str | None, offset: int, limit: int, deadline: Deadline | None = None) -> dict[str, object]:
    if status is not None and status not in FEATURE_STATUSES:
        raise InspectError("INSPECT_INVALID_ARGUMENT", "status 无效")
    if offset < 0 or not 1 <= limit <= MAX_PAGE:
        raise InspectError("INSPECT_INVALID_ARGUMENT", "分页参数无效")
    items, bad = _features(root, deadline)
    if status: items = [item for item in items if item["status"] == status]
    payload, diagnostics = [], [_diag("INSPECT_INVALID_DATA", x["error"], x["path"]) for x in bad]
    for item in items:
        try:
            payload.append(_summary(root, item, deadline))
        except (InspectError, OSError, UnicodeError, ValueError) as exc:
            diagnostics.append(_diag("INSPECT_INVALID_DATA", "无法读取需求记录", str(item.get("path"))))
    # The collection revision must detect in-place content changes, not only recorded dates.
    source_revisions = []
    for item in items:
        try:
            source_revisions.append((item["featureSlug"], _feature_revision(root / str(item["path"]), root, deadline)))
        except (InspectError, OSError, UnicodeError):
            source_revisions.append((item["featureSlug"], "unreadable"))
    rev = _revision(json.dumps({"items": payload, "sources": source_revisions}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    counts = {"parsed": len(payload), "diagnostics": len(diagnostics), **{state: sum(1 for item in payload if item["status"] == state) for state in sorted(FEATURE_STATUSES)}}
    return {"items": payload[offset:offset + limit], "counts": counts, "page": {"offset": offset, "limit": limit, "total": len(payload), "hasMore": offset + limit < len(payload)}, "diagnostics": diagnostics, "_collectionRevision": rev}


def feature(root: Path, slug: str, deadline: Deadline | None = None) -> dict[str, object]:
    items, bad = _features(root, deadline)
    item = next((x for x in items if x["featureSlug"] == slug), None)
    if item is None: raise InspectError("INSPECT_NOT_FOUND", f"需求不存在：{slug}")
    directory = _feature_dir(root, slug); revision = _feature_revision(directory, root, deadline); summary = _summary(root, item, deadline); plan_path = directory / "plans" / "implementation.md"; plan_bytes = _read(plan_path, root, deadline=deadline) if plan_path.is_file() else None; plan = plan_analysis(plan_path, plan_bytes.decode("utf-8") if plan_bytes else None, repositories=set(item["repositories"]))
    readme_text = _read(directory / "README.md", root, deadline=deadline).decode("utf-8")
    title, _, description = _title_description(directory / "README.md", root, readme_text, deadline)
    verification_path = directory / "testing/verification.md"
    _, evidence_results, _ = _task_evidence_state(
        root, _mode(root), directory, item, plan, verification_path
    )
    evidence_by_task = {result["taskId"]: result for result in evidence_results}
    tasks = []
    for task in plan["tasks"]:
        evidence = evidence_by_task.get(task["id"])
        value = {
                key: task.get(key)
                for key in ("id", "title", "completed", "dependencies", "references",
                            "startLine", "endLine")
            }
        if plan["completionPolicy"] == "task-evidence-v1":
            value.update(
                {
                    "repository": task["repository"],
                    "validationKind": task["validationKind"],
                    "deliverables": task["deliverables"],
                    "trusted": evidence["trusted"] if evidence is not None else None,
                    "evidenceSource": evidence["source"] if evidence is not None else None,
                    "evidenceDiagnostics": evidence["diagnostics"] if evidence is not None else [],
                }
            )
        value.update(
            {
                "path": "plans/implementation.md",
                "revision": _revision(plan_bytes) if plan_bytes else None,
            }
        )
        tasks.append(value)
    files = [_file_info(directory, rel, root, deadline) for rel in sorted(_linked_files(directory, root, deadline))]
    if item["status"] == "done":
        progression = {"state": "historical", "currentStage": None, "nextActions": [], "blockers": []}
    else:
        p = {"completed": sum(bool(task["completed"]) for task in plan["tasks"]), "total": len(plan["tasks"])}
        reviews, _, recorded = document_reviews(directory, readme_text)
        known = {**item, "progress": p, "documentReviews": reviews, "documentReviewsRecorded": recorded, "planExists": plan_path.is_file(), "designExists": (directory / "design/design.md").is_file(), "verificationPassed": False}
        if plan["completionPolicy"] == "task-evidence-v1":
            known.update({"completionPolicy": plan["completionPolicy"], "trustedProgress": summary["planSummary"]["trustedProgress"], "taskEvidence": evidence_results})
        existing = _single_feature_progress(known, mode=_mode(root))
        progression = {"state": "needs_code_check" if existing["currentStage"] in {"feature.verify", "feature.submit-test", "feature.complete"} else "known", "currentStage": existing["currentStage"], "nextActions": existing["nextActions"], "blockers": existing["blockers"]}
    if _feature_revision(directory, root, deadline) != revision: raise InspectError("INSPECT_INPUT_CHANGED", "Feature 读取期间发生变化", source=str(directory))
    return {"summary": summary, "tasks": tasks, "files": files, "artifacts": _inspect_artifacts(directory, root, deadline), "description": description, "progression": progression, "featureRevision": revision, "diagnostics": [_diag("INSPECT_INVALID_DATA", x["error"], x["path"]) for x in bad if x["path"].endswith(slug)]}


def document(root: Path, slug: str, relative: str, revision: str | None, deadline: Deadline | None = None) -> dict[str, object]:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\x00" in relative:
        raise InspectError("INSPECT_UNSAFE_PATH", "document path 无效")
    directory = _feature_dir(root, slug); path = directory / pure
    linked = _linked_files(directory, root, deadline)
    if relative not in linked and not relative.startswith("artifacts/"):
        raise InspectError("INSPECT_UNSAFE_PATH", "document 不在允许的 Feature 文件集合")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise InspectError("INSPECT_INVALID_ARGUMENT", "document 不是支持的文本类型")
    data = _read(path, root, deadline=deadline); current = _revision(data)
    if revision is not None and revision != current:
        raise InspectError("INSPECT_REVISION_CHANGED", "document revision 已变化", source=relative)
    try: content = data.decode("utf-8")
    except UnicodeDecodeError as exc: raise InspectError("INSPECT_INVALID_DATA", "document 不是 UTF-8 文本", source=relative) from exc
    return {"slug": slug, "path": relative, "mediaType": "text/markdown" if path.suffix == ".md" else "text/plain", "revision": current, "bytes": len(data), "lineCount": len(content.splitlines()), "content": content}


def _source(
    path: Path,
    *,
    root: Path,
    feature_root: Path,
    kind: str,
    deadline: Deadline,
    allowed_roots: Sequence[Path] = (),
) -> tuple[dict[str, object], str]:
    path = path.resolve()
    boundaries = [feature_root, root, *allowed_roots]
    boundary = next(
        (
            candidate.resolve()
            for candidate in boundaries
            if path.resolve().is_relative_to(candidate.resolve())
        ),
        None,
    )
    if boundary is None:
        raise InspectError("INSPECT_UNSAFE_PATH", "接手来源不在允许目录内", source=str(path))
    data = _read(path, boundary, deadline=deadline)
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InspectError("INSPECT_INVALID_DATA", "接手来源不是 UTF-8 文本", source=str(path)) from exc
    relative = (
        path.relative_to(feature_root).as_posix()
        if path.is_relative_to(feature_root)
        else Path(os.path.relpath(path, root)).as_posix()
    )
    return {
        "kind": kind,
        "path": relative,
        "revision": _revision(data),
        "startLine": 1,
        "endLine": max(1, len(content.splitlines())),
    }, content


def _handoff_once(root: Path, slug: str, deadline: Deadline) -> dict[str, object]:
    detail = feature(root, slug, deadline)
    directory = _feature_dir(root, slug)
    compact = (
        brief_result(root, slug, execution=True)
        if detail["summary"]["status"] != "done"
        else {}
    )
    current = compact.get("currentTask")
    current_task = None
    relevant_paths = {
        "README.md",
        "requirements/requirements.md",
        "design/design.md",
        "plans/implementation.md",
    }
    if isinstance(current, dict):
        plan_path = directory / "plans/implementation.md"
        plan_data = _read(plan_path, root, deadline=deadline)
        lines = plan_data.decode("utf-8").splitlines()
        start, end = int(current["startLine"]), int(current["endLine"])
        body = "\n".join(lines[start - 1:end])
        current_task = {
            **current,
            "body": body,
            "referenceIds": sorted(set(REFERENCE_ID.findall(body)), key=str.casefold),
        }
        for reference in current.get("references", []):
            target = str(reference).split("#", 1)[0]
            if not target or "://" in target:
                continue
            resolved = (plan_path.parent / target).resolve()
            if (
                resolved.is_relative_to(directory)
                and resolved.suffix.lower() == ".md"
                and resolved.is_file()
                and not resolved.is_symlink()
            ):
                relevant_paths.add(resolved.relative_to(directory).as_posix())

    allowed_roots: list[Path] = []
    repository = current.get("repository") if isinstance(current, dict) else None
    if _mode(root) == "workspace" and isinstance(repository, str):
        model = load_workspace(root)
        repo = next((item for item in model.repositories if item.path == repository), None)
        if repo is not None:
            allowed_roots.append(repository_path(model, repo))

    sources = []
    for relative in sorted(relevant_paths):
        path = directory / relative
        if not path.is_file() or path.is_symlink():
            continue
        source, _ = _source(
            path,
            root=root,
            feature_root=directory,
            kind="document",
            deadline=deadline,
        )
        sources.append(source)
    instruction = compact.get("instructionContext")
    rules = instruction.get("rules", []) if isinstance(instruction, dict) else []
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("path"), str):
            continue
        source, _ = _source(
            root / rule["path"],
            root=root,
            feature_root=directory,
            kind="rule",
            deadline=deadline,
            allowed_roots=allowed_roots,
        )
        source.update(
            {key: rule[key] for key in ("level", "scope", "repository") if key in rule}
        )
        sources.append(source)

    task_by_id = {task.get("id"): task for task in detail["tasks"]}
    dependencies = [] if current_task is None else [
        {
            "id": dependency,
            "title": task_by_id.get(dependency, {}).get("title"),
            "completed": task_by_id.get(dependency, {}).get("completed"),
            "trusted": task_by_id.get(dependency, {}).get("trusted"),
        }
        for dependency in current_task.get("dependencies", [])
    ]

    verification_path = directory / "testing/verification.md"
    latest_verification = None
    if verification_path.is_file() and not verification_path.is_symlink():
        verification_data = _read(verification_path, root, deadline=deadline)
        verification_text = verification_data.decode("utf-8")
        selected = describe_verification_document(verification_text)["selectedBatch"]
        if selected is not None:
            latest_verification = {
                key: selected.get(key)
                for key in (
                    "recordedAt",
                    "recordedResult",
                    "recordedReview",
                    "completeness",
                    "source",
                )
            }
        sources.append(
            {
                "kind": "verification",
                "path": "testing/verification.md",
                "revision": _revision(verification_data),
                "startLine": 1,
                "endLine": max(1, len(verification_text.splitlines())),
            }
        )

    summary = detail["summary"]
    progression = detail["progression"]
    description = detail.get("description")
    goal = description.get("content") if isinstance(description, dict) else None
    markdown = [
        f"# {summary['title']}",
        "",
        f"- Feature：`{slug}`",
        f"- 状态：`{summary['status']}`",
        f"- 当前阶段：`{progression['currentStage'] or '无'}`",
    ]
    if goal:
        markdown.extend(["", goal])
    if current_task is not None:
        markdown.extend(["", "## 当前任务", "", current_task["body"]])
    if dependencies:
        markdown.extend(
            ["", "## 直接依赖", ""]
            + [
                f"- {item['id']}：{item['title'] or '未找到'}（{'已完成' if item['completed'] else '未完成'}）"
                for item in dependencies
            ]
        )
    if progression["blockers"]:
        markdown.extend(["", "## 阻塞", ""] + [f"- {item}" for item in progression["blockers"]])
    if progression["nextActions"]:
        markdown.extend(
            ["", "## 下一步", ""]
            + [f"- {item['reason']}" for item in progression["nextActions"]]
        )
    if latest_verification is not None:
        markdown.extend(
            [
                "",
                "## 最近验证",
                "",
                f"- 结果：{latest_verification['recordedResult']}",
                f"- 时间：{latest_verification['recordedAt']}",
            ]
        )
    markdown.extend(
        ["", "## 必读来源", ""]
        + [f"- `{item['path']}` @ `{item['revision']}`" for item in sources]
    )
    content = "\n".join(markdown).rstrip() + "\n"
    for item in sources:
        source_path = (
            root / str(item["path"])
            if item["kind"] == "rule"
            else directory / str(item["path"])
        )
        current_source, _ = _source(
            source_path,
            root=root,
            feature_root=directory,
            kind=str(item["kind"]),
            deadline=deadline,
            allowed_roots=allowed_roots,
        )
        if current_source["revision"] != item["revision"]:
            raise InspectError(
                "INSPECT_INPUT_CHANGED",
                "接手来源在生成期间发生变化",
                source=str(source_path),
            )
    if _feature_revision(directory, root, deadline) != detail["featureRevision"]:
        raise InspectError(
            "INSPECT_INPUT_CHANGED", "接手包生成期间 Feature 发生变化", source=str(directory)
        )
    return {
        "slug": slug,
        "title": summary["title"],
        "status": summary["status"],
        "documentReviews": summary["documentReviews"],
        "progression": progression,
        "currentTask": current_task,
        "dependencies": dependencies,
        "latestVerification": latest_verification,
        "sources": sources,
        "content": content,
        "estimatedTokens": estimate_tokens(content)["estTokens"],
        "featureRevision": detail["featureRevision"],
    }


def handoff(root: Path, slug: str, deadline: Deadline | None = None) -> dict[str, object]:
    deadline = deadline or Deadline(NORMAL_TIMEOUT)
    for attempt in range(2):
        try:
            return _handoff_once(root, slug, deadline)
        except InspectError as exc:
            if exc.code != "INSPECT_INPUT_CHANGED" or attempt:
                raise
    raise AssertionError("unreachable")


def _searchable_paths(feature_root: Path, root: Path, deadline: Deadline) -> list[str]:
    paths = {
        relative
        for relative in _linked_files(feature_root, root, deadline)
        if relative.endswith(".md")
        and relative in STANDARD_FILES
        and (feature_root / relative).is_file()
        and not (feature_root / relative).is_symlink()
    }
    design = feature_root / "design"
    if design.is_dir() and not design.is_symlink():
        for path in _scan(design.glob("*.md"), deadline):
            if path.is_file() and not path.is_symlink():
                paths.add(path.relative_to(feature_root).as_posix())
    return sorted(paths)


def search(
    root: Path,
    query: str,
    repository: str | None,
    status: str | None,
    offset: int,
    limit: int,
    deadline: Deadline | None = None,
) -> dict[str, object]:
    deadline = deadline or Deadline(NORMAL_TIMEOUT)
    terms = [term.casefold() for term in query.split() if term]
    if not terms or len(query) > 200 or len(terms) > 10:
        raise InspectError(
            "INSPECT_INVALID_ARGUMENT", "query 必须包含 1 到 10 个关键词且不超过 200 字符"
        )
    if status is not None and status not in FEATURE_STATUSES:
        raise InspectError("INSPECT_INVALID_ARGUMENT", "status 无效")
    if offset < 0 or not 1 <= limit <= 50:
        raise InspectError("INSPECT_INVALID_ARGUMENT", "分页参数无效")
    if repository is not None:
        if _mode(root) == "maintenance":
            if repository != root.name:
                raise InspectError("INSPECT_INVALID_ARGUMENT", "repo 不属于当前工作区")
        else:
            try:
                repository = resolve_repository(load_workspace(root).repositories, repository).path
            except WorkspaceError as exc:
                raise InspectError("INSPECT_INVALID_ARGUMENT", str(exc)) from exc

    values, bad = _features(root, deadline)
    diagnostics = [
        _diag("INSPECT_INVALID_DATA", item["error"], item["path"]) for item in bad
    ]
    matches = []
    revisions = []
    consumed = 0
    scanned = 0
    stopped = False
    for item in values:
        if status is not None and item["status"] != status:
            continue
        if repository is not None and repository not in item["repositories"]:
            continue
        directory = root / str(item["path"])
        try:
            readme_text = _read(directory / "README.md", root, deadline=deadline).decode("utf-8")
            title = _title_description(
                directory / "README.md", root, readme_text, deadline
            )[0]
            paths = _searchable_paths(directory, root, deadline)
        except (InspectError, OSError, UnicodeError, ValueError):
            diagnostics.append(
                _diag("INSPECT_INVALID_DATA", "无法读取需求记录", str(item["path"]))
            )
            continue
        for relative in paths:
            scanned += 1
            if scanned > MAX_SCAN:
                diagnostics.append(
                    _diag("INSPECT_LIMIT_EXCEEDED", "搜索文件数量超过限制")
                )
                stopped = True
                break
            try:
                data = _read(directory / relative, root, deadline=deadline)
                consumed += len(data)
                if consumed > MAX_SEARCH_BYTES:
                    diagnostics.append(
                        _diag("INSPECT_LIMIT_EXCEEDED", "搜索累计读取超过限制")
                    )
                    stopped = True
                    break
                text = data.decode("utf-8")
            except (InspectError, OSError, UnicodeError) as exc:
                code = exc.code if isinstance(exc, InspectError) else "INSPECT_INVALID_DATA"
                diagnostics.append(
                    _diag(code, "无法搜索文档", f"{item['path']}/{relative}")
                )
                continue
            revision = _revision(data)
            revisions.append((item["featureSlug"], relative, revision))
            folded = text.casefold()
            if not all(term in folded for term in terms):
                continue
            lines = text.splitlines()
            title_line = lines[0].lstrip("# ").casefold() if lines else ""
            headings = [
                (number, line.lstrip("# ").strip())
                for number, line in enumerate(lines, 1)
                if line.startswith("#")
            ]
            heading_match = next(
                (
                    (number, line)
                    for number, line in headings
                    if all(term in line.casefold() for term in terms)
                ),
                None,
            )
            if all(term in title_line for term in terms):
                match_kind, line_number, heading = (
                    "title",
                    1,
                    lines[0].lstrip("# ") if lines else title,
                )
            elif heading_match is not None:
                match_kind, line_number, heading = (
                    "heading",
                    heading_match[0],
                    heading_match[1],
                )
            else:
                line_number = next(
                    (
                        number
                        for number, line in enumerate(lines, 1)
                        if any(term in line.casefold() for term in terms)
                    ),
                    1,
                )
                match_kind = "body"
                heading = next(
                    (line for number, line in reversed(headings) if number <= line_number),
                    None,
                )
            matching_lines = [line.strip() for line in lines if any(term in line.casefold() for term in terms)]
            snippet = "\n".join(matching_lines[:3]).strip()[:500]
            matches.append(
                {
                    "slug": item["featureSlug"],
                    "title": title,
                    "status": item["status"],
                    "lastUpdated": item["lastUpdated"],
                    "repositories": item["repositories"],
                    "path": relative,
                    "line": line_number,
                    "heading": heading,
                    "snippet": snippet,
                    "revision": revision,
                    "matchKind": match_kind,
                }
            )
        if stopped:
            break
    rank = {"title": 0, "heading": 1, "body": 2}
    matches.sort(
        key=lambda item: (
            rank[item["matchKind"]],
            -int(str(item["lastUpdated"]).replace("-", ""))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item["lastUpdated"]))
            else 0,
            str(item["slug"]),
            str(item["path"]),
            int(item["line"]),
        )
    )
    collection_revision = _revision(
        json.dumps(
            {
                "query": terms,
                "repository": repository,
                "status": status,
                "sources": revisions,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return {
        "query": query,
        "items": matches[offset:offset + limit],
        "counts": {
            "parsed": len(matches),
            "diagnostics": len(diagnostics),
            "documents": scanned,
        },
        "page": {
            "offset": offset,
            "limit": limit,
            "total": len(matches),
            "hasMore": offset + limit < len(matches),
        },
        "diagnostics": diagnostics,
        "_collectionRevision": collection_revision,
    }


def verification(root: Path, slug: str, check_code: bool, deadline: Deadline | None = None) -> dict[str, object]:
    detail = feature(root, slug, deadline); directory = _feature_dir(root, slug); mode = _mode(root)
    document_text = _read(directory / "testing" / "verification.md", root, deadline=deadline).decode("utf-8") if (directory / "testing" / "verification.md").is_file() else ""
    record = verification_record(directory, document_text if document_text else None)
    described = describe_verification_document(document_text)
    states: list[dict[str, object]] = []
    result = "unknown"
    if record:
        header = record.split("### 检查", 1)[0]
        if "- 总体结果：通过" in header: result = "passed"
        elif "- 总体结果：失败" in header: result = "failed"
    applicability = "historical" if detail["summary"]["status"] == "done" else "not_checked"
    if check_code and detail["summary"]["status"] != "done" and record:
        try:
            item = next(x for x in _features(root, deadline)[0] if x["featureSlug"] == slug)
            expected = dict(item["branches"])
            recorded = {}
            if described["selectedBatch"] is not None:
                raw = described["selectedBatch"].get("raw", "")
                match = re.search(r"^- 代码状态：(.*)$", raw, re.MULTILINE)
                if match:
                    try: recorded = json.loads(match.group(1))
                    except json.JSONDecodeError: recorded = {}
            current: dict[str, str] = {}
            record_valid = verification_passed(record, recorded) if recorded else False
            for name in item["repositories"]:
                try:
                    if mode == "maintenance": path = root
                    else:
                        model = load_workspace(root)
                        repo = next(repo for repo in model.repositories if repo.path == name)
                        path = repository_path(model, repo)
                    branch = _git_read(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"], deadline or Deadline(CODE_TIMEOUT)).stdout.strip()
                    head = _git_read(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], deadline or Deadline(CODE_TIMEOUT)).stdout.strip()
                    if branch != expected.get(name):
                        states.append({"repository": name, "state": "unknown", "reasonCodes": ["INSPECT_BRANCH_NOT_CHECKED_OUT"], "recordedFingerprint": recorded.get(name), "currentFingerprint": None, "currentHead": head or None, "source": "git"})
                        continue
                    current.update(feature_code_state(root, mode, {**item, "repositories": [name]}, timeout=(deadline.remaining() if deadline else CODE_TIMEOUT), inspect_budget=True))
                    end_head = _git_read(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], deadline or Deadline(CODE_TIMEOUT)).stdout.strip()
                    end_branch = _git_read(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"], deadline or Deadline(CODE_TIMEOUT)).stdout.strip()
                    repeat = feature_code_state(root, mode, {**item, "repositories": [name]}, timeout=(deadline.remaining() if deadline else CODE_TIMEOUT), inspect_budget=True)[name]
                    if head != end_head or branch != end_branch or current[name] != repeat:
                        states.append({"repository": name, "state": "unknown", "reasonCodes": ["INSPECT_INPUT_CHANGED"], "recordedFingerprint": recorded.get(name), "currentFingerprint": None, "currentHead": end_head or None, "source": "git"})
                        continue
                    expected_fingerprint = recorded.get(name)
                    state = "matched" if isinstance(expected_fingerprint, str) and expected_fingerprint == current[name] else "changed"
                    states.append({"repository": name, "state": state, "reasonCodes": [] if state == "matched" else ["INSPECT_FINGERPRINT_CHANGED"], "recordedFingerprint": expected_fingerprint, "currentFingerprint": current[name], "currentHead": head or None, "source": "git"})
                except Exception:
                    states.append({"repository": name, "state": "unknown", "reasonCodes": ["INSPECT_INVALID_DATA"], "recordedFingerprint": recorded.get(name), "currentFingerprint": None, "currentHead": None, "source": "git"})
            complete = described["selectedBatch"] is not None and described["selectedBatch"].get("completeness") == "complete"
            applicable = verification_passed(record, current)
            applicability = "valid" if record_valid and applicable and len(current) == len(item["repositories"]) and all(row["state"] == "matched" for row in states) else "invalid" if not record_valid or result == "failed" or not complete or any(row["state"] == "changed" for row in states) else "unknown"
        except Exception:
            states.append({"repository": name if 'name' in locals() else None, "state": "unknown", "reasonCodes": ["INSPECT_INVALID_DATA"], "recordedFingerprint": recorded.get(name) if 'name' in locals() else None, "currentFingerprint": None, "currentHead": None, "source": "git"}); applicability = "unknown"
    elif record is None and detail["summary"]["status"] != "done": applicability = "not_checked"
    if not states:
        item = next((x for x in _features(root, deadline)[0] if x["featureSlug"] == slug), None)
        states = [{"repository": name, "state": "not_checked", "reasonCodes": [], "recordedFingerprint": None, "currentFingerprint": None, "currentHead": None, "source": "record"} for name in item["repositories"]] if item else []
    result = {"slug": slug, "featureRevision": detail["featureRevision"], "documentRevision": described["documentRevision"] if document_text else None, "batches": described["batches"], "latestBatchId": described["latestBatchId"], "selectedBatch": described["selectedBatch"], "checkMode": "code_checked" if check_code else "records_only", "repositoryStates": states, "applicability": applicability, "progression": detail["progression"]}
    if detail["summary"]["planSummary"].get("completionPolicy") == "task-evidence-v1":
        result["taskEvidence"] = [
            {
                "taskId": task["id"],
                "trusted": task["trusted"],
                "source": task["evidenceSource"],
                "diagnostics": task["evidenceDiagnostics"],
            }
            for task in detail["tasks"]
            if task["evidenceSource"] is not None
        ]
    return result


def workflow(root: Path, deadline: Deadline | None = None) -> dict[str, object]:
    core = load_core_workflow(CORE_WORKFLOW); state = state_root(root); overlay_path = workflow_file(root)
    def extension_cards() -> list[dict[str, object]]:
        if deadline: deadline.check()
        try:
            manifests = discover_extensions(root)
        except Exception:
            return []
        status = extension_status(root)
        try:
            lock = _read_lock(root)
            locked = {item["id"]: item for item in lock.get("extensions", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        except Exception:
            locked = {}
        cards = []
        for manifest in manifests.values():
            item = locked.get(manifest.id)
            activation = "inactive" if item is None else "matched"
            locked_version = item.get("version") if item else None
            diagnostics = []
            if item is not None and (locked_version != manifest.version or item.get("digest") != extension_digest(manifest.root)):
                activation = "drifted"; diagnostics.append(_diag("EXTENSION_DRIFT", "已锁扩展与当前声明不一致", f"extensions/{manifest.id}"))
            cards.append({"id": manifest.id, "declaredVersion": manifest.version, "lockedVersion": locked_version, "activation": activation, "capabilities": [item.capability for item in manifest.provides], "actions": [{"id": action.action, "title": action.confirmation_title, "effects": list(action.effects)} for action in manifest.actions], "source": f"extensions/{manifest.id}", "diagnostics": diagnostics})
        for extension_id, item in locked.items():
            if extension_id not in manifests:
                cards.append({"id": extension_id, "declaredVersion": None, "lockedVersion": item.get("version"), "activation": "missing", "capabilities": [], "actions": [], "source": "extensions/.state/lock.json", "diagnostics": [_diag("EXTENSION_MISSING", "已锁扩展不存在", "extensions/.state/lock.json")]})
        return cards
    def provider_bindings() -> list[dict[str, object]]:
        status = extension_status(root)
        result = []
        for capability, provider in status["providers"].items():
            result.append({"capability": capability, "provider": provider, "source": "lock"})
        for capability, overrides in status["repositoryOverrides"].items():
            for repository, provider in overrides.items():
                result.append({"capability": capability, "provider": provider, "repository": repository, "source": "lock"})
        return result
    if not overlay_path.is_file() or overlay_path.is_symlink():
        return {"enabled": False, "workflowId": core.id, "configState": "missing", "orderedStages": [{"id": x.id, "core": True, "optional": x.optional, "action": None, "trigger": "manual", "placement": None, "title": x.id, "source": "core"} for x in core.stages], "configuredStages": [], "extensions": extension_cards(), "providerBindings": provider_bindings()}
    try:
        core, overlay, resolved, actions = _resolve(root)
        configured = {item.id: item for item in overlay.stages}
        stages = []
        for item in resolved.stages:
            source = configured.get(item.id)
            stages.append({"id": item.id, "core": item.core, "optional": item.optional, "action": item.action, "trigger": item.trigger or "manual", "placement": {"before": source.before, "after": source.after} if source else None, "title": item.id, "source": "overlay" if source else "core"})
        return {"enabled": True, "workflowId": core.id, "configState": "valid", "orderedStages": stages, "configuredStages": [{"id": x.id, "action": x.uses, "trigger": x.trigger, "placement": {"before": x.before, "after": x.after}} for x in overlay.stages], "extensions": extension_cards(), "providerBindings": provider_bindings()}
    except Exception as exc:
        configured = []
        try:
            raw = json.loads(_read(overlay_path, root, deadline=deadline).decode("utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("stages"), list):
                configured = [{"id": item.get("id"), "action": item.get("uses"), "trigger": item.get("trigger", "manual"), "placement": {"before": item.get("before"), "after": item.get("after")}} for item in raw["stages"] if isinstance(item, dict)]
        except Exception: pass
        return {"enabled": True, "workflowId": core.id, "configState": "invalid", "orderedStages": None, "configuredStages": configured, "extensions": extension_cards(), "providerBindings": provider_bindings(), "diagnostics": [_diag("INSPECT_INVALID_DATA", "Workflow Overlay 无效", ".workspace/workflow.json")]}


def runs(root: Path, feature_slug: str | None, offset: int, limit: int, deadline: Deadline | None = None) -> dict[str, object]:
    deadline = deadline or Deadline(NORMAL_TIMEOUT)
    path = workflow_runs_root(root)
    if not path.exists(): return {"items": [], "counts": {"parsed": 0, "diagnostics": 0}, "page": {"offset": offset, "limit": limit, "total": 0, "hasMore": False}, "_collectionRevision": _revision(b"[]")}
    if path.is_symlink() or not path.is_dir(): raise InspectError("INSPECT_UNSAFE_PATH", "runs 目录不安全")
    items, diagnostics = [], []
    candidates = [item for item in _scan(path.iterdir(), deadline) if item.suffix == ".json"]
    for candidate in candidates:
        try:
            deadline.check(); run, _ = _inspect_run(candidate, root, deadline)
            if feature_slug and run["featureSlug"] != feature_slug: continue
            records = run["stages"]
            items.append({"id": run["id"], "workflow": run["workflow"], "featureSlug": run["featureSlug"], "repository": run["repository"], "branch": run["branch"], "updatedAt": max((x.get("updatedAt") for x in records.values()), default=None), "source": candidate.relative_to(root).as_posix(), "recordCounts": {"total": len(records)}})
        except Exception as exc: diagnostics.append(_diag("INSPECT_INVALID_DATA", str(exc), candidate.relative_to(root).as_posix()))
    return {"items": items[offset:offset + limit], "counts": {"parsed": len(items), "diagnostics": len(diagnostics)}, "page": {"offset": offset, "limit": limit, "total": len(items), "hasMore": offset + limit < len(items)}, "diagnostics": diagnostics, "_collectionRevision": _revision(json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())}


def run(root: Path, run_id: str, deadline: Deadline | None = None) -> dict[str, object]:
    deadline = deadline or Deadline(NORMAL_TIMEOUT)
    if not _valid_run_id(run_id): raise InspectError("INSPECT_INVALID_ARGUMENT", "run id 无效")
    source = workflow_runs_root(root) / f"{run_id}.json"; raw, data = _inspect_run(source, root, deadline)
    try:
        _, overlay, _, actions = _resolve(root)
        stages = {stage.id: stage for stage in overlay.stages}
        records = []
        for stage_id, record in raw["stages"].items():
            stage = stages.get(stage_id)
            if stage is None: match = {"state": "removed", "reasonCodes": ["INSPECT_STAGE_REMOVED"]}
            elif stage.uses not in actions: match = {"state": "unknown", "reasonCodes": ["INSPECT_ACTION_UNAVAILABLE"]}
            else: match = {"state": "matched" if _stage_fingerprint(stage, actions[stage.uses], raw) == record["fingerprint"] else "changed", "reasonCodes": []}
            records.append({"stage": stage_id, **record, "configurationMatch": match})
    except Exception:
        records = [{"stage": stage, **record, "configurationMatch": {"state": "unknown", "reasonCodes": ["INSPECT_CONFIGURATION_UNAVAILABLE"]}} for stage, record in raw["stages"].items()]
    return {"id": raw["id"], "workflow": raw["workflow"], "featureSlug": raw["featureSlug"], "repository": raw["repository"], "branch": raw["branch"], "records": records, "source": source.relative_to(root).as_posix(), "sourceRevision": _revision(data), "rawRecord": raw}


def execute(args: argparse.Namespace) -> dict[str, object]:
    deadline = Deadline(CODE_TIMEOUT if args.operation == "verification" and args.check_code else NORMAL_TIMEOUT)
    root = Path(args.root)
    if args.api_major != API_MAJOR: raise InspectError("INSPECT_UNSUPPORTED_VERSION", f"不支持的 api major：{args.api_major}")
    if root.is_symlink() or not root.is_dir(): raise InspectError("INSPECT_INVALID_ARGUMENT", "root 必须是普通目录")
    root = root.resolve(); op = args.operation
    if op == "workspace": data = workspace(root, deadline)
    elif op == "features": data = features(root, args.status, args.offset, args.limit, deadline)
    elif op == "feature": data = feature(root, args.slug, deadline)
    elif op == "document": data = document(root, args.slug, args.path, args.revision, deadline)
    elif op == "verification": data = verification(root, args.slug, args.check_code, deadline)
    elif op == "handoff": data = handoff(root, args.slug, deadline)
    elif op == "search": data = search(root, args.query, args.repo, args.status, args.offset, args.limit, deadline)
    elif op == "workflow": data = workflow(root, deadline)
    elif op == "runs": data = runs(root, args.feature, args.offset, args.limit, deadline)
    else: data = run(root, args.run_id, deadline)
    deadline.check()
    collection_revision = data.pop("_collectionRevision", None) if isinstance(data, dict) else None
    diagnostics = data.pop("diagnostics", []) if isinstance(data, dict) else []
    revision = collection_revision or _revision(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    return {"apiVersion": {"major": API_MAJOR, "minor": API_MINOR}, "operation": op, "status": "partial" if diagnostics else "ok", "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "root": str(root), "revision": revision, "data": data, "diagnostics": diagnostics}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("--root", type=Path, default=Path.cwd()); p.add_argument("--api-major", type=int, default=API_MAJOR); p.add_argument("--json", action="store_true"); sub = p.add_subparsers(dest="operation", required=True)
    sub.add_parser("workspace"); f = sub.add_parser("features"); f.add_argument("--status"); f.add_argument("--offset", type=int, default=0); f.add_argument("--limit", type=int, default=100)
    x = sub.add_parser("feature"); x.add_argument("slug"); d = sub.add_parser("document"); d.add_argument("slug"); d.add_argument("--path", required=True); d.add_argument("--revision")
    v = sub.add_parser("verification"); v.add_argument("slug"); v.add_argument("--check-code", action="store_true")
    h = sub.add_parser("handoff"); h.add_argument("slug")
    s = sub.add_parser("search"); s.add_argument("--query", required=True); s.add_argument("--repo"); s.add_argument("--status"); s.add_argument("--offset", type=int, default=0); s.add_argument("--limit", type=int, default=20)
    sub.add_parser("workflow"); rs = sub.add_parser("runs"); rs.add_argument("--feature"); rs.add_argument("--offset", type=int, default=0); rs.add_argument("--limit", type=int, default=100); r = sub.add_parser("run"); r.add_argument("run_id")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args: argparse.Namespace | None = None
    try:
        parser = build_parser()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                args = parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raw = list(sys.argv[1:] if argv is None else argv)
            operation = next((value for value in raw if value in {"workspace", "features", "feature", "document", "verification", "handoff", "search", "workflow", "runs", "run"}), None)
            print(json.dumps({"apiVersion":{"major":API_MAJOR,"minor":API_MINOR},"operation":operation,"status":"error","observedAt":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"root":None,"revision":None,"data":None,"diagnostics":[_diag("INSPECT_INVALID_ARGUMENT","参数无效")]}, ensure_ascii=False, separators=(",", ":")))
            return 2
        result = execute(args); text = json.dumps(result, ensure_ascii=False, separators=(",", ":"));
        if len(text.encode()) > MAX_RESPONSE: raise InspectError("INSPECT_LIMIT_EXCEEDED", "响应超过 8 MiB 限制")
        print(text); return 0
    except InspectError as exc:
        root = None
        if args is not None:
            candidate = Path(args.root)
            if candidate.is_dir() and not candidate.is_symlink():
                root = str(candidate.resolve())
        code = 2 if exc.code in {"INSPECT_UNSUPPORTED_VERSION", "INSPECT_INVALID_ARGUMENT"} else 1
        print(json.dumps({"apiVersion": {"major": API_MAJOR, "minor": API_MINOR}, "operation": getattr(args, "operation", None), "status": "error", "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "root": root, "revision": None, "data": None, "diagnostics": [_diag(exc.code, str(exc), exc.source)]}, ensure_ascii=False, separators=(",", ":"))); return code
    except (OSError, UnicodeError, ValueError, WorkspaceError) as exc:
        root = str(Path(args.root).resolve()) if args is not None and Path(args.root).is_dir() and not Path(args.root).is_symlink() else None
        print(json.dumps({"apiVersion": {"major": API_MAJOR, "minor": API_MINOR}, "operation": getattr(args, "operation", None), "status": "error", "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "root": root, "revision": None, "data": None, "diagnostics": [_diag("INSPECT_INVALID_DATA", str(exc))]}, ensure_ascii=False, separators=(",", ":"))); return 1


if __name__ == "__main__": raise SystemExit(main())
