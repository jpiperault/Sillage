"""Journal : une ligne par événement sur stdout."""
from __future__ import annotations

import logging
import sys
import time


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        msg = record.getMessage().replace("\n", " ⏎ ")
        line = f"{ts}Z {record.levelname.lower():<7} {record.name}: {msg}"
        if record.exc_info:
            exc = self.formatException(record.exc_info).replace("\n", " ⏎ ")
            line += f" | {exc}"
        return line


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn : pas de journal d'accès (il contient l'IP) ; le nôtre est dans main.py.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("psycopg.pool").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"sillage.{name}")
