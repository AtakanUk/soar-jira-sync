from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from config import get_settings

_initialized = False
_lock = threading.Lock()


def truncate_text(value: Any, max_len: int | None = None) -> str:
    settings = get_settings().logging
    text = str(value)
    limit = max_len if max_len is not None else settings.truncate_length
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def safe_dump(value: Any, max_len: int | None = None) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        rendered = str(value)
    return truncate_text(rendered, max_len=max_len)


class DailyPruningFileHandler(TimedRotatingFileHandler):
    def __init__(
        self, filename: str, max_folder_bytes: int, prune_interval_seconds: int, **kwargs: Any
    ) -> None:
        super().__init__(filename, when="midnight", interval=1, **kwargs)
        self.max_folder_bytes = max_folder_bytes
        self._last_prune_ts = 0.0
        self._prune_interval_seconds = max(60, prune_interval_seconds)
        self._prune_folder_if_needed(force=True)

    def doRollover(self) -> None:  # noqa: N802 - overrides the logging API
        super().doRollover()
        self._prune_folder_if_needed(force=True)

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self._prune_folder_if_needed(force=False)

    def _prune_folder_if_needed(self, force: bool) -> None:
        now = time.time()
        if not force and now - self._last_prune_ts < self._prune_interval_seconds:
            return
        self._last_prune_ts = now

        base_path = Path(self.baseFilename)
        log_dir = base_path.parent
        if not log_dir.exists():
            return

        files: list[tuple[Path, float, int]] = []
        for path in log_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix not in {".log", ".txt"} and ".log." not in path.name:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((path, stat.st_mtime, stat.st_size))

        total_size = sum(item[2] for item in files)
        if total_size <= self.max_folder_bytes:
            return

        files.sort(key=lambda item: item[1])
        active_name = base_path.name
        for path, _, size in files:
            if total_size <= self.max_folder_bytes:
                break
            if path.name == active_name:
                continue
            try:
                os.remove(path)
                total_size -= size
            except OSError:
                continue


def _initialize_logging() -> None:
    global _initialized
    if _initialized:
        return

    settings = get_settings().logging
    with _lock:
        if _initialized:
            return

        log_dir = Path.cwd() / settings.dir_name
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / settings.file

        formatter = logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        root = logging.getLogger()
        root.setLevel(getattr(logging, settings.level.upper(), logging.INFO))
        root.handlers.clear()

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)

        file_handler = DailyPruningFileHandler(
            str(log_path),
            max_folder_bytes=settings.max_folder_bytes,
            prune_interval_seconds=settings.prune_check_interval_seconds,
            backupCount=settings.backup_days,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        root.addHandler(console)
        root.addHandler(file_handler)
        _initialized = True
        root.info("Logging initialized | file=%s", log_path)


def get_logger(name: str = "sync_engine") -> logging.Logger:
    _initialize_logging()
    return logging.getLogger(name)
