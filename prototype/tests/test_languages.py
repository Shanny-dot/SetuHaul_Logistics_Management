"""Gujarati, Marathi, Punjabi and Marwari: detection, reading, replies, guard, and full turns.

Run:  python3 -I -m unittest tests.test_languages -v      (from the prototype folder)
"""
import os
import string
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "scripts"))
from setuhaul import nlu, replies as R, languages as L
from setuhaul.clock import ts

AS_OF = ts("2026-08-04T09:30:00+05:30")
NEW = ["gu", "mr", "pa", "mwr"]


def x(text):
    return nlu.extract(text, AS_OF)


class Detect(unittest.TestCase):
    def test_scripts_and_markers(self):
        cases = {
            "હું એક કલાક મોડો છું": "gu",
            "ਮੈਂ ਇੱਕ ਘੰਟਾ ਲੇਟ ਹਾਂ": "pa",
            "मला 45 मिनिटे उशीर होईल": "mr",
            "भाई म्हैं थोड़ो मोड़ो हूं": "mwr",
            "मैं एक घंटा लेट हूँ": "hi",
            "thoda late hoon bhai": "hi-Latn",
            "I am late by one hour": "en",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(nlu.detect_lang(text, "en"), want)

    def test_short_replies_keep_the_thread_language(self):
        self.assertEqual(nlu.detect_lang("1", "gu"), "gu")
        self.assertEqual(nlu.detect_lang("हाँ", "mr"), "mr")        # Devanagari, no markers: stays Marathi
        self.assertEqual(nlu.detect_lang("हाँ", "en"), "hi")


class Read(unittest.TestCase):
    def test_delay_and_durations(self):
        for text, mins in [("હું એક કલાક મોડો છું", 60), ("दीड तास उशीर होईल", 90), ("ਅੱਧਾ ਘੰਟਾ ਲੇਟ ਹਾਂ", 30),
                           ("डेढ़ घंटो मोड़ो हूं", 90), ("2 કલાક મોડું થશે", 120), ("45 मिनिटे उशीर", 45), ("20 ਮਿੰਟ ਦੇਰ", 20)]:
            with self.subTest(text=text):
                f = x(text)
                self.assertEqual(f.intent, "REPORT_DELAY")
                self.assertEqual(f.delay_min, mins)

    def test_clock_times_and_native_digits(self):
        for text, hhmm in [("૧૧:૩૦ વાગ્યે પહોંચીશ", (11, 30)), ("मी 11:20 वाजता पोहोचेन", (11, 20)),
                           ("ਮੈਂ ੧੧:੧੦ ਵਜੇ ਤੱਕ ਪਹੁੰਚ ਜਾਵਾਂਗਾ", (11, 10)), ("11:30 बज्यां तांई पूगसूं", (11, 30))]:
            with self.subTest(text=text):
                f = x(text)
                self.assertEqual((f.eta.hour, f.eta.minute), hhmm)
                self.assertEqual(f.intent, "REPORT_DELAY")

    def test_after_words_set_earliest_start(self):
        for text in ["12 नंतर कोणता स्लॉट आहे?", "12 વાગ્યા પછી કોઈ સ્લોટ?", "12 ਵਜੇ ਤੋਂ ਬਾਅਦ ਕੋਈ ਸਲਾਟ?", "12 बज्यां पछै कोई स्लॉट?"]:
            with self.subTest(text=text):
                self.assertEqual(x(text).earliest_start.hour, 12)

    def test_a_slot_reference_is_not_an_arrival_time(self):
        f = x("माझा 4 वाजताचा स्लॉट अजून चालू आहे का?")
        self.assertIsNone(f.eta)
        self.assertEqual(f.intent, "CHECK_STATUS")

    def test_choices(self):
        for text, n in [("પહેલો", 1), ("दुसरा", 2), ("ਤੀਜਾ", 3), ("दूजो", 2), ("૨", 2), ("੩", 3)]:
            with self.subTest(text=text):
                self.assertEqual(x(text).choice, n)
                self.assertEqual(x(text).intent, "CHOOSE")

    def test_safety_words(self):
        for text in ["અકસ્માત થયો છે", "हेल्परला जखमी झाला", "ਹਾਦਸਾ ਹੋ ਗਿਆ", "ਹੈਲਪਰ ਨੂੰ ਸੱਟ ਲੱਗੀ", "हेल्पर घायल है", "આગ લાગી છે"]:
            with self.subTest(text=text):
                self.assertTrue(x(text).safety)
                self.assertEqual(x(text).intent, "SAFETY")

    def test_no_false_safety_alarm(self):
        # 'આગળ' (ahead) and 'ਅੱਗੇ' (ahead) contain the letters of 'fire'; '11 વાગ્યે' contains 'hurt' letters.
        for text in ["આગળ ટ્રાફિક છે, 11 વાગ્યે પહોંચીશ", "ਅੱਗੇ ਜਾਮ ਹੈ, 11 ਵਜੇ ਪਹੁੰਚਾਂਗਾ"]:
            with self.subTest(text=text):
                self.assertFalse(x(text).safety)


class Replies(unittest.TestCase):
    def fields(self, s):
        return {f for _, f, _, _ in string.Formatter().parse(s) if f}

    def test_same_placeholders_as_english(self):
        for key, table in R.T.items():
            for lang in NEW:
                if lang in table:
                    with self.subTest(key=key, lang=lang):
                        self.assertEqual(self.fields(table[lang]), self.fields(table["en"]))

    def test_core_flow_is_translated(self):
        core = ["ask_eta", "options", "line", "booked", "option_gone", "no_slot", "safety", "approval_requested", "unknown"]
        for lang in NEW:
            for key in core:
                self.assertIn(lang, R.T[key], f"{key} missing in {lang}")

    def test_untranslated_keys_fall_back_to_hindi(self):
        self.assertEqual(R.t("early_policy", "gu", old="9:00 AM"), R.t("early_policy", "hi", old="9:00 AM"))

    def test_guard_reads_every_dock_word(self):
        ev = {"docks": {"D2"}, "times": {"1:00 PM"}, "confirmed": False}
        self.assertEqual(R.guard("ડૉક D9 - 1:00 PMથી", ev), ["dock D9"])
        self.assertEqual(R.guard("ਡੌਕ D9 1:00 PM", ev), ["dock D9"])
        self.assertEqual(R.guard("डॉक D9 1:00 PM", ev), ["dock D9"])

    def test_guard_blocks_booked_words_without_a_commit(self):
        ev = {"docks": {"D2"}, "times": {"1:00 PM"}, "confirmed": False}
        for lang in NEW:
            text = R.t("booked", lang, dock="D2", facility="Jaipur DC", start="1:00 PM", gate_by="1:00 PM", ref="X")
            with self.subTest(lang=lang):
                self.assertIn("claims booked without CONFIRMED commit", R.guard(text, ev))
                self.assertEqual(R.guard(text, dict(ev, confirmed=True)), [])


class FullTurns(unittest.TestCase):
    """Every demo conversation, run through the real engine: same outcome as in English, reply in the driver's script."""

    @classmethod
    def setUpClass(cls):
        import language_demo as LD
        cls.LD = LD
        cls.convs = []
        for cid, lang, title, case, drv, turns in LD.CONVERSATIONS:
            _, native = LD.run(drv, turns, True)
            _, english = LD.run(drv, turns, False)
            cls.convs.append((cid, lang, native, english))

    def test_detected_language_and_same_outcome(self):
        for cid, lang, native, english in self.convs:
            for n, e in zip(native, english):
                with self.subTest(conv=cid, text=n["text"]):
                    if any(c.isalpha() for c in n["text"]):
                        self.assertEqual(n["lang"], lang)
                    self.assertEqual(n["state"], e["state"])

    def test_replies_are_in_the_drivers_script_and_pass_the_guard(self):
        block = {"gu": "gujr", "pa": "guru", "mr": "deva", "mwr": "deva", "hi": "deva"}
        for cid, lang, native, _ in self.convs:
            for n in native:
                if n["reply"]:
                    with self.subTest(conv=cid):
                        share, _ = L.script_share(n["reply"])
                        if lang in block:
                            self.assertGreater(share.get(block[lang], 0), 0.3)   # dock codes, times and names stay Latin
                        else:                                                    # English and Hinglish: Latin only
                            self.assertEqual(sum(share.get(b, 0) for b in ("deva", "gujr", "guru")), 0)
                        self.assertFalse(any("output_guard:BLOCKED" in t for t in n["tools"] or []))


if __name__ == "__main__":
    unittest.main()
