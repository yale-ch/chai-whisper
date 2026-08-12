"""Unit tests for the CHAI-Whisper workspace: append-only versioning + diff metrics."""

import os
import tempfile
import unittest
from pathlib import Path


class TestDiffing(unittest.TestCase):
    def setUp(self):
        from app import diffing
        self.diffing = diffing

    def test_edit_add_remove_speaker(self):
        v1 = {
            "speakers": {"SPEAKER_00": {"label": "SPEAKER_00"}, "SPEAKER_01": {"label": "SPEAKER_01"}},
            "segments": [
                {"id": "seg-0", "speaker": "SPEAKER_00", "text": "hello world"},
                {"id": "seg-1", "speaker": "SPEAKER_01", "text": "how are you"},
            ],
        }
        v2 = {
            "speakers": {"SPEAKER_00": {"label": "Alice"}, "SPEAKER_01": {"label": "SPEAKER_01"}},
            "segments": [
                {"id": "seg-0", "speaker": "SPEAKER_00", "text": "hello there world"},  # edited
                {"id": "seg-1", "speaker": "SPEAKER_00", "text": "how are you"},        # reassigned
                {"id": "seg-2", "speaker": "SPEAKER_01", "text": "brand new line"},     # added
            ],
        }
        d = self.diffing.diff(v1, v2)
        self.assertEqual(d["segments_added"], 1)
        self.assertEqual(d["segments_removed"], 0)
        self.assertEqual(d["segments_edited"], 1)
        self.assertEqual(d["speaker_reassignments"], 1)
        self.assertEqual(d["speakers_renamed"], 1)  # SPEAKER_00 -> Alice
        self.assertEqual(d["words_added"], 1 + 3)   # "there" + the 3 new-segment words
        self.assertEqual(self.diffing.total_edits(d), 1 + 0 + 1 + 1 + 1)

    def test_removed_segment(self):
        v1 = {"speakers": {}, "segments": [{"id": "seg-0", "text": "a b c"}, {"id": "seg-1", "text": "d"}]}
        v2 = {"speakers": {}, "segments": [{"id": "seg-0", "text": "a b c"}]}
        d = self.diffing.diff(v1, v2)
        self.assertEqual(d["segments_removed"], 1)
        self.assertEqual(d["words_removed"], 1)

    def test_cumulative(self):
        snaps = [
            {"speakers": {}, "segments": [{"id": "seg-0", "text": "one"}]},
            {"speakers": {}, "segments": [{"id": "seg-0", "text": "one two"}]},   # +1 edit
            {"speakers": {}, "segments": [{"id": "seg-0", "text": "one two"}, {"id": "seg-1", "text": "x"}]},  # +1 add
        ]
        agg = self.diffing.cumulative(snaps)
        self.assertEqual(agg["segments_edited"], 1)
        self.assertEqual(agg["segments_added"], 1)
        self.assertEqual(agg["total_edits"], 2)


class TestVersioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CHAI_WHISPER_DATA"] = self.tmp
        # import after env is set so DATA_DIR/DB_PATH point at the temp dir
        import importlib
        import app.db as dbmod
        importlib.reload(dbmod)
        self.db = dbmod
        self.db.init_db()

    def test_append_only_monotonic_versions(self):
        proj = self.db.create_project("P")
        mat = self.db.create_material(proj["id"], "clip.mov", "/tmp/clip.mov", "video")
        v1 = self.db.save_version(mat["id"], {"segments": [{"id": "seg-0", "text": "a"}]}, source="run")
        v2 = self.db.save_version(mat["id"], {"segments": [{"id": "seg-0", "text": "a b"}]}, source="edit")
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v2["version"], 2)
        # restore appends a NEW version, never mutates history
        old = self.db.get_version(mat["id"], 1)
        v3 = self.db.save_version(mat["id"], old["data"], source="restore", note="Restored from v1")
        self.assertEqual(v3["version"], 3)
        self.assertEqual(len(self.db.list_versions(mat["id"])), 3)
        self.assertEqual(self.db.get_version(mat["id"])["version"], 3)  # latest

    def test_material_stores_path_not_bytes(self):
        proj = self.db.create_project("P")
        mat = self.db.create_material(proj["id"], "a.wav", "/some/abs/a.wav", "audio")
        got = self.db.get_material(mat["id"])
        self.assertEqual(got["media_path"], "/some/abs/a.wav")
        self.assertIn("media_exists", got)


if __name__ == "__main__":
    unittest.main()
