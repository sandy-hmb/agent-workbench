#!/usr/bin/env python3
"""Run the local Workbench CLI without requiring installation."""
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.cli.main import main
if __name__ == '__main__':
    raise SystemExit(main())
