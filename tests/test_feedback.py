"""Tests for the editor-feedback round: transcript import (vtt/srt/txt, any
encoding), cue-level scan reports + CSV, ISO language codes, IIIF body parsing,
per-recording editors, and TPME provenance records."""

import csv
import io
import os
import tempfile
import unittest

VTT = """WEBVTT
Kind: captions

NOTE a comment
spanning lines

intro
00:01.000 --> 00:04.500 align:start position:10%
<v Jane Doe>Hello &amp; <i>welcome</i>
to the show

2
00:00:05.000 --> 00:00:07.250
<v.loud Bob>Thanks</v>
"""


class TestDecoding(unittest.TestCase):
    def test_encodings(self):
        from app.textio import decode_text
        s = "Olá — café\n"
        for enc in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "cp1252"):
            self.assertEqual(decode_text(s.encode(enc)), s, enc)

    def test_crlf_normalized(self):
        from app.textio import decode_text
        self.assertEqual(decode_text(b"a\r\nb\rc"), "a\nb\nc")


class TestImportParsers(unittest.TestCase):
    def setUp(self):
        from app import ingest
        self.ingest = ingest

    def seg(self, data):
        return [(s["start"], s["end"], s["speaker"], s["text"]) for s in data["segments"]]

    def test_vtt_utf16_voice_tags_settings_notes(self):
        data, fmt, timed = self.ingest.parse_transcript(VTT.encode("utf-16"), "a.vtt")
        self.assertEqual(fmt, "vtt")
        self.assertEqual(self.seg(data), [(1.0, 4.5, "Jane Doe", "Hello & welcome to the show"),
                                          (5.0, 7.25, "Bob", "Thanks")])

    def test_srt_cp1252_speaker_prefix(self):
        srt = "1\r\n00:00:01,000 --> 00:00:02,000\r\nSPEAKER_00: Olá café\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nsecond\r\n"
        data, fmt, _ = self.ingest.parse_transcript(srt.encode("cp1252"), "x.srt")
        self.assertEqual(fmt, "srt")
        self.assertEqual(self.seg(data)[0], (1.0, 2.0, "SPEAKER_00", "Olá café"))

    def test_vtt_content_in_txt_file_is_sniffed(self):
        _, fmt, _ = self.ingest.parse_transcript(VTT.encode(), "captions.txt")
        self.assertEqual(fmt, "vtt")

    def test_txt_bracket_timestamps(self):
        txt = "Interview with X\n\n[00:00:01] Interviewer: Where were you born?\n[00:00:04.5] Jane: In Ohio.\nWe moved later.\n"
        data, fmt, timed = self.ingest.parse_transcript(txt.encode(), "t.txt", duration=60)
        self.assertEqual((fmt, timed), ("txt", True))
        self.assertEqual(self.seg(data)[1][:3], (1.0, 4.5, "Interviewer"))
        self.assertEqual(self.seg(data)[2][2:], ("Jane", "In Ohio. We moved later."))
        self.assertEqual(self.seg(data)[0][3], "Interview with X")  # preamble kept at 0:00

    def test_txt_speaker_header_lines(self):
        txt = "Speaker 1  0:00\nHello there.\n\nSpeaker 2  0:15\nHi! We met at 10:30 today.\n"
        data, _, timed = self.ingest.parse_transcript(txt.encode(), "o.txt", duration=30)
        self.assertTrue(timed)
        self.assertEqual([(s[0], s[2]) for s in self.seg(data)], [(0.0, "Speaker 1"), (15.0, "Speaker 2")])
        self.assertEqual(self.seg(data)[1][3], "Hi! We met at 10:30 today.")

    def test_txt_inline_speaker_paren_time(self):
        data, _, _ = self.ingest.parse_transcript(b"Jane Doe (00:01:23): First.\nJohn (00:01:30): Second.\n", "o.txt")
        self.assertEqual([(s[0], s[2]) for s in self.seg(data)], [(83.0, "Jane Doe"), (90.0, "John")])

    def test_txt_untimed_spreads_over_duration(self):
        txt = "We met at 10:30 that morning.\n\nThen it rained for a long time.\n"
        data, _, timed = self.ingest.parse_transcript(txt.encode("cp1252"), "o.txt", duration=60)
        self.assertFalse(timed)  # a single stray time is not a timestamp column
        segs = self.seg(data)
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0][0], 0.0)
        self.assertAlmostEqual(segs[-1][1], 60.0, places=2)

    def test_bad_json_still_reports_json_error(self):
        import json
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            self.ingest.parse_transcript(b'{"nope": 1}', "x.json")


DATA = {
    "speakers": {"S0": {"label": "Alice"}},
    "segments": [
        {"id": "a", "start": 1.5, "end": 3.25, "speaker": "S0", "text": "hello damn world"},
        {"id": "b", "start": 4.0, "end": 5.0, "speaker": None, "text": "fine, damn it"},
    ],
}


class TestCueScan(unittest.TestCase):
    def setUp(self):
        from app import scan
        self.scan = scan

    def test_vtt_flags_carry_timing_and_cue_text(self):
        r = self.scan.scan_data(DATA, ["damn"], "vtt")
        self.assertEqual(r["flag_count"], 2)
        f = r["flags"][0]
        self.assertEqual((f["cue"], f["start_ts"], f["end_ts"], f["speaker"]), (1, "00:00:01.500", "00:00:03.250", "Alice"))
        self.assertEqual(f["text"], "hello damn world")
        self.assertEqual(f["seg_id"], "a")

    def test_srt_csv(self):
        out = self.scan.report_csv(self.scan.scan_data(DATA, ["damn"], "srt"))
        rows = list(csv.reader(io.StringIO(out.lstrip("﻿"))))
        self.assertEqual(rows[0], self.scan.CSV_COLUMNS)
        self.assertEqual(rows[1], ["srt", "damn", "1", "00:00:01,500", "00:00:03,250", "Alice", "hello damn world"])

    def test_txt_csv_has_context(self):
        out = self.scan.report_csv(self.scan.scan_data(DATA, ["damn"], "txt"))
        rows = list(csv.reader(io.StringIO(out.lstrip("﻿"))))
        self.assertEqual(rows[1][:2], ["txt", "damn"])
        self.assertIn("damn", rows[1][-1])

    def test_report_text_has_timestamps(self):
        txt = self.scan.report_text(self.scan.scan_data(DATA, ["damn"], "vtt"))
        self.assertIn("#1  00:00:01.500 --> 00:00:03.250  [damn]", txt)

    def test_transcript_csv_export(self):
        from app import exporters
        rows = list(csv.reader(io.StringIO(exporters.to_csv(DATA).lstrip("﻿"))))
        self.assertEqual(rows[1], ["1", "00:00:01.500", "00:00:03.250", "Alice", "hello damn world"])


class TestLanguages(unittest.TestCase):
    def test_normalize(self):
        from app.languages import normalize
        for v, want in [("en", "en"), ("eng", "en"), ("EN-us", "en"), ("English", "en"), ("ger", "de"),
                        ("deu", "de"), ("fre", "fr"), ("pt_BR", "pt"), ("jv", "jw"), ("jav", "jw"),
                        ("nb", "no"), ("yue", "yue"), ("haw", "haw"), ("", None), ("auto", None)]:
            self.assertEqual(normalize(v), want, v)
        with self.assertRaises(ValueError):
            normalize("klingon")


class TestIIIFBodies(unittest.TestCase):
    def test_choice_and_separate_sound(self):
        from app.ingest import _iiif_av_canvases
        man = {"items": [
            {"label": {"en": ["c1"]}, "items": [{"items": [{"body": {"type": "Choice", "items": [
                {"id": "https://x/a.mp4", "type": "Video"}, {"id": "https://x/b.m3u8", "type": "Video",
                                                             "format": "application/x-mpegURL"}]}}]}]},
            {"label": "c2", "items": [{"items": [{"body": {"id": "https://x/v.mp4", "type": "Video"}},
                                                 {"body": [{"id": "https://x/s.mp3", "type": "Sound"}]}]}]},
        ]}
        out = list(_iiif_av_canvases(man))
        self.assertEqual([(l, [r["url"] for r in v], [r["url"] for r in s]) for l, v, s in out], [
            ("c1", ["https://x/a.mp4", "https://x/b.m3u8"], []),
            ("c2", ["https://x/v.mp4"], ["https://x/s.mp3"]),
        ])


class TestEditorsAndTPME(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CHAI_WHISPER_DATA"] = self.tmp
        import importlib
        import app.db as dbmod
        importlib.reload(dbmod)
        self.db = dbmod
        self.db.init_db()

    def test_editors_dedup(self):
        p = self.db.create_project("P")
        m = self.db.create_material(p["id"], "a.wav", "/x/a.wav", "audio")
        self.assertEqual(self.db.set_material_editors(m["id"], ["Ann", " ann ", "Bo", ""]), ["Ann", "Bo"])
        self.assertEqual(self.db.get_material(m["id"])["editors"], ["Ann", "Bo"])

    def test_tpme_chain(self):
        from app import tpme
        p = self.db.create_project("P")
        self.db.save_project_settings(p["id"], {"provider": "Test Library"})
        m = self.db.get_material(self.db.create_material(p["id"], "int.wav", "/x/int.wav", "audio")["id"])
        self.db.save_version(m["id"], {**DATA, "language": "en"}, source="run", model="large-v3",
                             meta={"params": {"model": "large-v3", "language": "en", "diarize": True}})
        self.db.save_version(m["id"], DATA, source="edit", editor="Ann", note="manual edit")
        self.db.save_version(m["id"], DATA, source="restore", meta={"restored_from": 1})
        settings = self.db.get_project_settings(p["id"])
        v2 = tpme.build_record(m, self.db.get_version(m["id"], 2), settings, fmt="vtt", file_bytes=b"WEBVTT\n")
        self.assertEqual(v2["transcript_id"], f"{m['id']}-v2")
        self.assertEqual(v2["parent_transcript_id"], f"{m['id']}-v1")
        self.assertEqual(v2["human_review_level"], "partially-edited")  # derived until the user picks
        self.assertEqual(v2["human_agent"], ["Ann"])
        self.assertEqual(v2["media_id"], "int")
        self.assertEqual(v2["originating_media_file"], "int.wav")
        self.assertEqual(v2["media_language"], ["en"])
        self.assertEqual(v2["provider"], "Test Library")
        self.assertEqual(v2["file_format"], "WebVTT")
        self.assertTrue(v2["transcript_checksum"].startswith("sha256:"))
        self.assertTrue(v2["features"]["speaker_identification"])  # "Alice" is a real name
        hist = tpme.history(m, settings)
        self.assertEqual([r["transcript_id"].rsplit("-", 1)[1] for r in hist], ["v3", "v2", "v1"])
        self.assertEqual(hist[0]["human_review_level"], "unedited")  # restored v1 = machine output
        self.assertTrue(all(r["media_language"] == ["en"] for r in hist))  # in every record, not just v1
        asr = [s for s in hist[2]["processing"] if s.get("application_type") == "ASR"][0]
        self.assertEqual(asr["inference_model"], "large-v3")

    def test_tpme_user_fields(self):
        from app import exports, scan, tpme
        p = self.db.create_project("P")
        self.db.save_project_settings(p["id"], {"provider": "Default Lib", "conventions": "Guide v1"})
        mid = self.db.create_material(p["id"], "tape3.mp4", "/x/tape3.mp4", "video",
                                      tpme={"originating_media_file": "tape3.mp4"})["id"]
        self.db.set_material_editors(mid, ["Ann", "Bo"])
        self.db.set_material_tpme(mid, {"media_title": "'Compass', Great Tapes (3 of 3)", "provider": "Fortunoff",
                                        "media_language": tpme.normalize_languages(["yid", "English", "lad"]),
                                        "human_review_level": "full-edited"})
        self.db.save_version(mid, {**DATA, "language": "yi"}, source="run",
                             meta={"params": {"model": "large-v3", "language": "yi"}})
        self.db.save_version(mid, DATA, source="edit", editor="Cy",
                             meta=tpme.version_meta(self.db.get_material(mid)))
        m = self.db.get_material(mid)
        settings = self.db.get_project_settings(p["id"])
        rec = tpme.build_record(m, self.db.get_version(mid, 2), settings)
        self.assertEqual(rec["media_id"], "tape3")
        self.assertEqual(rec["media_title"], "'Compass', Great Tapes (3 of 3)")
        self.assertEqual(rec["originating_media_file"], "tape3.mp4")
        self.assertEqual(rec["provider"], "Fortunoff")          # per-recording override
        self.assertEqual(rec["conventions"], "Guide v1")        # project default
        self.assertEqual(rec["media_language"], ["yi", "en", "lad"])
        self.assertEqual(rec["human_review_level"], "full-edited")
        self.assertEqual(rec["human_agent"], ["Ann", "Bo", "Cy"])
        # Older version keeps its own (derived) level; changing the setting only moves the latest.
        self.db.set_material_tpme(mid, {"human_review_level": "partially-edited"})
        m = self.db.get_material(mid)
        self.assertEqual(tpme.build_record(m, self.db.get_version(mid, 1), settings)["human_review_level"], "unedited")
        self.assertEqual(tpme.build_record(m, self.db.get_version(mid, 2), settings)["human_review_level"], "partially-edited")
        with self.assertRaises(ValueError):
            tpme.normalize_languages(["not a language"])

        # Export documents the scan + lexicons, and can embed the version chain.
        lx = self.db.create_lexicon("Harmful terms", ["Alice"])
        exports.EXPORTS_ROOT = __import__("pathlib").Path(self.tmp) / "exports"
        self.db.save_project_settings(p["id"], {"tpme_history": "embedded"})
        run = exports.write_run(m, 2, DATA, ["vtt"], {"enabled": True, "lexicon_ids": [lx["id"]], "format": "vtt"})
        import json, pathlib
        folder = pathlib.Path(run["folder_path"])
        side = json.loads((folder / "tape3.vtt.tpme.json").read_text())
        step = [s for s in side["processing"] if s["application_type"] == "sensitive-language scan"][0]
        self.assertEqual(step["application_parameters"]["lexicons"], [{"name": "Harmful terms", "term_count": 1}])
        self.assertEqual([r["transcript_id"] for r in side["version_history"]], [f"{mid}-v1"])
        self.assertFalse(list(folder.glob("*.tpme-history.json")))
        # The provenance view picks up the recorded scan.
        self.assertTrue(any(s["application_type"] == "sensitive-language scan"
                            for s in tpme.build_record(m, self.db.get_version(mid, 2), settings,
                                                       scan_summary=tpme.last_scan(mid, 2))["processing"]))


class TestPlaybackAndScript(unittest.TestCase):
    def test_playback_issue(self):
        from app.media import playback_issue
        aac = {"codec": "aac", "profile": "LC", "channels": 2}
        ok = {"has_audio": True, "audio_streams": [aac]}
        self.assertIsNone(playback_issue("a.mp4", ok))
        self.assertIn("2 audio tracks", playback_issue("a.mp4", {"has_audio": True, "audio_streams": [aac, aac]}))
        self.assertIn("6-channel", playback_issue("a.mp4", {"has_audio": True, "audio_streams": [{**aac, "channels": 6}]}))
        self.assertIn("ac3", playback_issue("a.mp4", {"has_audio": True, "audio_streams": [{"codec": "ac3"}]}))
        self.assertIn("Main", playback_issue("a.mp4", {"has_audio": True, "audio_streams": [{**aac, "profile": "Main"}]}))
        self.assertIn(".avi", playback_issue("a.avi", ok))
        self.assertIsNone(playback_issue("a.wav", {"has_audio": True, "audio_streams": [{"codec": "pcm_s16le"}]}))

    def test_script_mismatch(self):
        from app.ingest import script_mismatch
        eng = {"segments": [{"text": "I was born in a small town near Vilna, and my father was a tailor."}]}
        yid = {"segments": [{"text": "איך בין געבוירן געוואָרן אין אַ קליין שטעטל לעבן ווילנע, און מײַן טאַטע איז געווען אַ שנײַדער."}]}
        self.assertIn("translated", script_mismatch("yi", eng))
        self.assertIsNone(script_mismatch("yi", yid))
        self.assertIsNone(script_mismatch("en", eng))
        self.assertIsNone(script_mismatch(None, eng))


class TestLanguageModels(unittest.TestCase):
    def test_resolve(self):
        from app.languages import resolve_model, LANGUAGE_MODELS
        self.assertEqual(resolve_model("auto", "yi"), (LANGUAGE_MODELS["yi"], "yi"))
        self.assertEqual(resolve_model(None, "he"), (LANGUAGE_MODELS["he"], "he"))
        self.assertEqual(resolve_model("auto", "en"), ("large-v3", "en"))
        self.assertEqual(resolve_model("auto", None), ("large-v3", None))
        self.assertEqual(resolve_model("large-v3", "yi"), ("large-v3", "yi"))   # explicit choice wins
        self.assertEqual(resolve_model(LANGUAGE_MODELS["yi"], None), (LANGUAGE_MODELS["yi"], "yi"))  # forced
        with self.assertRaises(ValueError):
            resolve_model(LANGUAGE_MODELS["he"], "en")


if __name__ == "__main__":
    unittest.main()
