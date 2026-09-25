#!/usr/bin/env python3
"""Run the local Workbench CLI without requiring installation."""
from __future__ import annotations

from pathlib import Path
import sys

sys.dont_write_bytecode = True

MIN_PYTHON = (3, 10)


def python_requirement_error(version_info=None):
    """Return a stable diagnostic before importing modules using newer syntax."""
    value = version_info or sys.version_info
    if (value.major, value.minor) >= MIN_PYTHON:
        return None
    return (
        "KIT_PYTHON_UNSUPPORTED: 需要 Python >= 3.10，"
        f"当前为 Python {value.major}.{value.minor}.{value.micro}"
    )


message = python_requirement_error()
if message:
    sys.stderr.write(message + "\n")
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.cli.main import main
if __name__ == '__main__':
    raise SystemExit(main())
