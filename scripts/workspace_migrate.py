#!/usr/bin/env python3
"""Preview or apply versioned local workspace migrations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
for path in (SCRIPT_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from migrations.workspace_migrations import apply, preview  # noqa: E402
from workspace_model import VERSION, WorkspaceError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="只读预览 workspace 状态迁移")
    preview.add_argument("--root", type=Path, default=ROOT)
    preview.add_argument("--json", action="store_true")
    apply = commands.add_parser("apply", help="原子写入 .workspace 状态树")
    apply.add_argument("--root", type=Path, default=ROOT)
    apply.add_argument("--preview-hash", required=True)
    apply.add_argument("--backup-dir", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = (
            preview(args.root, target_version=VERSION)
            if args.command == "preview"
            else apply(
                args.root,
                args.preview_hash,
                target_version=VERSION,
                backup_dir=args.backup_dir,
            )
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, UnicodeError, ValueError, WorkspaceError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
