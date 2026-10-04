from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Keep test runs from writing log files into the working copy.
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="soar-jira-sync-logs-"))


@pytest.fixture
def local_test_db_dir() -> Path:
    root = Path(__file__).resolve().parent / ".runtime"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    yield root
    if root.exists():
        shutil.rmtree(root)
