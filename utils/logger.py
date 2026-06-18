"""Centralised logging: rotating daily file + console handler."""

import logging
import pathlib
from datetime import date
from logging.handlers import RotatingFileHandler

from config_loader import get_config

_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    cfg = get_config()
    log_dir = pathlib.Path(cfg["agent_log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"agent-{date.today()}.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        _loggers[name] = logger
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=7, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False

    _loggers[name] = logger
    return logger
