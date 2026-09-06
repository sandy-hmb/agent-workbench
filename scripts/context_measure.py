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
}

PATHS: dict[str, list[str]] = {
    "init": ["agents_md", "skill_init", "getting_started"],
    "new_feature": ["status_json", "skill_feature_design"],
    "lightweight": ["agents_md", "status_json"],
    "standard": ["status_json", "skill_feature_design"],
    "verify": ["skill_verify", "status_json"],
    "resume": ["status_json", "brief_text"],
}

PATH_LABELS = {
    "init": "初始化",
    "new_feature": "开始一个新需求",
    "lightweight": "轻量改动",
    "standard": "标准需求",
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


def _brief_slug(root: Path, status: object) -> str | None:
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


def build_report(root: Path) -> dict[str, object]:
    components: dict[str, object] = {}
    for component_id, component in COMPONENTS.items():
        if component_id == "brief_text":
            continue
        components[component_id] = measure_component(component_id, component, root)

    status = None
    status_raw = components.get("status_json", {}).get("rawStdout")
    if status_raw:
        try:
            status = json.loads(status_raw)
        except json.JSONDecodeError:
            pass
    slug = _brief_slug(root, status)
    brief = COMPONENTS["brief_text"]
    if slug is None:
        components["brief_text"] = _not_applicable_component(brief, "无法确定唯一需求")
    else:
        components["brief_text"] = measure_component(
            "brief_text", {**brief, "argv": [*brief["argv"], slug]}, root
        )
        components["brief_text"]["applicable"] = True

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    root = Path(args.root).resolve()
    report = build_report(root)

    if args.json:
        print(json.dumps(_json_safe(report), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
