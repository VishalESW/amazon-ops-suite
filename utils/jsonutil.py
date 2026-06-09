"""JSON helpers for numpy-typed data.

Installs a global JSONEncoder fallback so jsonify can serialise numpy scalars,
and provides convert_numpy() for deep-converting structures before jsonify.
"""

import json
import numpy as np

_saved_default = json.JSONEncoder.default


def _json_default(self, obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return _saved_default(self, obj)


# Patch once on import.
json.JSONEncoder.default = _json_default


def convert_numpy(obj):
    if isinstance(obj, dict):
        return {k: convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_numpy(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(convert_numpy(v) for v in obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj
