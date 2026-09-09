"""Compare real legacy CLI output on identical, isolated input trees."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE_REF = os.environ.get("INSPECT_COMPAT_BASE_REF", "cc361e303a83234975ec33e3a0b182221e3e1351")
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=ENV, text=True, capture_output=True, timeout=40, check=False)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


class InspectCompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if run("git", "cat-file", "-e", BASE_REF + "^{commit}").returncode:
            raise unittest.SkipTest("Compatibility baseline unavailable; provide INSPECT_COMPAT_BASE_REF and full history")
        cls.temporary = tempfile.TemporaryDirectory(prefix="inspect-compat-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.parent = Path(cls.temporary.name).resolve()
        cls.baseline = cls.parent / "baseline"
        cls.baseline.mkdir()
        archive = subprocess.run(
            ["git", "archive", BASE_REF, "scripts", "schemas", "workflows", "VERSION"],
            cwd=ROOT, env=ENV, capture_output=True, timeout=40, check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for member in bundle.getmembers():
                target = cls.baseline / member.name
                if not target.resolve().is_relative_to(cls.baseline):
                    raise AssertionError("Unsafe archive path")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    assert source is not None
                    with source:
                        target.write_bytes(source.read())
                else:
                    raise AssertionError("Unexpected non-regular baseline archive member")

    def setUp(self) -> None:
        self.root = self.parent / self._testMethodName
        self.root.mkdir()
        result = run("git", "init", "-q", "--initial-branch=main", str(self.root))
        self.assertEqual(0, result.returncode, result.stderr)
        write(self.root / ".gitignore", "/.workspace/\n/docs/development/\n")
        for arguments in [("add", ".gitignore"), ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")]:
            result = run("git", "-C", str(self.root), *arguments)
            self.assertEqual(0, result.returncode, result.stderr)

    def make_feature(self, slug: str, *, workspace: bool = False, status: str = "development") -> Path:
        path = self.root / (".workspace/docs/features" if workspace else "docs/development/features") / slug
        bindings = (
            "- 涉及仓库：service\n- 工作分支：service -> main\n- 基线分支：service -> main\n"
            if workspace else f"- 需求短名：`{slug}`\n- 工作分支：`main`\n- 基线分支：`main`\n"
        )
        write(path / "README.md", f"# {slug}\n\n- 状态：{status}\n{bindings}"
              "- 需求审阅：已批准\n- 设计审阅：已批准\n- 计划审阅：已批准\n- 最后更新：2026-09-08\n")
        write(path / "requirements/requirements.md", "# 需求\n\n## R1 行为\n\n验收可复现。\n")
        write(path / "design/design.md", '# 设计\n\n<a id="d1"></a>\n## D1 方案\n\n复用已有实现。\n')
        write(path / "plans/implementation.md", "# 计划\n\n" + "\n\n".join(
            f"- [{'x' if number == 1 else ' '}] T{number:02} 任务 {number}\n\n"
            "  依据：[D1](../design/design.md#d1)\n  依赖：无"
            for number in range(1, 13)
        ) + "\n")
        return path

    def compare(self, arguments: list[str]) -> None:
        before = snapshot(self.root)
        outputs = [
            run(sys.executable, "-B", str(code / "scripts/kit.py"), *arguments)
            for code in (self.baseline, ROOT)
        ]
        old, new = outputs
        self.assertEqual(old.returncode, new.returncode, (old.stderr, new.stderr))
        self.assertEqual(old.stdout, new.stdout, "Legacy stdout changed on identical inputs")
        self.assertEqual(old.stderr, new.stderr, "Legacy stderr changed on identical inputs")
        self.assertEqual(before, snapshot(self.root), "Read-only command changed fixture files or Git state")
        if "--json" in arguments:
            json.loads(new.stdout)

    def test_maintenance_default_and_explicit_brief_match_baseline(self) -> None:
        self.make_feature("sample")
        for args in (
            ["status", "--root", str(self.root), "--json"],
            ["status", "--root", str(self.root)],
            ["brief", "--root", str(self.root), "--json"],
            ["brief", "sample", "--root", str(self.root), "--json"],
            ["brief", "sample", "--root", str(self.root), "--task", "T02", "--json"],
            ["brief", "sample", "--root", str(self.root), "--check", "--json"],
        ):
            with self.subTest(arguments=args):
                self.compare(args)

    def test_registered_multi_feature_queries_match_baseline(self) -> None:
        service = self.parent / "service"
        result = run("git", "init", "-q", "--initial-branch=main", str(service))
        self.assertEqual(0, result.returncode)
        write(service / "source.txt", "fixture\n")
        run("git", "-C", str(service), "add", "source.txt")
        result = run("git", "-C", str(service), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
        self.assertEqual(0, result.returncode, result.stderr)
        write(self.root / ".workspace/workspace.json", json.dumps({
            "version": {"major": 1, "minor": 0}, "workspace": {"name": "Fixture"},
            "context": {}, "branchPolicy": {}, "extensions": {"providers": {}, "config": {}},
            "repositories": [{"path": "service", "aliases": [], "remote": None, "category": "backend", "description": "Fixture", "instruction": "docs/repositories/service.md"}],
        }))
        write(self.root / ".workspace/workspace.local.json", json.dumps({
            "branchOwner": "fixture", "primaryRole": None, "extensions": {}, "activeFeature": "sample",
        }))
        write(self.root / ".workspace/docs/repositories/service.md", "# Service\n")
        self.make_feature("sample", workspace=True)
        self.make_feature("other", workspace=True, status="paused")
        self.make_feature("history", workspace=True, status="done")
        for args in (
            ["status", "--root", str(self.root), "--json"],
            ["brief", "--root", str(self.root), "--json"],
            ["brief", "other", "--root", str(self.root), "--json"],
            ["brief", "sample", "--root", str(self.root), "--task", "T12", "--check", "--json"],
        ):
            with self.subTest(arguments=args):
                self.compare(args)


if __name__ == "__main__":
    unittest.main()
