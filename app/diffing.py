"""Change quantification between transcript version snapshots.

Segments carry stable ids (assigned at v1, preserved across edits), so we align
two snapshots by id and report how much changed: segments added/removed/edited,
word- and character-level deltas (via difflib), speaker reassignments, and
speaker-label renames. Used per-version (v_{n-1} → v_n) and summed cumulatively.
"""

from __future__ import annotations

import difflib
import re

_WORD = re.compile(r"\S+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def _word_delta(a: str, b: str) -> tuple[int, int, int]:
    """(added, removed, changed) words between two texts via difflib opcodes."""
    aw, bw = _words(a), _words(b)
    added = removed = changed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aw, bw, autojunk=False).get_opcodes():
        if tag == "insert":
            added += (j2 - j1)
        elif tag == "delete":
            removed += (i2 - i1)
        elif tag == "replace":
            changed += max(i2 - i1, j2 - j1)
    return added, removed, changed


def _char_delta(a: str, b: str) -> int:
    """Total characters inserted/deleted/replaced between two texts."""
    n = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a or "", b or "", autojunk=False).get_opcodes():
        if tag == "insert":
            n += (j2 - j1)
        elif tag == "delete":
            n += (i2 - i1)
        elif tag == "replace":
            n += max(i2 - i1, j2 - j1)
    return n


def _seg_map(data: dict) -> dict[str, dict]:
    return {s.get("id"): s for s in (data or {}).get("segments", []) if s.get("id") is not None}


def _labels(data: dict) -> dict[str, str]:
    out = {}
    for key, info in ((data or {}).get("speakers") or {}).items():
        out[key] = (info or {}).get("label", key) if isinstance(info, dict) else str(info)
    return out


def diff(prev: dict, curr: dict) -> dict:
    """Return change metrics for prev → curr (empty prev = everything is 'added')."""
    pm, cm = _seg_map(prev), _seg_map(curr)
    prev_ids, curr_ids = set(pm), set(cm)

    added_ids = curr_ids - prev_ids
    removed_ids = prev_ids - curr_ids
    common = prev_ids & curr_ids

    segments_edited = 0
    words_added = words_removed = words_changed = 0
    chars_changed = 0
    speaker_reassignments = 0

    for sid in common:
        pa, ca = pm[sid], cm[sid]
        if (pa.get("text") or "") != (ca.get("text") or ""):
            segments_edited += 1
            a, r, c = _word_delta(pa.get("text", ""), ca.get("text", ""))
            words_added += a
            words_removed += r
            words_changed += c
            chars_changed += _char_delta(pa.get("text", ""), ca.get("text", ""))
        if (pa.get("speaker") or "") != (ca.get("speaker") or ""):
            speaker_reassignments += 1

    # new/removed segments contribute their words to added/removed
    for sid in added_ids:
        words_added += len(_words(cm[sid].get("text", "")))
    for sid in removed_ids:
        words_removed += len(_words(pm[sid].get("text", "")))

    # speaker-label renames (same key, changed display label)
    pl, cl = _labels(prev), _labels(curr)
    speakers_renamed = sum(1 for k in (set(pl) & set(cl)) if pl[k] != cl[k])

    # notes (review comments)
    pn = {n.get("id"): n for n in (prev or {}).get("notes", []) if n.get("id")}
    cn = {n.get("id"): n for n in (curr or {}).get("notes", []) if n.get("id")}
    notes_added = len(set(cn) - set(pn))
    notes_resolved = sum(1 for k in (set(pn) & set(cn))
                         if pn[k].get("status") != "resolved" and cn[k].get("status") == "resolved")

    return {
        "segments_added": len(added_ids),
        "segments_removed": len(removed_ids),
        "segments_edited": segments_edited,
        "words_added": words_added,
        "words_removed": words_removed,
        "words_changed": words_changed,
        "chars_changed": chars_changed,
        "speaker_reassignments": speaker_reassignments,
        "speakers_renamed": speakers_renamed,
        "notes_added": notes_added,
        "notes_resolved": notes_resolved,
    }


_METRIC_KEYS = ("segments_added", "segments_removed", "segments_edited", "words_added",
                "words_removed", "words_changed", "chars_changed",
                "speaker_reassignments", "speakers_renamed", "notes_added", "notes_resolved")


def total_edits(metrics: dict) -> int:
    """A single headline number: how many discrete changes this diff represents."""
    return (metrics["segments_added"] + metrics["segments_removed"]
            + metrics["segments_edited"] + metrics["speaker_reassignments"]
            + metrics["speakers_renamed"] + metrics.get("notes_added", 0)
            + metrics.get("notes_resolved", 0))


def cumulative(snapshots: list[dict]) -> dict:
    """Sum per-version diffs across an ordered list of snapshots (only the human
    edits/restores after the first version count as 'changes I made')."""
    agg = {k: 0 for k in _METRIC_KEYS}
    for prev, curr in zip(snapshots, snapshots[1:]):
        d = diff(prev, curr)
        for k in _METRIC_KEYS:
            agg[k] += d[k]
    agg["total_edits"] = total_edits(agg)
    return agg
