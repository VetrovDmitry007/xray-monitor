# backend/app/common/logging.py
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional
from logging.handlers import TimedRotatingFileHandler

# ---------- Defaults (can be overridden via env) ----------
DEFAULT_LOG_DIR = os.getenv("LOG_DIR", "logs")
DEFAULT_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DEFAULT_BACKUP_DAYS = int(os.getenv("LOG_BACKUP_DAYS", "14"))
# ---------------------------------------------------------


def _ensure_log_dir(log_dir: str | Path) -> Path:
    p = Path(log_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _build_formatter(color: bool = False) -> logging.Formatter:
    # без цветов по умолчанию (читаемо в файлах и CI)
    fmt = "%(asctime)s.%(msecs)03d | %(levelname)s | %(name)s:%(lineno)d | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    return logging.Formatter(fmt=fmt, datefmt=datefmt)


def _make_file_handler(log_dir: Path, level: int) -> TimedRotatingFileHandler:
    """
    app.log -> автоматически будет ротация:
    app.log.2025-09-06, app.log.2025-09-07, ...
    """
    file_path = log_dir / "app.log"
    fh = TimedRotatingFileHandler(
        filename=str(file_path),
        when="midnight",
        interval=1,
        backupCount=DEFAULT_BACKUP_DAYS,
        encoding="utf-8",
        delay=True,
        utc=False,  # ротация по локальному времени
    )
    # Чёткий суффикс для имён файлов
    fh.suffix = "%Y-%m-%d"
    fh.setLevel(level)
    fh.setFormatter(_build_formatter(color=False))
    return fh


def _make_console_handler(level: int) -> logging.StreamHandler:
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(_build_formatter(color=False))
    return ch


def _patch_uvicorn_loggers(level: int) -> None:
    """
    Перенастраиваем uvicorn-логгеры так, чтобы они шли через наш root-логгер
    и не дублировались в консоль/файл.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(level)


def _install_excepthook() -> None:
    def _handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            # позволяем штатно завершаться по Ctrl+C
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logging.getLogger("app").exception(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback)
        )

    sys.excepthook = _handler


def setup_logging(
    level: Optional[str] = None,
    log_dir: Optional[str | Path] = None,
    to_console: bool = True,
) -> None:
    """
    Инициализация логирования приложения.
    Вызывать один раз на старте (самое начало main.py).
    """
    level_name = (level or DEFAULT_LOG_LEVEL).upper()
    numeric_level = logging.getLevelName(level_name)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    log_path = _ensure_log_dir(log_dir or DEFAULT_LOG_DIR)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Чистим прежние хэндлеры (если пересоздаём конфиг)
    for h in list(root.handlers):
        root.removeHandler(h)

    # Файл по дням
    root.addHandler(_make_file_handler(log_path, numeric_level))

    # Консоль
    if to_console:
        root.addHandler(_make_console_handler(numeric_level))

    # Uvicorn/SQLAlchemy/и т.п. — под общий конфиг
    _patch_uvicorn_loggers(numeric_level)

    # Записываем любые необработанные исключения в лог
    _install_excepthook()

    logging.getLogger(__name__).info(
        "Logging initialized: level=%s, dir=%s, console=%s",
        level_name,
        str(log_path.resolve()),
        to_console,
    )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Удобный геттер: get_logger(__name__)
    """
    return logging.getLogger(name or "app")
