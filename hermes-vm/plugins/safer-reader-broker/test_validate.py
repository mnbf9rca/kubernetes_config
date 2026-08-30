#!/usr/bin/env python3
"""Unit tests for the safer-reader-broker envelope validator.

    python3 hermes-vm/plugins/safer-reader-broker/test_validate.py

Stdlib `unittest` run directly, matching the repo's two other suites, and
NOT pytest -- which cannot collect this file at all, whatever import mode it is
given. The broker spec's layout puts `test_validate.py` inside a Python
package, and that package's `__init__.py` must import `model_tools` and
`tools.kanban_tools` at module top level so an upstream rename fails at worker
start. pytest imports the containing package before the test module, so
collection dies on `ModuleNotFoundError: model_tools` on any machine that is
not the VM -- which is every machine this test exists to run on. Direct
execution never touches the package.

`validate.py` is loaded BY FILE PATH for the same reason, rather than imported
as `safer-reader-broker.validate`: the whole point of `validate.py` holding no
Hermes imports is that it can be tested off the VM, and the path load is what
cashes that in.

What these lock down is the four rules of the broker spec's "Validation, in
trusted code" section, plus the containment guard's four branches. Both fail
INVISIBLY if they regress: a validator that accepts everything, and a guard
that always passes, look exactly like a validator and a guard that are never
given anything bad. The rules are the only thing standing between a
model-controlled string and the board's SQLite file, which a human reads
through the dashboard.

The live checks the design keeps for the VM -- error-to-model, no partial
write, in-run recovery -- are verification item 4 and are not repeated here.
"""

import importlib.util
import json
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "safer_reader_broker_validate", os.path.join(_HERE, "validate.py")
)
validate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate)


def _load_broker_against_stubs():
    """Load `__init__.py` with the Hermes modules it imports stubbed out.

    This exists for the containment-guard tests and for nothing else. The guard
    is the plugin's only other piece of real logic, and three of its four
    branches are soft fails that look identical to success from outside -- a
    regression in any of them is a guard that has silently stopped guarding, so
    they need a test, and a test that only runs on the VM is a test nobody runs.

    The stubs are the thinnest thing that lets the module body execute: they
    are NOT a model of Hermes, and nothing calls through the six
    `_unreachable_stub`s -- each raises if it is reached. The handler tests
    below do run the handlers, against per-test replacements they install
    themselves; what they cannot do is assert against a real board, which is
    exactly what the live verification items are for.

    The module is loaded as a package with `submodule_search_locations` set,
    which is how Hermes's own plugin loader does it, so the `from . import
    validate` line resolves the same way it does on the VM.
    """
    saved = {}
    stubs = {}

    model_tools = types.ModuleType("model_tools")
    model_tools._last_resolved_tool_names = []
    stubs["model_tools"] = model_tools

    tools = types.ModuleType("tools")
    tools.__path__ = []
    stubs["tools"] = tools

    kanban_tools = types.ModuleType("tools.kanban_tools")
    for symbol in (
        "_connect",
        "_default_task_id",
        "_enforce_worker_task_ownership",
        "_reject_delegated_child_mutation",
        "_stamp_worker_session_metadata",
        "_worker_run_id",
    ):
        setattr(kanban_tools, symbol, _unreachable_stub(symbol))
    stubs["tools.kanban_tools"] = kanban_tools

    registry = types.ModuleType("tools.registry")
    registry.tool_error = lambda message, **extra: json.dumps(
        dict({"error": message}, **extra)
    )
    stubs["tools.registry"] = registry

    for name, module in stubs.items():
        saved[name] = sys.modules.get(name)
        sys.modules[name] = module
    try:
        spec = importlib.util.spec_from_file_location(
            "safer_reader_broker",
            os.path.join(_HERE, "__init__.py"),
            submodule_search_locations=[_HERE],
        )
        broker = importlib.util.module_from_spec(spec)
        sys.modules["safer_reader_broker"] = broker
        spec.loader.exec_module(broker)
    finally:
        for name, previous in saved.items():
            if previous is None:
                del sys.modules[name]
            else:
                sys.modules[name] = previous
    return broker, model_tools


def _unreachable_stub(symbol):
    def _stub(*args, **kwargs):
        raise AssertionError(
            "stub %s was called; these tests must not reach Hermes" % symbol
        )

    return _stub


broker, stub_model_tools = _load_broker_against_stubs()


def _reject(case, envelope):
    """Assert the envelope is refused, and return the message it was refused with."""
    with case.assertRaises(validate.EnvelopeError) as caught:
        validate.validate_envelope(envelope)
    return str(caught.exception)


class TestRule1Size(unittest.TestCase):
    """Rule 1: a string, at most 65,536 bytes of UTF-8."""

    def test_non_string_is_rejected(self):
        for value in (None, 42, {"status": "OK"}, ["status"], b'{"status":"OK"}'):
            self.assertIn("rule 1", _reject(self, value))

    def test_at_the_limit_is_accepted(self):
        # Pad `answer` until the envelope is exactly MAX_ENVELOPE_BYTES.
        skeleton = '{"status": "OK", "answer": ""}'
        padding = validate.MAX_ENVELOPE_BYTES - len(skeleton.encode("utf-8"))
        envelope = '{"status": "OK", "answer": "%s"}' % ("a" * padding)
        self.assertEqual(len(envelope.encode("utf-8")), validate.MAX_ENVELOPE_BYTES)
        self.assertEqual(validate.validate_envelope(envelope)["status"], "OK")

    def test_one_byte_over_the_limit_is_rejected(self):
        skeleton = '{"status": "OK", "answer": ""}'
        padding = validate.MAX_ENVELOPE_BYTES - len(skeleton.encode("utf-8")) + 1
        envelope = '{"status": "OK", "answer": "%s"}' % ("a" * padding)
        self.assertIn("rule 1", _reject(self, envelope))

    def test_the_limit_is_bytes_not_characters(self):
        # A three-byte character counts three times. A validator measuring
        # len() would accept roughly three times the intended payload.
        skeleton = '{"status": "OK", "answer": ""}'
        room = validate.MAX_ENVELOPE_BYTES - len(skeleton.encode("utf-8"))
        envelope = '{"status": "OK", "answer": "%s"}' % ("中" * (room // 3 + 1))
        self.assertIn("rule 1", _reject(self, envelope))


class TestRule2Json(unittest.TestCase):
    """Rule 2: parses as JSON, and the result is an object."""

    def test_non_json_is_rejected(self):
        for value in ("", "   ", "not json at all", '{"status": "OK"', "{'status': 'OK'}"):
            self.assertIn("rule 2", _reject(self, value))

    def test_json_that_is_not_an_object_is_rejected(self):
        for value in ('"OK"', "[]", '["status", "OK"]', "42", "true", "null"):
            self.assertIn("rule 2", _reject(self, value))


class TestRule3Status(unittest.TestCase):
    """Rule 3: `status` is present and is exactly OK or UNASSESSED."""

    def test_missing_status_is_rejected(self):
        self.assertIn("rule 3", _reject(self, '{"answer": "something"}'))
        self.assertIn("rule 3", _reject(self, "{}"))

    def test_wrong_status_is_rejected(self):
        for status in ('"MAYBE"', '"ok"', '"Ok"', '" OK"', '"OK "', "null", "true", "[]"):
            envelope = '{"status": %s}' % status
            self.assertIn("rule 3", _reject(self, envelope))

    def test_status_must_not_be_a_truthy_lookalike(self):
        # `1 == True` in Python, so a membership test written against a list of
        # truthy values would accept this. It must not.
        self.assertIn("rule 3", _reject(self, '{"status": 1}'))


class TestRule4ControlCharacters(unittest.TestCase):
    """Rule 4: no control characters other than newline and tab."""

    def test_newline_and_tab_are_accepted(self):
        envelope = '{"status": "OK", "answer": "line one\\nline two\\tcolumn"}'
        parsed = validate.validate_envelope(envelope)
        self.assertEqual(parsed["answer"], "line one\nline two\tcolumn")

    def test_raw_newline_between_tokens_is_accepted(self):
        parsed = validate.validate_envelope('{\n\t"status": "OK"\n}')
        self.assertEqual(parsed["status"], "OK")

    def test_escaped_ansi_escape_is_rejected(self):
        # The case rule 4 exists for: json.loads accepts this happily and
        # decodes it into the value, which then reaches the operator's terminal.
        envelope = '{"status": "OK", "answer": "\\u001b[31mred\\u001b[0m"}'
        message = _reject(self, envelope)
        self.assertIn("rule 4", message)
        self.assertIn("U+001B", message)

    def test_escaped_nul_is_rejected(self):
        self.assertIn("rule 4", _reject(self, '{"status": "OK", "answer": "a\\u0000b"}'))

    def test_escaped_carriage_return_is_rejected(self):
        self.assertIn("rule 4", _reject(self, '{"status": "OK", "answer": "a\\rb"}'))

    def test_escaped_c1_control_is_rejected(self):
        self.assertIn("rule 4", _reject(self, '{"status": "OK", "answer": "a\\u0085b"}'))

    def test_control_character_in_a_key_is_rejected(self):
        self.assertIn("rule 4", _reject(self, '{"status": "OK", "an\\u001bswer": "x"}'))

    def test_control_character_nested_in_a_list_is_rejected(self):
        envelope = '{"status": "OK", "sources": ["https://ok", "\\u001b[2J"]}'
        self.assertIn("rule 4", _reject(self, envelope))

    def test_control_character_nested_in_an_object_is_rejected(self):
        envelope = '{"status": "OK", "quotes": [{"text": "a\\u0007b"}]}'
        self.assertIn("rule 4", _reject(self, envelope))

    def test_raw_carriage_return_outside_a_string_is_rejected(self):
        # This is the case the raw-string scan exists for. A carriage return is
        # legal JSON whitespace, so json.loads accepts it and it survives into
        # the raw envelope the broker stores; no scan of the decoded values
        # would ever see it.
        self.assertIn("rule 4", _reject(self, '{"status": "OK"}\r'))

    def test_every_bidi_control_is_rejected_when_escaped(self):
        # Adjudicated into the rule: these are category Cf, so the
        # control-character range does not reach them, but a right-to-left
        # override reorders what the operator reads off the dashboard, which is
        # the asset rule 4 protects. All nine, so a truncated set is caught.
        for code in list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)):
            envelope = '{"status": "OK", "answer": "a\\u%04xb"}' % code
            message = _reject(self, envelope)
            self.assertIn("rule 4", message)
            self.assertIn("U+%04X" % code, message)

    def test_a_raw_bidi_control_is_rejected(self):
        # Raw, not escaped: legal inside a JSON string, so json.loads accepts
        # it and only the scans refuse it.
        envelope = '{"status": "OK", "answer": "a%sb"}' % chr(0x202E)
        self.assertIn("rule 4", _reject(self, envelope))

    def test_the_bidi_set_is_exactly_nine_characters(self):
        self.assertEqual(len(validate.BIDI_CONTROL_CHARS), 9)

    def test_ordinary_non_ascii_text_is_still_accepted(self):
        # The bidi refusal must not become a refusal of non-Latin scripts. Both
        # of these are ordinary letters, and an Arabic answer is a legitimate
        # research result.
        parsed = validate.validate_envelope(
            '{"status": "OK", "answer": "\\u0645\\u0631\\u062d\\u0628\\u0627 \\u4e2d"}'
        )
        self.assertEqual(parsed["answer"], "مرحبا 中")


class TestValidEnvelopes(unittest.TestCase):
    """Two envelopes the validator must accept, not two the profile should send.

    These are deliberately looser than the SOUL.md contract: the OK envelope
    carries bare-string quotes and a null `reason`, neither of which the profile
    is meant to produce. The validator's job is to stop the shapes the broker
    cannot safely hand to the board, and these are not those, so it accepts
    them -- that gap between the two contracts is what these cases pin down.
    """

    def test_a_full_ok_envelope(self):
        envelope = (
            '{"status": "OK",'
            ' "answer": "The page says the release is 1.2.3.",'
            ' "sources": ["https://example.invalid/release-notes"],'
            ' "quotes": ["Release 1.2.3 is now available."],'
            ' "reason": null}'
        )
        parsed = validate.validate_envelope(envelope)
        self.assertEqual(parsed["status"], "OK")
        self.assertEqual(parsed["sources"], ["https://example.invalid/release-notes"])

    def test_a_fail_closed_unassessed_envelope_with_empty_lists(self):
        envelope = (
            '{"status": "UNASSESSED",'
            ' "answer": "",'
            ' "sources": [],'
            ' "quotes": [],'
            ' "reason": "the URL did not resolve"}'
        )
        parsed = validate.validate_envelope(envelope)
        self.assertEqual(parsed["status"], "UNASSESSED")
        self.assertEqual(parsed["sources"], [])
        self.assertEqual(parsed["quotes"], [])

    def test_unknown_keys_are_data_not_a_rejection(self):
        # The consumer validates the rest, unconditionally, and treats unknown
        # keys as data. The broker reads none of these fields.
        parsed = validate.validate_envelope('{"status": "OK", "whatever": {"a": [1, 2]}}')
        self.assertEqual(parsed["whatever"], {"a": [1, 2]})

    def test_the_parsed_object_is_returned_not_the_string(self):
        self.assertIsInstance(validate.validate_envelope('{"status": "OK"}'), dict)


class TestContainmentGuard(unittest.TestCase):
    """`_containment_check`: three soft fails, one pass, one hard refusal.

    The guard reads `model_tools._last_resolved_tool_names`, which the stub
    module carries; each test sets it and reads the verdict. `None` means
    "carry on", a string means "refuse and return this to the model".
    """

    TOOL = "safer_reader_complete"
    FOUR = [
        "web_search",
        "web_extract",
        "safer_reader_task",
        "safer_reader_complete",
    ]

    def setUp(self):
        stub_model_tools._last_resolved_tool_names = list(self.FOUR)

    def tearDown(self):
        stub_model_tools._last_resolved_tool_names = []

    def test_the_four_tool_surface_passes(self):
        self.assertIsNone(broker._containment_check(self.TOOL))

    def test_tool_search_bridges_pass(self):
        # Progressive disclosure replaces plugin tools with these; a bridged
        # list is not a containment failure.
        stub_model_tools._last_resolved_tool_names = [
            "web_search",
            "web_extract",
            "tool_search",
            "tool_describe",
            "tool_call",
            self.TOOL,
        ]
        self.assertIsNone(broker._containment_check(self.TOOL))

    def test_an_unexpected_tool_is_a_hard_refusal(self):
        stub_model_tools._last_resolved_tool_names = self.FOUR + ["kanban_create"]
        verdict = broker._containment_check(self.TOOL)
        self.assertIsNotNone(verdict)
        self.assertIn("kanban_create", verdict)
        self.assertIn("refused", verdict)

    def test_the_refusal_names_every_unexpected_tool(self):
        stub_model_tools._last_resolved_tool_names = self.FOUR + [
            "kanban_create",
            "shell_exec",
        ]
        verdict = broker._containment_check(self.TOOL)
        self.assertIn("kanban_create", verdict)
        self.assertIn("shell_exec", verdict)

    def test_a_missing_symbol_is_a_soft_fail(self):
        del stub_model_tools._last_resolved_tool_names
        try:
            with self.assertLogs(broker.logger, level="WARNING"):
                self.assertIsNone(broker._containment_check(self.TOOL))
        finally:
            stub_model_tools._last_resolved_tool_names = []

    def test_an_empty_list_is_a_soft_fail_not_a_vacuous_pass(self):
        # THE REGRESSION THIS EXISTS FOR. The module global is initialised to
        # [], so "empty" is the never-populated state, not a four-tool surface.
        # An `is None` test alone sails past it: the set subtraction over an
        # empty list yields no unexpected tools, and the guard reports
        # containment intact on the strength of a list nobody ever wrote.
        # Soft fail is right -- it must not take completion down -- but it must
        # be a soft fail with a log line, not a silent pass.
        stub_model_tools._last_resolved_tool_names = []
        with self.assertLogs(broker.logger, level="WARNING"):
            self.assertIsNone(broker._containment_check(self.TOOL))

    def test_a_list_without_the_running_tool_is_a_soft_fail(self):
        # A tool the model has just called is in the surface the model was
        # served, by construction. Its absence proves the list is stale, so the
        # list says nothing about containment either way -- including when it
        # is stale AND contains a kanban tool, which must not be reported as a
        # live containment failure.
        stub_model_tools._last_resolved_tool_names = ["web_search", "kanban_show"]
        with self.assertLogs(broker.logger, level="WARNING"):
            self.assertIsNone(broker._containment_check(self.TOOL))

    def test_the_guard_is_per_tool(self):
        # The read tool asks about itself, and gets the same three states.
        stub_model_tools._last_resolved_tool_names = list(self.FOUR)
        self.assertIsNone(broker._containment_check("safer_reader_task"))
        stub_model_tools._last_resolved_tool_names = ["safer_reader_complete"]
        with self.assertLogs(broker.logger, level="WARNING"):
            self.assertIsNone(broker._containment_check("safer_reader_task"))


class _ClosingConn(object):
    """A board connection whose `close()` raises, which is the whole point."""

    def __init__(self, raise_on_close=True):
        self.raise_on_close = raise_on_close
        self.closed = False

    def close(self):
        self.closed = True
        if self.raise_on_close:
            raise RuntimeError("the board connection failed while closing")


class _FakeKb(object):
    """Just enough of `hermes_cli.kanban_db` for the two handlers' one call each."""

    def __init__(self, complete_result=True, raises=None):
        self.complete_result = complete_result
        self.raises = raises
        self.complete_calls = []

    def complete_task(self, conn, task_id, **kwargs):
        self.complete_calls.append((task_id, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.complete_result

    def get_task(self, conn, task_id):
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(id=task_id, title="A title", body="A body")


class TestPostWriteFailures(unittest.TestCase):
    """A failure AFTER the write must not turn a completed task into an error.

    THE REGRESSION THIS EXISTS FOR: with the close inside the same `try` as
    `complete_task`, a connection that failed while closing -- after the write
    had committed -- returned an error, and the model retried a task that was
    already done. The property being locked down is that the success return is
    unreachable by any post-write raise.

    These tests replace the six kanban helpers on the broker module with
    functions of their own, so the handler's control flow runs end to end
    against a fake board. They assert the plugin's own flow and nothing about
    Hermes: what `complete_task` does with its arguments is the live
    verification items' business, not a fake's.
    """

    PATCHED = (
        "_connect",
        "_default_task_id",
        "_enforce_worker_task_ownership",
        "_reject_delegated_child_mutation",
        "_stamp_worker_session_metadata",
        "_worker_run_id",
    )
    ENVELOPE = '{"status": "OK", "answer": "done", "sources": [], "quotes": []}'

    def setUp(self):
        stub_model_tools._last_resolved_tool_names = [
            "web_search",
            "web_extract",
            "safer_reader_task",
            "safer_reader_complete",
        ]
        self._saved = {name: getattr(broker, name) for name in self.PATCHED}
        self.conn = _ClosingConn()
        self.kb = _FakeKb()
        broker._connect = lambda board=None: (self.kb, self.conn)
        broker._default_task_id = lambda arg: "t_fixture"
        broker._enforce_worker_task_ownership = lambda tid: None
        broker._reject_delegated_child_mutation = lambda tool: None
        broker._stamp_worker_session_metadata = lambda tid, meta: meta
        broker._worker_run_id = lambda tid: 7

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(broker, name, value)
        stub_model_tools._last_resolved_tool_names = []

    def test_a_failing_close_does_not_replace_the_success_return(self):
        with self.assertLogs(broker.logger, level="ERROR"):
            result = json.loads(broker.handle_complete({"envelope": self.ENVELOPE}))
        self.assertTrue(self.conn.closed)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("task_id"), "t_fixture")
        self.assertEqual(result.get("run_id"), 7)
        self.assertNotIn("error", result)

    def test_the_write_still_happened_exactly_once(self):
        with self.assertLogs(broker.logger, level="ERROR"):
            broker.handle_complete({"envelope": self.ENVELOPE})
        self.assertEqual(len(self.kb.complete_calls), 1)
        task_id, kwargs = self.kb.complete_calls[0]
        self.assertEqual(task_id, "t_fixture")
        self.assertEqual(kwargs["expected_run_id"], 7)
        self.assertEqual(kwargs["result"], self.ENVELOPE)

    def test_a_failing_close_does_not_replace_the_read_result(self):
        with self.assertLogs(broker.logger, level="ERROR"):
            result = json.loads(broker.handle_task({}))
        self.assertTrue(self.conn.closed)
        self.assertEqual(result.get("title"), "A title")
        self.assertEqual(result.get("body"), "A body")

    def test_a_clean_close_returns_success_and_logs_nothing(self):
        self.conn.raise_on_close = False
        with self.assertNoLogs(broker.logger):
            result = json.loads(broker.handle_complete({"envelope": self.ENVELOPE}))
        self.assertTrue(result.get("ok"))
        self.assertTrue(self.conn.closed)

    def test_a_failing_write_is_still_an_error_and_still_closes(self):
        self.kb.raises = RuntimeError("database is locked")
        with self.assertLogs(broker.logger, level="ERROR"):
            result = json.loads(broker.handle_complete({"envelope": self.ENVELOPE}))
        self.assertIn("error", result)
        self.assertIn("worker log", result["error"])
        self.assertNotIn("database is locked", result["error"])
        self.assertTrue(self.conn.closed)

    def test_complete_task_returning_false_is_an_error_naming_the_task(self):
        self.kb.complete_result = False
        with self.assertLogs(broker.logger, level="ERROR"):
            result = json.loads(broker.handle_complete({"envelope": self.ENVELOPE}))
        self.assertIn("t_fixture", result["error"])

    def test_a_connect_failure_says_nothing_was_written(self):
        def _boom(board=None):
            raise RuntimeError("no such board file")

        broker._connect = _boom
        with self.assertLogs(broker.logger, level="ERROR"):
            result = json.loads(broker.handle_complete({"envelope": self.ENVELOPE}))
        self.assertIn("nothing was written", result["error"])
        self.assertNotIn("no such board file", result["error"])

    def test_a_bad_envelope_never_reaches_the_board(self):
        result = json.loads(broker.handle_complete({"envelope": "not json"}))
        self.assertIn("still in-flight", result["error"])
        self.assertEqual(self.kb.complete_calls, [])
        self.assertFalse(self.conn.closed)

    def test_a_missing_run_id_never_reaches_the_board(self):
        broker._worker_run_id = lambda tid: None
        result = json.loads(broker.handle_complete({"envelope": self.ENVELOPE}))
        self.assertIn("HERMES_KANBAN_RUN_ID", result["error"])
        self.assertEqual(self.kb.complete_calls, [])
        self.assertFalse(self.conn.closed)


class TestRejectionFraming(unittest.TestCase):
    """Every envelope rejection keeps upstream's still-in-flight framing."""

    def test_the_framing_is_present(self):
        message = broker._rejection("-- rule 3: status must be one of ...")
        self.assertIn("still in-flight", message)
        self.assertIn("no state change", message)
        self.assertIn("safer_reader_complete again", message)


if __name__ == "__main__":
    unittest.main()
