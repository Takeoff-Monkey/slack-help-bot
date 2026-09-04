"""Tests for the bot-side routing this tool introduced: tool.json `triggers` →
tool_registry.keyword_hits / action_triggered / filename_hits / routing_note.

These live here, next to the tool that first used the feature, and only need the standard
library plus the repo root on sys.path (tool_registry imports nothing heavy), so they run in
this tool's venv with the rest of the suite:

    cd tools/arazoza-formatter && .venv/bin/python -m unittest discover -s tests

The rules they pin down come straight from the user's instruction — "there should always be a
worksheet uploaded alongside the prompt; if not, ask the user to send it before attempting to
start work" — plus the requirement that talking *about* Arazoza (the workspace also has an
Arazoza DynamoDB worker Lambda) must still be answered as a question.
"""

from __future__ import annotations

import logging
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[1]
REPO = TOOL_DIR.parents[1]
sys.path.insert(0, str(REPO))

import tool_registry            # noqa: E402


@dataclass
class FakeFile:
    """Stands in for slack_files.StagedFile (routing only ever reads .handle/.filename)."""
    handle: str
    filename: str


def specs():
    return tool_registry.discover_tools(REPO / "tools")


class ManifestTests(unittest.TestCase):
    def test_the_tool_is_discovered_with_its_triggers(self):
        spec = specs()["arazoza-formatter"]
        self.assertEqual(spec.triggers["filename_contains"], ["arazoza"])
        self.assertTrue(spec.triggers["keywords"])
        self.assertTrue(spec.triggers["action_words"])
        self.assertEqual(spec.accepts, {"file_types": ["xlsx", "xlsm"], "max_files": 1})
        self.assertEqual(spec.input_schema["required"], ["input_file"])

    def test_a_malformed_triggers_block_never_stops_the_tool_loading(self):
        logging.disable(logging.ERROR)          # these are deliberately broken; don't shout about it
        self.addCleanup(logging.disable, logging.NOTSET)
        for raw in (None, "nonsense", 42, {"keywords": "not-a-list"}, {"keywords": [1, None, ""]},
                    {"keywords": ["("]}, {"filename_contains": {"a": 1}}):
            with self.subTest(raw=raw):
                self.assertIsInstance(tool_registry._parse_triggers("t", raw), dict)

    def test_action_words_that_all_fail_to_compile_fail_CLOSED(self):
        logging.disable(logging.ERROR)
        self.addCleanup(logging.disable, logging.NOTSET)
        # An unusable action_words list must not read as "this tool declares none", which would
        # turn every bare keyword mention back into a forced action.
        parsed = tool_registry._parse_triggers("t", {"keywords": [r"\barazoza\b"], "action_words": ["(unclosed"]})
        self.assertIn("action_words", parsed)
        self.assertEqual(parsed["action_words"], [])


class KeywordTests(unittest.TestCase):
    def setUp(self):
        self.specs = specs()

    def hit(self, text):
        return [(s.name, m, act) for s, m, act in tool_registry.keyword_hits(self.specs, text)]

    def test_questions_about_arazoza_are_not_actions(self):
        for q in ["what does the arazoza db worker do?",
                  "how does the Arazoza export work?",
                  "is the arazoza lambda still live?",
                  "who owns the Arazoza integration?",
                  "what is the arazoza project about?",
                  "can you check the arazoza file sync lambda"]:
            with self.subTest(q=q):
                self.assertTrue(self.hit(q), "the keyword should still be noticed")
                self.assertFalse(tool_registry.action_triggered(self.specs, q), q)

    def test_requests_to_format_are_actions(self):
        for q in ["this is an Arazoza project, please format it",
                  "format this for Arazoza",
                  "here's the Arazoza worksheet",
                  "Arazoza's worksheet needs cleaning up",
                  "can you prep the arazoza sheet",
                  "run the arazoza spreadsheet through the formatter",
                  "ARAZOZA — reformat please",
                  "fix up this arazoza spreadsheet"]:
            with self.subTest(q=q):
                self.assertTrue(tool_registry.action_triggered(self.specs, q), q)

    def test_unrelated_text_never_fires(self):
        for q in ["extract the schedules from this pdf", "", "   ", "format this worksheet"]:
            with self.subTest(q=q):
                self.assertEqual(self.hit(q), [])
                self.assertFalse(tool_registry.action_triggered(self.specs, q))

    def test_keyword_is_word_bounded(self):
        self.assertEqual(self.hit("arazozalike"), [])
        self.assertTrue(self.hit("Arazoza."))
        self.assertTrue(self.hit("(arazoza)"))


class RoutingNoteTests(unittest.TestCase):
    def setUp(self):
        self.specs = specs()

    def note(self, question, files=()):
        return tool_registry.routing_note(self.specs, question, list(files))

    def test_worksheet_attached_points_at_the_tool(self):
        n = self.note("format this Arazoza worksheet", [FakeFile("file_1", "Arazoza - Job.xlsm")])
        self.assertIn("`file_1`", n)
        self.assertIn("Call `arazoza-formatter` on it", n)
        self.assertNotIn("ask_user", n)

    def test_no_worksheet_at_all_asks_for_one(self):
        n = self.note("this is an Arazoza project, please format it")
        self.assertIn("ask_user", n)
        self.assertIn("do not start anything", n)
        self.assertIn(".xlsx, .xlsm", n)

    def test_only_a_schedule_image_asks_for_the_worksheet(self):
        n = self.note("this is an Arazoza project, format it", [FakeFile("file_1", "plant schedule.png")])
        self.assertIn("ask_user", n)
        self.assertIn("are not .xlsx, .xlsm", n)
        self.assertNotIn("Call `arazoza-formatter` on it", n)

    def test_filename_match_of_the_wrong_type_asks_rather_than_calling(self):
        for name in ("Arazoza - Old.xls", "Arazoza plans.pdf", "Arazoza schedule.png"):
            with self.subTest(name=name):
                n = self.note("can you format this?", [FakeFile("file_1", name)])
                self.assertIn("ask_user", n)
                self.assertNotIn("Call `arazoza-formatter` on it", n)

    def test_image_plus_worksheet_picks_the_worksheet(self):
        n = self.note("format the Arazoza job", [FakeFile("file_1", "schedule.png"),
                                                 FakeFile("file_2", "Arazoza - X - Worksheet.xlsm")])
        self.assertIn("`file_2`", n)
        self.assertNotIn("`file_1`", n)
        self.assertIn("Call `arazoza-formatter` on it", n)

    def test_two_worksheets_make_the_model_ask_which(self):
        n = self.note("format the Arazoza job", [FakeFile("file_1", "Arazoza - A.xlsm"),
                                                 FakeFile("file_2", "Arazoza - B.xlsm")])
        self.assertIn("more than one", n)
        self.assertIn("ask_user", n)

    def test_bare_mention_with_a_worksheet_is_a_hint_not_an_order(self):
        n = self.note("how many trees are in this Arazoza list?", [FakeFile("file_1", "Arazoza - X.xlsx")])
        self.assertIn("ignore this note", n)

    def test_uppercase_extension_is_accepted(self):
        n = self.note("format this Arazoza worksheet", [FakeFile("file_1", "ARAZOZA - JOB.XLSM")])
        self.assertIn("Call `arazoza-formatter` on it", n)

    def test_nothing_fires_for_an_unrelated_turn(self):
        self.assertEqual(self.note("extract the schedules", [FakeFile("file_1", "plans.pdf")]), "")

    def test_odd_inputs_do_not_raise(self):
        for question, files in [("", []), (None, None), ("arazoza", [FakeFile("file_1", "")]),
                                ("arazoza", [FakeFile("file_1", "noextension")]),
                                ("format the arazoza sheet", [FakeFile("file_1", None)])]:
            with self.subTest(question=question):
                self.assertIsInstance(tool_registry.routing_note(self.specs, question, files), str)

    def test_note_never_points_at_a_file_the_runner_would_reject(self):
        """The runner's own accepted-type check is the contract this must not contradict."""
        spec = self.specs["arazoza-formatter"]
        for name, ok in [("Arazoza - X.xlsm", True), ("Arazoza - X.xlsx", True),
                         ("Arazoza - X.xls", False), ("Arazoza - X.png", False), ("noext", False)]:
            with self.subTest(name=name):
                self.assertEqual(tool_registry._accepts_file(spec, FakeFile("file_1", name)), ok)
                n = self.note("format this Arazoza worksheet", [FakeFile("file_1", name)])
                self.assertEqual("Call `arazoza-formatter` on it" in n, ok)


if __name__ == "__main__":
    unittest.main()
