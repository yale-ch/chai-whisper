"""Unit tests for wish-list features: exporters, scan, notes diff metrics."""

import unittest


DATA = {
    "speakers": {"S0": {"label": "Alice"}, "S1": {"label": "Bob"}},
    "segments": [
        {"id": "seg-0", "start": 1.5, "end": 3.25, "speaker": "S0", "text": "hello world"},
        {"id": "seg-1", "start": 3.3, "end": 5.0, "speaker": "S1", "text": "the sensitive term is here"},
    ],
    "notes": [],
}


class TestExporters(unittest.TestCase):
    def setUp(self):
        from app import exporters
        self.ex = exporters

    def test_vtt(self):
        out = self.ex.to_vtt(DATA)
        self.assertTrue(out.startswith("WEBVTT"))
        self.assertIn("00:00:01.500 --> 00:00:03.250", out)
        self.assertIn("<v Alice>hello world", out)

    def test_srt_uses_comma_ms(self):
        out = self.ex.to_srt(DATA)
        self.assertIn("00:00:01,500 --> 00:00:03,250", out)
        self.assertIn("Alice: hello world", out)

    def test_txt_speaker_headers(self):
        out = self.ex.to_txt(DATA)
        self.assertIn("Alice:", out)
        self.assertIn("Bob:", out)
        self.assertIn("hello world", out)


class TestScan(unittest.TestCase):
    def setUp(self):
        from app import scan
        self.scan = scan

    def test_whole_word_no_substring(self):
        self.assertEqual(self.scan.scan_text("world worker network", ["work"]), [])

    def test_case_insensitive_and_phrase(self):
        hits = self.scan.scan_text("The Sensitive Term appears", ["sensitive term"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["term"].lower(), "sensitive term")

    def test_scan_data_segment_mapping(self):
        res = self.scan.scan_data(DATA, ["sensitive term", "world"], "txt")
        self.assertEqual(res["flag_count"], 2)
        segids = {f["seg_id"] for f in res["segment_flags"]}
        self.assertEqual(segids, {"seg-0", "seg-1"})

    def test_parse_terms_drops_blanks_and_comments(self):
        terms = self.scan.parse_terms("alpha\n\n# comment\nbeta\nAlpha\n")
        self.assertEqual(terms, ["alpha", "beta"])  # dedup case-insensitive, comment/blank dropped


class TestNotesDiff(unittest.TestCase):
    def setUp(self):
        from app import diffing
        self.diffing = diffing

    def test_notes_added_and_resolved(self):
        v1 = {"segments": [{"id": "seg-0", "text": "x"}], "notes": [{"id": "n1", "status": "open"}]}
        v2 = {"segments": [{"id": "seg-0", "text": "x"}],
              "notes": [{"id": "n1", "status": "resolved"}, {"id": "n2", "status": "open"}]}
        d = self.diffing.diff(v1, v2)
        self.assertEqual(d["notes_added"], 1)
        self.assertEqual(d["notes_resolved"], 1)
        self.assertEqual(self.diffing.total_edits(d), 2)


if __name__ == "__main__":
    unittest.main()
