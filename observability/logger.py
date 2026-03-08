"""
Logger stile trading con tag colorati: INFO, OK, FILL, HEDG, LOCK, RWRD, CLOS, WARN, ERR.
"""

import logging
import sys
from datetime import datetime


# Colori ANSI
COLORS = {
    "INFO": "\033[36m",    # Cyan
    "OK":   "\033[32m",    # Verde
    "FILL": "\033[33m",    # Giallo
    "HEDG": "\033[35m",    # Magenta
    "LOCK": "\033[32;1m",  # Verde bold
    "RWRD": "\033[34m",    # Blu
    "CLOS": "\033[32;1m",  # Verde bold
    "WARN": "\033[33;1m",  # Giallo bold
    "ERR":  "\033[31;1m",  # Rosso bold
    "KILL": "\033[31;1m",  # Rosso bold
    "RESET": "\033[0m",
}


class TradingFormatter(logging.Formatter):
    def format(self, record):
        timestamp = datetime.now().strftime("%H:%M:%S")
        tag = getattr(record, "tag", "INFO")
        color = COLORS.get(tag, COLORS["INFO"])
        reset = COLORS["RESET"]
        msg = record.getMessage()
        return f"{color}{timestamp} [{tag:4s}]{reset} {msg}"


def setup_logger(name: str = "bot", level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(TradingFormatter())
        logger.addHandler(handler)

    logger.propagate = False
    return logger


# Logger globale
log = setup_logger()


def info(msg: str):
    log.info(msg, extra={"tag": "INFO"})

def ok(msg: str):
    log.info(msg, extra={"tag": "OK"})

def fill(msg: str):
    log.info(msg, extra={"tag": "FILL"})

def hedg(msg: str):
    log.info(msg, extra={"tag": "HEDG"})

def lock(msg: str):
    log.info(msg, extra={"tag": "LOCK"})

def rwrd(msg: str):
    log.info(msg, extra={"tag": "RWRD"})

def clos(msg: str):
    log.info(msg, extra={"tag": "CLOS"})

def warn(msg: str):
    log.warning(msg, extra={"tag": "WARN"})

def err(msg: str):
    log.error(msg, extra={"tag": "ERR"})

def kill(msg: str):
    log.critical(msg, extra={"tag": "KILL"})
