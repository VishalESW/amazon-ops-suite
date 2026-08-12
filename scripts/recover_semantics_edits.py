"""Recover / re-key Semantics (and PAT) edits for a project.

WHY: edits used to be stored keyed by row INDEX. Re-uploading files or changing
a file's detected source reorders the Semantics rows, so those index-keyed edits
stop lining up and look "lost" — but _merge_edits never deletes, so the raw edit
values are still in the DB. This tool:

  1. Dumps the stored edit blob so you can SEE the values are still there.
  2. Converts any legacy index-keyed entries to the new keyword/ASIN-keyed format
     using the project's CURRENT row order, and saves them back.

Run ON THE SERVER (it needs the app's DB):

    python -m scripts.recover_semantics_edits <project_id>            # dry run
    python -m scripts.recover_semantics_edits <project_id> --apply    # write back

Dry run by default — it prints what it would do and changes nothing.

NOTE: if the row order changed since the edits were saved, index->keyword
mapping is best-effort. The dump (step 1) always shows every saved value so you
can verify/fix any row by hand. New edits are keyword-keyed and never orphan.
"""
import sys

from utils import campaign_db as cdb
from utils import campaign_builder as cb


def _is_index_keyed(blob):
    return bool(blob) and all(str(k).isdigit() for k in blob)


def recover(pid, apply=False):
    p = cdb.get_project(pid)
    if not p:
        print(f"No project {pid}")
        return
    print(f"Project: {p.get('name')}  (profile: {p.get('profile_name')})\n")

    inp, _meta = cb.assemble(pid)
    order = [(i, str(s.get("keyword", "")).strip()) for i, s in enumerate(inp.semantics_rows)]
    kw_by_index = {str(i): kw for i, kw in order}

    for key_field, state_key, rows_attr in [
        ("keyword", "semantics_edits", inp.semantics_rows),
        ("asin", "pat_edits", inp.pat_targets),
    ]:
        blob = cdb.get_state(pid, state_key) or {}
        if not blob:
            print(f"[{state_key}] empty — nothing stored.")
            continue
        print(f"[{state_key}] {len(blob)} entries stored. Sample values:")
        for k in list(blob)[:8]:
            print(f"    {k}: {blob[k]}")
        if not _is_index_keyed(blob):
            print(f"    -> already identity-keyed; no conversion needed.\n")
            continue
        if state_key == "pat_edits":
            idx_map = {str(i): str(r.get("asin", "")).strip().lower()
                       for i, r in enumerate(rows_attr)}
        else:
            idx_map = {i: kw.lower() for i, kw in kw_by_index.items()}
        converted, missed = {}, []
        for idx, fields in blob.items():
            kv = idx_map.get(str(idx), "")
            if kv:
                converted[kv] = dict(fields, __kw=kv)
            else:
                missed.append(idx)
        print(f"    -> would re-key {len(converted)} entries by {key_field}"
              + (f"; {len(missed)} had no current row: {missed}" if missed else ""))
        if apply and converted:
            # keep any legacy entries we could not map, alongside the converted ones
            merged = dict(converted)
            for idx in missed:
                merged[idx] = blob[idx]
            cdb.save_state(pid, state_key, merged)
            print(f"    -> SAVED {len(converted)} keyword-keyed edits.\n")
        else:
            print("    -> dry run; pass --apply to write back.\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m scripts.recover_semantics_edits <project_id> [--apply]")
        sys.exit(1)
    recover(sys.argv[1], apply="--apply" in sys.argv)
