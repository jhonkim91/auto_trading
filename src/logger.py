"""로깅 설정 - 파일(회전) + 콘솔 핸들러."""
import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")


def get_logger(name: str = "autotrader") -> logging.Logger:
    os.makedirs(_LOG_DIR, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:  # 중복 핸들러 방지
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 콘솔: INFO 이상
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 파일: DEBUG 이상, 5MB x 5개 회전 (모든 주문/체결/오류 감사 추적)
    fh = RotatingFileHandler(
        os.path.join(_LOG_DIR, "autotrader.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger
