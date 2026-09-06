#!/usr/bin/env python3
"""Exceed the runner stdout limit without waiting for stdin."""

from __future__ import annotations

import sys


sys.stdout.buffer.write(b"x" * (1024 * 1024 + 1))
sys.stdout.buffer.flush()
