import logging
from pathlib import Path
from datetime import datetime

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured — avoid duplicate handlers on repeated calls
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)   # a fresh clone / CI checkout has no logs/ yet
    log_path = log_dir / f"{name}_{datetime.now():%Y%m%d}.log"
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.setLevel(logging.INFO)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger
