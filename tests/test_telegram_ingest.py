"""Offline tests for telegram_ingest.py, the manual lake-reading backup.

No network: getUpdates/sendMessage/append/offset are all injected fakes,
and file writes go to a temp path. Covers parsing, plausibility rejection,
the message-time stamping, the schema handed to verify_and_learn, the
chat-id authorization guard, and offset advancement (no reprocessing)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import telegram_ingest as ti


def _msg(text, chat_id=42, update_id=1, ts=1_753_000_200):
    # ts default = 2025-07-20T09:50:00Z-ish; exact value asserted where it matters
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "date": ts, "text": text}}


class ParseTests(unittest.TestCase):
    def test_bare_number_is_mean_wind(self):
        self.assertEqual(ti.parse_lake_command("/lake 5"), {"avg_wind_kmh": 5.0})

    def test_key_values_english_and_german_order_free(self):
        got = ti.parse_lake_command("/lake gust=16 mittelwind=5 luftdruck=1016.2 windrichtung=90")
        self.assertEqual(got, {"gust_kmh": 16.0, "avg_wind_kmh": 5.0,
                               "pressure_hpa": 1016.2, "wind_dir_deg": 90.0})

    def test_botname_suffix_and_commas(self):
        self.assertEqual(ti.parse_lake_command("/lake@WingBot mean=5,gust=16"),
                         {"avg_wind_kmh": 5.0, "gust_kmh": 16.0})

    def test_missing_mean_raises(self):
        with self.assertRaises(ValueError):
            ti.parse_lake_command("/lake gust=16")

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            ti.parse_lake_command("/lake foo=1")

    def test_two_bare_numbers_raises(self):
        with self.assertRaises(ValueError):
            ti.parse_lake_command("/lake 5 6")


class ValidateTests(unittest.TestCase):
    def test_clean_reading_has_no_problems(self):
        self.assertEqual(ti.validate({"avg_wind_kmh": 5.0, "gust_kmh": 16.0}), [])

    def test_gust_below_mean_flagged(self):
        self.assertTrue(ti.validate({"avg_wind_kmh": 20.0, "gust_kmh": 10.0}))

    def test_out_of_range_flagged(self):
        self.assertTrue(ti.validate({"avg_wind_kmh": 999.0}))
        self.assertTrue(ti.validate({"humidity_pct": 250.0}))


class BuildObservationTests(unittest.TestCase):
    def setUp(self):
        self.dt = datetime(2026, 7, 19, 14, 30, tzinfo=timezone.utc)

    def test_matches_scraper_schema_and_stamps_message_time(self):
        obs = ti.build_observation(
            {"avg_wind_kmh": 5.0, "gust_kmh": 16.0, "wind_dir_deg": 90.0,
             "temp_c": 18.1, "humidity_pct": 43.0, "pressure_hpa": 1016.2}, self.dt)
        # keys verify_and_learn / closest_observation depend on
        for k in ("observed_at", "avg_wind_kmh", "gust_kmh", "wind_dir_deg"):
            self.assertIn(k, obs)
        self.assertEqual(obs["observed_at"], self.dt.isoformat())   # message time, not now
        self.assertEqual(obs["avg_wind_kmh"], 5.0)
        self.assertEqual(obs["gust_kmh"], 16.0)
        self.assertEqual(obs["wind_dir_compass"], "O")              # 90 deg -> Ost
        self.assertAlmostEqual(obs["gust_kn"], 16.0 / 1.852, places=1)
        self.assertEqual(obs["source"], "telegram_manual")

    def test_gust_defaults_to_mean_when_omitted(self):
        obs = ti.build_observation({"avg_wind_kmh": 7.0}, self.dt)
        self.assertEqual(obs["gust_kmh"], 7.0)
        self.assertTrue(obs["_gust_defaulted"])


class HandleCommandTests(unittest.TestCase):
    def setUp(self):
        self.dt = datetime(2026, 7, 19, 14, 30, tzinfo=timezone.utc)

    def test_good_lake_returns_observation(self):
        reply, obs = ti.handle_command("/lake mean=5 gust=16", self.dt)
        self.assertIsNotNone(obs)
        self.assertIn("Logged", reply)

    def test_implausible_lake_is_rejected_not_logged(self):
        reply, obs = ti.handle_command("/lake mean=999", self.dt)
        self.assertIsNone(obs)
        self.assertIn("Rejected", reply)

    def test_help_and_unknown_and_chatter(self):
        self.assertIsNone(ti.handle_command("/help", self.dt)[1])
        self.assertIn("Wingcheck", ti.handle_command("/help", self.dt)[0])
        self.assertIn("Unknown", ti.handle_command("/frobnicate", self.dt)[0])
        self.assertEqual(ti.handle_command("hi there", self.dt), (None, None))


class PollTests(unittest.TestCase):
    def _run(self, updates, chat_id=42, offset0=0):
        appended, sent = [], []
        saved = {}
        s = ti.poll(
            "TOKEN", chat_id,
            get_updates=lambda tok, off: updates,
            send_message=lambda tok, cid, text: sent.append((cid, text)),
            append=lambda obs: appended.append(obs),
            load_offset=lambda: offset0,
            save_offset=lambda o: saved.setdefault("offset", o),
        )
        return s, appended, sent, saved

    def test_authorized_lake_is_logged_and_offset_advances(self):
        updates = [_msg("/lake mean=5 gust=16", chat_id=42, update_id=7)]
        s, appended, sent, saved = self._run(updates, chat_id=42)
        self.assertEqual(s["logged"], 1)
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["source"], "telegram_manual")
        self.assertNotIn("_gust_defaulted", appended[0])   # stripped before logging
        self.assertEqual(saved["offset"], 8)               # highest update_id + 1
        self.assertTrue(sent)

    def test_foreign_chat_id_is_never_logged(self):
        updates = [_msg("/lake mean=5 gust=16", chat_id=9999, update_id=3)]
        s, appended, sent, saved = self._run(updates, chat_id=42)
        self.assertEqual(appended, [])                     # security: no injection
        self.assertEqual(s["ignored"], 1)
        self.assertEqual(saved["offset"], 4)               # still advances past it
        self.assertEqual(sent, [])                         # no reply to a stranger

    def test_rejected_reading_replies_but_does_not_log(self):
        updates = [_msg("/lake mean=999", chat_id=42, update_id=5)]
        s, appended, sent, saved = self._run(updates, chat_id=42)
        self.assertEqual(appended, [])
        self.assertEqual(s["rejected"], 1)
        self.assertTrue(sent)

    def test_no_updates_leaves_offset_untouched(self):
        s, appended, sent, saved = self._run([], chat_id=42, offset0=11)
        self.assertEqual(saved, {})                        # nothing to save


class OffsetPersistenceTests(unittest.TestCase):
    def test_roundtrip_and_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "telegram_offset.json")
            self.assertEqual(ti._load_offset(p), 0)        # missing -> 0
            ti._save_offset(123, p)
            self.assertEqual(ti._load_offset(p), 123)
            with open(p, "w") as f:
                f.write("not json")
            self.assertEqual(ti._load_offset(p), 0)        # corrupt -> 0


class NoProductionMutationTests(unittest.TestCase):
    def test_import_does_not_need_network_or_playwright(self):
        # importing the module (done at top) must not require requests/playwright
        self.assertTrue(hasattr(ti, "poll"))
        self.assertTrue(callable(ti.handle_command))


if __name__ == "__main__":
    unittest.main()
