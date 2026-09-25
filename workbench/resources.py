"""Public resources share one root independently of caller cwd."""
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
