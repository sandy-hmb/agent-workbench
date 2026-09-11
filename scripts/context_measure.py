"""确定性、零依赖的上下文消耗测量脚本（token-measure-v1）。

只读：读取仓内文档、以只读参数调用现有脚本捕获其 stdout，按固定口径把字节数
换算成估算 token 数。不修改任何文件，不改变被调用脚本的行为。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from workspace_local import load_local_settings
from workspace_model import WorkspaceError

ROOT = Path(__file__).resolve().parents[1]

SCHEMA_VERSION = 1

CJK_RANGES = (
    (0x3040, 0x30FF),  # 平假名 + 片假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 统一表意文字
    (0x3000, 0x303F),  # CJK 标点
    (0xFF00, 0xFFEF),  # 全角字符
)

COMPONENTS: dict[str, dict[str, object]] = {
    "agents_md": {"kind": "file", "path": "AGENTS.md"},
    "getting_started": {"kind": "file", "path": "docs/getting-started.md"},
    "skill_init": {"kind": "file", "path": ".agents/skills/workspace-init/SKILL.md"},
    "skill_feature_design": {
        "kind": "file",
        "path": ".agents/skills/workspace-feature-design/SKILL.md",
    },
    "skill_writing_plan": {
        "kind": "file",
        "path": ".agents/skills/workspace-writing-plan/SKILL.md",
    },
    "skill_execute_plan": {
        "kind": "file",
        "path": ".agents/skills/workspace-execute-plan/SKILL.md",
    },
    "skill_verify": {"kind": "file", "path": ".agents/skills/workspace-verify/SKILL.md"},
    "status_json": {
        "kind": "command",
        "argv": ["workspace_status.py", "--root", ".", "--json"],
    },
    "doctor_text": {"kind": "command", "argv": ["workspace_doctor.py", "--root", "."]},
    "setup_init_explain": {
        "kind": "command",
        "argv": ["workspace_setup.py", "init", "explain", "--json"],
    },
    "describe_json": {"kind": "command", "argv": ["kit.py", "describe", "--json"]},
    "brief_text": {"kind": "command", "argv": ["kit.py", "brief"], "optional": True},
    "brief_task": {"kind": "command", "argv": ["kit.py", "brief"], "optional": True},
    "handoff": {"kind": "command", "argv": ["kit.py", "inspect"], "optional": True},
}

PATHS: dict[str, list[str]] = {
    "init": ["agents_md", "skill_init", "getting_started"],
    "new_feature": ["status_json", "skill_feature_design"],
    "lightweight": ["agents_md", "status_json"],
    "standard": ["status_json", "skill_feature_design"],
    "plan": ["status_json", "skill_writing_plan"],
    "implement": ["status_json", "skill_execute_plan"],
    "implement_task": ["status_json", "skill_execute_plan", "brief_task"],
    "verify": ["skill_verify", "status_json"],
    "resume": ["status_json", "brief_text"],
}

PATH_LABELS = {
    "init": "初始化",
    "new_feature": "开始一个新需求",
    "lightweight": "轻量改动",
    "standard": "标准需求",
    "plan": "实施计划",
    "implement": "执行计划",
    "implement_task": "执行计划（展开任务与规范链）",
    "verify": "验证",
    "resume": "续接需求",
}


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in CJK_RANGES)


def estimate_tokens(text: str) -> dict[str, int]:
    cjk_chars = sum(1 for ch in text if is_cjk(ch))
    other_bytes = sum(len(ch.encode("utf-8")) for ch in text if not is_cjk(ch))
    est_tokens = cjk_chars + -(-other_bytes // 4)
    return {
        "bytes": len(text.encode("utf-8")),
        "chars": len(text),
        "cjkChars": cjk_chars,
        "estTokens": est_tokens,
    }


def _run_readonly(argv: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    script = root / "scripts" / argv[0]
    return subprocess.run(
        [sys.executable, str(script), *argv[1:]],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def measure_component(component_id: str, component: dict[str, object], root: Path) -> dict[str, object]:
    if component["kind"] == "file":
        path = root / component["path"]
        text = path.read_text(encoding="utf-8")
        measurement = estimate_tokens(text)
        measurement.update({"kind": "file", "source": component["path"]})
        return measurement

    argv = component["argv"]
    result = _run_readonly(argv, root)
    measurement = estimate_tokens(result.stdout)
    measurement.update(
        {
            "kind": "command",
            "source": "python3 scripts/" + " ".join(argv),
            "exitCode": result.returncode,
            "rawStdout": result.stdout,
        }
    )
    return measurement


def _brief_slug(root: Path, status: object, feature: str | None = None) -> str | None:
    if feature is not None:
        return feature
    if not isinstance(status, dict):
        return None
    features = status.get("features")
    if not isinstance(features, list):
        return None
    slugs = [item.get("featureSlug") for item in features if isinstance(item, dict)]
    slugs = [slug for slug in slugs if isinstance(slug, str)]
    if len(slugs) == 1:
        return slugs[0]
    if status.get("mode") != "workspace":
        return None
    try:
        active = load_local_settings(root, required=False).active_feature
    except WorkspaceError:
        return None
    return active if active in slugs else None


def _instruction_sources(root: Path, payload: object) -> list[dict[str, object]]:
    """列出规则链各条目的实际读取成本；路径不可读时省略该条目。"""
    if not isinstance(payload, dict):
        return []
    entries: list[str] = []
    # 兼容扁平 rules 与早期嵌套形状，只取可读的仓内相对路径
    for rule in payload.get("rules", []) if isinstance(payload.get("rules"), list) else []:
        if isinstance(rule, dict) and isinstance(rule.get("path"), str):
            entries.append(rule["path"])
    for item in payload.get("workspace", []) if isinstance(payload.get("workspace"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            entries.append(item["path"])
    for repository in (
        payload.get("repositories", []) if isinstance(payload.get("repositories"), list) else []
    ):
        if not isinstance(repository, dict):
            continue
        for key in ("governance", "sourceInstruction"):
            if isinstance(repository.get(key), str):
                entries.append(repository[key])
        scoped = repository.get("scopedInstructions")
        entries.extend(item for item in scoped if isinstance(item, str)) if isinstance(scoped, list) else None

    sources = []
    seen = set()
    for relative in entries:
        if relative in seen:
            continue
        seen.add(relative)
        target = root / relative
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        sources.append({"path": relative, "estTokens": estimate_tokens(text)["estTokens"]})
    return sources


def _measure_brief_task(root: Path, slug: str | None) -> dict[str, object]:
    """展开当前任务，测量任务正文与规范链的合计读取成本。"""
    component = COMPONENTS["brief_task"]
    if slug is None:
        return _not_applicable_component(component, "无法确定唯一需求")

    execution = _run_readonly(
        [
            "kit.py", "brief", slug, "--execution", "--json",
            "--projection", "execution",
        ],
        root,
    )
    try:
        current = json.loads(execution.stdout).get("currentTask")
    except (json.JSONDecodeError, AttributeError):
        current = None
    task_id = current.get("id") if isinstance(current, dict) else None
    if not isinstance(task_id, str):
        return _not_applicable_component(component, "当前没有可展开的任务")

    argv = [
        "kit.py", "brief", slug, "--task", task_id, "--execution", "--check",
        "--json", "--projection", "execution",
    ]
    full_argv = [
        "kit.py", "brief", slug, "--task", task_id, "--execution", "--check", "--json",
    ]
    result = _run_readonly(argv, root)
    full_result = _run_readonly(full_argv, root)
    measurement = estimate_tokens(result.stdout)
    brief_tokens = measurement["estTokens"]
    full_brief_tokens = estimate_tokens(full_result.stdout)["estTokens"]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    sources = _instruction_sources(
        root, payload.get("instructionContext") if isinstance(payload, dict) else None
    )
    measurement.update(
        {
            "kind": "command",
            "source": "python3 scripts/" + " ".join(argv),
            "exitCode": result.returncode,
            "rawStdout": result.stdout,
            "applicable": True,
            "taskId": task_id,
            "briefEstTokens": brief_tokens,
            "fullBriefEstTokens": full_brief_tokens,
            "savedBriefEstTokens": max(0, full_brief_tokens - brief_tokens),
            "instructionSources": sources,
            "estTokens": brief_tokens + sum(item["estTokens"] for item in sources),
        }
    )
    return measurement


def _measure_handoff(
    root: Path, slug: str | None, status: dict[str, object] | None
) -> dict[str, object]:
    component = COMPONENTS["handoff"]
    if slug is None or not isinstance(status, dict):
        return _not_applicable_component(component, "无法确定唯一需求")
    argv = [
        "kit.py", "inspect", "--root", ".", "--api-major", "1", "--json",
        "handoff", slug,
    ]
    result = _run_readonly(argv, root)
    response = estimate_tokens(result.stdout)
    try:
        payload = json.loads(result.stdout)
        data = payload["data"]
        sources = data["sources"]
        feature = next(item for item in status["features"] if item["featureSlug"] == slug)
    except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
        return _not_applicable_component(component, "当前 Kit 未返回有效接手包")
    source_measurements = []
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            continue
        path = (
            root / source["path"]
            if source.get("kind") == "rule"
            else root / feature["path"] / source["path"]
        ).resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            tokens = estimate_tokens(path.read_text(encoding="utf-8"))["estTokens"]
        except (OSError, UnicodeError):
            continue
        source_measurements.append(
            {"path": source["path"], "kind": source.get("kind"), "estTokens": tokens}
        )
    response.update(
        {
            "kind": "command",
            "source": "python3 scripts/" + " ".join(argv),
            "exitCode": result.returncode,
            "rawStdout": result.stdout,
            "applicable": result.returncode == 0,
            "responseEstTokens": response["estTokens"],
            "sourceEstTokens": sum(item["estTokens"] for item in source_measurements),
            "sources": source_measurements,
            "estTokens": response["estTokens"]
            + sum(item["estTokens"] for item in source_measurements),
        }
    )
    return response


def _flow_scenario(components: dict[str, object]) -> dict[str, object]:
    """Estimate one handoff, three sequential tasks, and one fresh-session resume."""
    status = int(components["status_json"]["estTokens"])
    brief = components["brief_text"]
    task = components["brief_task"]
    if brief.get("applicable") is False or task.get("applicable") is False:
        return {
            "kind": "static estimate",
            "taskCount": 3,
            "applicable": False,
            "reason": "当前没有可测量的唯一需求和任务",
        }
    brief_tokens = int(brief["estTokens"])
    projected = int(task["briefEstTokens"])
    full = int(task["fullBriefEstTokens"])
    rules = sum(int(item["estTokens"]) for item in task["instructionSources"])
    handoff = components.get("handoff", {})
    sources = handoff.get("sources", []) if handoff.get("applicable") is True else []
    document_sources = sum(
        int(item["estTokens"]) for item in sources if item.get("kind") != "rule"
    )
    handoff_rules = sum(
        int(item["estTokens"]) for item in sources if item.get("kind") == "rule"
    )
    handoff_response = (
        int(handoff["responseEstTokens"])
        if handoff.get("applicable") is True
        else status + brief_tokens
    )
    baseline = 2 * (status + brief_tokens + document_sources) + 3 * (full + rules)
    optimized = 2 * (handoff_response + document_sources + handoff_rules) + 3 * projected
    return {
        "kind": "static estimate",
        "taskCount": 3,
        "applicable": True,
        "firstHandoffEstTokens": status + brief_tokens,
        "newSessionResumeEstTokens": status + brief_tokens,
        "baselineEstTokens": baseline,
        "optimizedEstTokens": optimized,
        "savedEstTokens": baseline - optimized,
        "assumptions": [
            "三个任务使用当前任务的输出体量作为固定样本",
            "两次接手都读取相同的必要文档；旧路径每任务重复规则，新路径每个会话读取一次",
            "不代表模型账单、缓存命中或工具内部读取成本",
        ],
    }


def _not_applicable_component(component: dict[str, object], reason: str) -> dict[str, object]:
    measurement = estimate_tokens("")
    argv = component["argv"]
    measurement.update(
        {
            "kind": "command",
            "source": "python3 scripts/" + " ".join(argv),
            "exitCode": None,
            "rawStdout": "",
            "applicable": False,
            "reason": reason,
            "briefEstTokens": 0,
            "instructionSources": [],
        }
    )
    return measurement


def measure_file_path(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    measurement = estimate_tokens(text)
    measurement["path"] = path.name
    return measurement


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def build_report(root: Path, feature: str | None = None) -> dict[str, object]:
    components: dict[str, object] = {}
    for component_id, component in COMPONENTS.items():
        if component_id in {"brief_text", "brief_task", "handoff"}:
            continue
        components[component_id] = measure_component(component_id, component, root)

    status = None
    status_raw = components.get("status_json", {}).get("rawStdout")
    if status_raw:
        try:
            status = json.loads(status_raw)
        except json.JSONDecodeError:
            pass
    slug = _brief_slug(root, status, feature)
    brief = COMPONENTS["brief_text"]
    if slug is None:
        components["brief_text"] = _not_applicable_component(brief, "无法确定唯一需求")
    else:
        components["brief_text"] = measure_component(
            "brief_text", {**brief, "argv": [*brief["argv"], slug]}, root
        )
        components["brief_text"]["applicable"] = True
    components["brief_task"] = _measure_brief_task(root, slug)
    components["handoff"] = _measure_handoff(root, slug, status)

    paths: dict[str, object] = {}
    for path_name, component_ids in PATHS.items():
        total_bytes = sum(components[cid]["bytes"] for cid in component_ids)
        total_tokens = sum(components[cid]["estTokens"] for cid in component_ids)
        paths[path_name] = {
            "components": list(component_ids),
            "bytes": total_bytes,
            "estTokens": total_tokens,
            "commandCalls": sum(
                components[cid]["kind"] == "command"
                and components[cid].get("applicable") is not False
                for cid in component_ids
            ),
            "notApplicable": [
                cid for cid in component_ids if components[cid].get("applicable") is False
            ],
        }

    skill_files = sorted(root.glob(".agents/skills/*/SKILL.md"))
    skill_measurements = [measure_file_path(path) for path in skill_files]
    all_skills = {
        "files": [path.relative_to(root).as_posix() for path in skill_files],
        "bytes": sum(item["bytes"] for item in skill_measurements),
        "estTokens": sum(item["estTokens"] for item in skill_measurements),
    }

    script_files = sorted(root.glob("scripts/*.py"))
    script_measurements = []
    for path in script_files:
        text = path.read_text(encoding="utf-8")
        measurement = estimate_tokens(text)
        script_measurements.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": measurement["bytes"],
                "lines": text.count("\n") + 1,
                "estTokens": measurement["estTokens"],
            }
        )
    largest = sorted(script_measurements, key=lambda item: item["bytes"], reverse=True)[:5]
    scripts_summary = {
        "totalBytes": sum(item["bytes"] for item in script_measurements),
        "totalLines": sum(item["lines"] for item in script_measurements),
        "totalEstTokens": sum(item["estTokens"] for item in script_measurements),
        "largest": largest,
    }

    active_feature_count = None
    if isinstance(status, dict) and isinstance(status.get("features"), list):
        active_feature_count = len(status["features"])

    repo_state = {
        "headCommit": _git(root, "rev-parse", "HEAD"),
        "workingTreeDirty": bool(_git(root, "status", "--porcelain")),
        "activeFeatureCount": active_feature_count,
    }

    return {
        "schemaVersion": SCHEMA_VERSION,
        "tokenModel": {"cjkCharsPerToken": 1, "otherBytesPerToken": 4},
        "repoState": repo_state,
        "components": components,
        "paths": paths,
        "allSkills": all_skills,
        "scripts": scripts_summary,
        "flowScenario": _flow_scenario(components),
    }


def format_text(report: dict[str, object]) -> str:
    lines: list[str] = []
    state = report["repoState"]
    lines.append("== 上下文消耗基线（token-measure-v1）==")
    lines.append(
        "仓库状态：HEAD={head}  工作区={dirty}  活跃需求={count}".format(
            head=state["headCommit"] or "(unknown)",
            dirty="dirty" if state["workingTreeDirty"] else "clean",
            count=state["activeFeatureCount"],
        )
    )
    model = report["tokenModel"]
    lines.append(
        "token 模型：CJK {cjk} token/字；其余按 UTF-8 {bytes} 字节/token".format(
            cjk=model["cjkCharsPerToken"], bytes=model["otherBytesPerToken"]
        )
    )
    lines.append("")
    lines.append("-- 路径 --")
    for name, entry in report["paths"].items():
        label = PATH_LABELS.get(name, name)
        lines.append(f"{label}\tbytes={entry['bytes']}\testTokens={entry['estTokens']}")

    lines.append(f"全部 Skill 全量加载\tbytes={report['allSkills']['bytes']}\testTokens={report['allSkills']['estTokens']}")

    lines.append("")
    lines.append("-- 命令输出 --")
    for component_id, entry in report["components"].items():
        if entry["kind"] != "command":
            continue
        if entry.get("applicable") is False:
            lines.append(f"{entry['source']}\t不适用：{entry['reason']}")
            continue
        lines.append(
            f"{entry['source']}\tbytes={entry['bytes']}\testTokens={entry['estTokens']}\texitCode={entry['exitCode']}"
        )

    lines.append("")
    lines.append("-- 脚本源码 --")
    scripts_summary = report["scripts"]
    lines.append(
        "scripts/ 合计\tbytes={bytes}\tlines={lines}\testTokens={tokens}".format(
            bytes=scripts_summary["totalBytes"],
            lines=scripts_summary["totalLines"],
            tokens=scripts_summary["totalEstTokens"],
        )
    )
    for item in scripts_summary["largest"]:
        lines.append(
            f"  {item['path']}\tbytes={item['bytes']}\tlines={item['lines']}\testTokens={item['estTokens']}"
        )

    scenario = report["flowScenario"]
    lines.append("")
    lines.append("-- 固定流程场景 --")
    if scenario.get("applicable") is False:
        lines.append(f"不适用：{scenario['reason']}")
    else:
        lines.append(
            "三任务 + 新会话续接\tbaseline={baseline}\toptimized={optimized}\tsaved={saved}".format(
                baseline=scenario["baselineEstTokens"],
                optimized=scenario["optimizedEstTokens"],
                saved=scenario["savedEstTokens"],
            )
        )

    return "\n".join(lines) + "\n"


def _json_safe(report: dict[str, object]) -> dict[str, object]:
    components = {
        component_id: {k: v for k, v in entry.items() if k != "rawStdout"}
        for component_id, entry in report["components"].items()
    }
    return {**report, "components": components}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="测量 Agent 典型路径的上下文消耗基线（只读，零依赖）。")
    parser.add_argument("--root", default=str(ROOT), help="Kit 根目录（默认脚本所在项目目录）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--feature",
        help="显式指定需求短名；多需求工作区无法自动选择时用它测量 brief 相关路径",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.root).resolve()
    report = build_report(root, args.feature)

    if args.json:
        print(json.dumps(_json_safe(report), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
