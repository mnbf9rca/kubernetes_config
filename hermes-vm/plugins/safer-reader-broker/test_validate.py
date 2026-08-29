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
trusted code" section. They are the only thing standing between a
model-controlled string and the board's SQLite file, which a human reads
through the dashboard, and three of the four fail INVISIBLY if they regress: a
validator that accepts everything looks exactly like a validator that is never
given anything bad.

The live checks the design keeps for the VM -- error-to-model, no partial
write, in-run recovery -- are verification item 4 and are not repeated here.
"""

import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "safer_reader_broker_validate", os.path.join(_HERE, "validate.py")
)
validate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate)


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

    def test_a_bidi_override_is_not_a_control_character(self):
        # Recorded rather than asserted as desirable: U+202E is category Cf, and
        # rule 4 as the spec words it covers control characters only. If the
        # estate later decides format characters belong in the rule, this test
        # is the one that changes.
        parsed = validate.validate_envelope('{"status": "OK", "answer": "a\\u202eb"}')
        self.assertEqual(parsed["answer"], "a‮b")


class TestValidEnvelopes(unittest.TestCase):
    """The two shapes the profile is expected to produce."""

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


if __name__ == "__main__":
    unittest.main()
