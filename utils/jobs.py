"""Tiny in-memory background job runner.

Report pulls take minutes (6 reports, each request->poll->download). Running them
in a worker thread keeps the HTTP request snappy; the frontend polls status.

Single-process only — fine for this local Flask app.
"""

import threading
import time
import traceback
import uuid

_jobs = {}
_lock = threading.Lock()


def start(target, *args, **kwargs):
    """Run target(progress, *args, **kwargs) in a thread. Returns job_id.

    `progress(msg)` updates the job's status message. The target's return value
    is stored as the job result.
    """
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "id": job_id, "state": "running", "message": "Starting…",
            "result": None, "error": None, "created": time.time(),
        }

    def _progress(msg):
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["message"] = msg

    def _run():
        try:
            result = target(_progress, *args, **kwargs)
            with _lock:
                _jobs[job_id].update(state="done", message="Complete", result=result)
        except Exception as e:  # noqa: BLE001
            with _lock:
                _jobs[job_id].update(
                    state="error", message=str(e), error=traceback.format_exc())

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def get(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        return dict(job)


def public_status(job_id):
    """Status without the (possibly large) result payload."""
    job = get(job_id)
    if not job:
        return None
    return {"id": job["id"], "state": job["state"], "message": job["message"],
            "error": job["error"]}
