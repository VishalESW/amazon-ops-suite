"""JSON-file archive store for N-gram report metadata (unchanged behaviour)."""

import json
import os

from config import cfg

_ARCHIVE_FILE = os.path.join(cfg.ARCHIVE_FOLDER, "archive.json")


def load_archive():
    if os.path.exists(_ARCHIVE_FILE):
        try:
            with open(_ARCHIVE_FILE, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []
    return []


def save_archive(archive_data):
    os.makedirs(cfg.ARCHIVE_FOLDER, exist_ok=True)
    with open(_ARCHIVE_FILE, "w") as f:
        json.dump(archive_data, f, indent=2)
