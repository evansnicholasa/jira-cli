"""Tests for the Jira CLI.

**`unittest` rather than pytest**, deliberately. The point of this tool is that
it runs with nothing installed — no venv, no requirements file, whatever Python
is on PATH. Tests that need `pip install pytest` first would undo that for the
one part of the tool most likely to be run by someone checking whether it still
works after six months of not being touched.

    python -m unittest discover

Nothing here touches the network. The live API was exercised by hand against a
throwaway issue (created, read, edited, commented, transitioned, searched,
deleted) before any of this was written; these cover the logic that sits
*around* the call, which is where the mistakes actually are.
"""

import contextlib
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import jira  # noqa: E402


CONFIG = {
    "JIRA_SITE": "example.atlassian.net",
    "JIRA_EMAIL": "someone@example.com",
    "JIRA_API_TOKEN": "token",
}


@contextlib.contextmanager
def captured():
    """Run a command with stdout captured.

    Two reasons. A test suite that prints is one whose real failures get
    skimmed past — and the printed line *is* the command's entire result, so
    capturing it turns noise into the thing worth asserting.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def http_error(code, payload):
    return urllib.error.HTTPError(
        "https://example.atlassian.net", code, "err", {},
        io.BytesIO(json.dumps(payload).encode()),
    )


class TestLoadConfig(unittest.TestCase):
    def test_reads_the_file(self):
        with mock.patch.object(jira, "CONFIG_FILE") as path:
            path.exists.return_value = True
            path.read_text.return_value = (
                "# a comment\n"
                "JIRA_SITE=example.atlassian.net\n"
                "\n"
                "JIRA_EMAIL=someone@example.com\n"
                "JIRA_API_TOKEN=secret\n"
            )
            with mock.patch.dict("os.environ", {}, clear=True):
                config = jira.load_config()

        self.assertEqual(config["JIRA_SITE"], "example.atlassian.net")
        self.assertEqual(config["JIRA_API_TOKEN"], "secret")

    def test_the_environment_wins_over_the_file(self):
        # So a one-off run against another site needs no edit to a file that
        # holds a credential.
        with mock.patch.object(jira, "CONFIG_FILE") as path:
            path.exists.return_value = True
            path.read_text.return_value = (
                "JIRA_SITE=from-file\nJIRA_EMAIL=e\nJIRA_API_TOKEN=t\n"
            )
            with mock.patch.dict("os.environ", {"JIRA_SITE": "from-env"}, clear=True):
                self.assertEqual(jira.load_config()["JIRA_SITE"], "from-env")

    def test_jira_config_redirects_the_loader(self):
        # A CI job has no home directory worth writing to, so the path has to be
        # overridable without editing the script.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as handle:
            handle.write(
                """JIRA_SITE=elsewhere
JIRA_EMAIL=e
JIRA_API_TOKEN=t
"""
            )
            name = handle.name
        try:
            with mock.patch.dict("os.environ", {"JIRA_CONFIG": name}, clear=True):
                self.assertEqual(jira.load_config()["JIRA_SITE"], "elsewhere")
        finally:
            Path(name).unlink()

    def test_says_which_keys_are_missing_and_where_to_put_them(self):
        # The failure mode here is a first-time setup, so the error has to be
        # the instructions rather than a stack trace.
        with mock.patch.object(jira, "CONFIG_FILE") as path:
            path.exists.return_value = False
            with mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(SystemExit) as caught:
                    jira.load_config()

        message = str(caught.exception)
        self.assertIn("JIRA_API_TOKEN", message)
        self.assertIn("api-tokens", message)


class TestErrorRendering(unittest.TestCase):
    """Jira reports failures in two places at once; both have to surface."""

    def test_flattens_both_error_shapes_into_a_sentence(self):
        error = http_error(400, {
            "errorMessages": ["Field 'nope' cannot be set"],
            "errors": {"summary": "Summary is required"},
        })
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(jira.JiraError) as caught:
                jira.request(CONFIG, "POST", "/x", body={})

        message = str(caught.exception)
        self.assertIn("Field 'nope' cannot be set", message)
        self.assertIn("summary: Summary is required", message)
        self.assertNotIn("{", message)  # not a JSON dump

    def test_survives_a_body_that_is_not_json(self):
        error = urllib.error.HTTPError(
            "https://x", 502, "bad gateway", {}, io.BytesIO(b"<html>nope</html>")
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(jira.JiraError) as caught:
                jira.request(CONFIG, "GET", "/x")
        self.assertIn("502", str(caught.exception))

    def test_an_empty_body_is_not_a_decode_error(self):
        # edit and transition answer 204 with no content. Parsing that as JSON
        # would turn every successful write into a crash.
        response = mock.MagicMock()
        response.read.return_value = b""
        response.__enter__.return_value = response
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(jira.request(CONFIG, "PUT", "/x", body={}), {})


class TestReadText(unittest.TestCase):
    def test_reads_a_file_as_utf8(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write("café — §4.2 ✓")
            name = handle.name
        try:
            self.assertEqual(jira.read_text(name), "café — §4.2 ✓")
        finally:
            Path(name).unlink()

    def test_a_dash_means_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO("from a pipe")):
            self.assertEqual(jira.read_text("-"), "from a pipe")


class TestTransition(unittest.TestCase):
    """Resolving a name to an id is the whole reason this command exists."""

    TRANSITIONS = {"transitions": [
        {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
        {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
        {"id": "41", "name": "Done", "to": {"name": "Done"}},
    ]}

    def run_transition(self, status):
        calls = []

        def fake(config, method, path, body=None, params=None):
            calls.append((method, path, body))
            return self.TRANSITIONS if method == "GET" else {}

        with mock.patch.object(jira, "request", side_effect=fake), captured():
            jira.cmd_transition(CONFIG, SimpleNamespace(key="PROJ-1", status=status))
        return calls

    def test_posts_the_id_that_matches_the_name(self):
        calls = self.run_transition("Done")
        self.assertEqual(calls[-1][2], {"transition": {"id": "41"}})

    def test_matching_is_case_insensitive(self):
        # "done" is what a person types; the workflow spells it "Done".
        self.assertEqual(self.run_transition("done")[-1][2], {"transition": {"id": "41"}})

    def test_an_unreachable_status_lists_the_ones_that_are(self):
        # Jira only offers transitions valid from the *current* status, so
        # "no" is a useless answer on its own — what is reachable is the thing
        # the caller needs next.
        def fake(config, method, path, body=None, params=None):
            return self.TRANSITIONS

        with mock.patch.object(jira, "request", side_effect=fake):
            with self.assertRaises(SystemExit) as caught:
                jira.cmd_transition(CONFIG, SimpleNamespace(key="PROJ-1", status="Nope"))

        message = str(caught.exception)
        self.assertIn("Done", message)
        self.assertIn("In Progress", message)


class TestCommands(unittest.TestCase):
    def test_search_uses_the_enhanced_endpoint(self):
        # The original /search is HTTP 410 Gone on Jira Cloud — measured, not
        # assumed. If this ever points back at /search, every query 410s.
        seen = {}

        def fake(config, method, path, body=None, params=None):
            seen["path"] = path
            return {"issues": [], "isLast": True}

        with mock.patch.object(jira, "request", side_effect=fake), captured() as out:
            jira.cmd_search(CONFIG, SimpleNamespace(jql="project = PROJ", limit=50, json=False))

        self.assertEqual(seen["path"], "/rest/api/2/search/jql")
        # It counts what came back. The enhanced endpoint returns no `total`,
        # so claiming one would be inventing a number.
        self.assertIn("(0 shown)", out.getvalue())

    def test_create_sends_the_fields_jira_expects(self):
        seen = {}

        def fake(config, method, path, body=None, params=None):
            seen.update(body or {})
            return {"key": "PROJ-99"}

        args = SimpleNamespace(
            project="PROJ", type="Story", summary="A summary",
            description_file=None, parent=None,
        )
        with mock.patch.object(jira, "request", side_effect=fake), captured() as out:
            jira.cmd_create(CONFIG, args)

        self.assertIn("PROJ-99", out.getvalue())
        self.assertEqual(seen["fields"]["project"], {"key": "PROJ"})
        self.assertEqual(seen["fields"]["issuetype"], {"name": "Story"})
        self.assertEqual(seen["fields"]["summary"], "A summary")
        self.assertNotIn("description", seen["fields"])

    def test_the_project_key_falls_back_to_the_config(self):
        # So a board used every day needs --project on none of its creates.
        seen = {}

        def fake(config, method, path, body=None, params=None):
            seen.update(body or {})
            return {"key": "PROJ-99"}

        config = dict(CONFIG, JIRA_PROJECT="PROJ")
        args = SimpleNamespace(
            project=None, type="Story", summary="A summary",
            description_file=None, parent=None,
        )
        with mock.patch.object(jira, "request", side_effect=fake), captured():
            jira.cmd_create(config, args)

        self.assertEqual(seen["fields"]["project"], {"key": "PROJ"})

    def test_an_explicit_project_beats_the_config(self):
        seen = {}

        def fake(config, method, path, body=None, params=None):
            seen.update(body or {})
            return {"key": "OTHER-1"}

        config = dict(CONFIG, JIRA_PROJECT="PROJ")
        args = SimpleNamespace(
            project="OTHER", type="Story", summary="s",
            description_file=None, parent=None,
        )
        with mock.patch.object(jira, "request", side_effect=fake), captured():
            jira.cmd_create(config, args)

        self.assertEqual(seen["fields"]["project"], {"key": "OTHER"})

    def test_no_project_anywhere_is_a_sentence_not_a_traceback(self):
        # The likeliest first-run mistake, so it has to read as instructions.
        called = False

        def fake(*a, **k):
            nonlocal called
            called = True
            return {}

        args = SimpleNamespace(
            project=None, type="Story", summary="s",
            description_file=None, parent=None,
        )
        with mock.patch.object(jira, "request", side_effect=fake):
            with self.assertRaises(SystemExit) as caught:
                jira.cmd_create(CONFIG, args)

        self.assertIn("JIRA_PROJECT", str(caught.exception))
        self.assertFalse(called)

    def test_create_nests_under_parent_when_passed(self):
        seen = {}

        def fake(config, method, path, body=None, params=None):
            seen.update(body or {})
            return {"key": "PROJ-100"}

        args = SimpleNamespace(
            project="PROJ", type="Story", summary="Child",
            description_file=None, parent="PROJ-1",
        )
        with mock.patch.object(jira, "request", side_effect=fake), captured():
            jira.cmd_create(CONFIG, args)

        self.assertEqual(seen["fields"]["parent"], {"key": "PROJ-1"})

    def test_create_omits_parent_when_unset(self):
        seen = {}

        def fake(config, method, path, body=None, params=None):
            seen.update(body or {})
            return {"key": "PROJ-101"}

        args = SimpleNamespace(
            project="PROJ", type="Story", summary="Orphan",
            description_file=None, parent=None,
        )
        with mock.patch.object(jira, "request", side_effect=fake), captured():
            jira.cmd_create(CONFIG, args)

        self.assertNotIn("parent", seen["fields"])

    def test_edit_with_nothing_to_change_refuses_rather_than_calling(self):
        # A PUT with an empty fields object is accepted by Jira and does
        # nothing, so the mistake would otherwise report success.
        called = False

        def fake(*a, **k):
            nonlocal called
            called = True
            return {}

        args = SimpleNamespace(key="PROJ-1", summary=None, description_file=None)
        with mock.patch.object(jira, "request", side_effect=fake):
            with self.assertRaises(SystemExit):
                jira.cmd_edit(CONFIG, args)

        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
