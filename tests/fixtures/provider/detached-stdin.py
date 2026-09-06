#!/usr/bin/env python3
"""Leave a detached child holding stdin without consuming it."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


sentinel = Path(sys.argv[1])
ready_read, ready_write = os.pipe()
if os.fork() == 0:
    os.close(ready_read)
    os.setsid()
    os.write(ready_write, b"ready")
    os.close(ready_write)
    time.sleep(0.8)
    sentinel.write_text("detached child exited", encoding="utf-8")
    os._exit(0)
os.close(ready_write)
os.read(ready_read, 5)
os.close(ready_read)
time.sleep(10)
