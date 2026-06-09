"""Campaign Processor v2 — per-project file storage.

Raw uploads + parsed grid tables live on disk under
OUTPUT_FOLDER/../campaign_projects/<pid>/ to keep big tables out of the DB.
The DB (cp_state) only holds lightweight metadata + selections.
"""

import json
import os
import shutil

from config import cfg

ROOT = os.path.join(cfg.DATA_FOLDER, "campaign_projects")


def project_dir(pid):
    d = os.path.join(ROOT, pid)
    os.makedirs(os.path.join(d, "raw"), exist_ok=True)
    os.makedirs(os.path.join(d, "parsed"), exist_ok=True)
    return d


def save_raw(pid, filekey, file_storage):
    d = project_dir(pid)
    ext = os.path.splitext(file_storage.filename or "")[1] or ".bin"
    path = os.path.join(d, "raw", f"{filekey}{ext}")
    file_storage.save(path)
    return path


def raw_path(pid, filekey):
    d = os.path.join(ROOT, pid, "raw")
    if not os.path.isdir(d):
        return None
    for f in os.listdir(d):
        if f.startswith(filekey):
            return os.path.join(d, f)
    return None


def save_parsed(pid, filekey, data):
    d = project_dir(pid)
    with open(os.path.join(d, "parsed", f"{filekey}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def load_parsed(pid, filekey):
    p = os.path.join(ROOT, pid, "parsed", f"{filekey}.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_file(pid, filekey):
    rp = raw_path(pid, filekey)
    if rp and os.path.exists(rp):
        os.remove(rp)
    pp = os.path.join(ROOT, pid, "parsed", f"{filekey}.json")
    if os.path.exists(pp):
        os.remove(pp)


def delete_project(pid):
    d = os.path.join(ROOT, pid)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
