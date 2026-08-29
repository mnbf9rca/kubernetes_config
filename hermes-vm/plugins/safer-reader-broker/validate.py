"""Envelope validation for the safer-reader-broker plugin.

This module imports nothing from Hermes -- standard library only -- which is
what lets ``test_validate.py`` run on a machine with no Hermes installed. Keep
it that way: the moment a Hermes symbol appears here the test stops being
runnable off the VM, and nothing in this estate runs it on the VM.

The four rules are the broker spec's "Validation, in trusted code" section,
implemented in the order they are numbered there:

  1. the argument is a string of at most 65,536 bytes of UTF-8;
  2. it parses as JSON and the result is an object;
  3. ``status`` is present and is exactly ``OK`` or ``UNASSESSED``;
  4. no control characters other than ``\\n`` and ``\\t``.

Rule 1 runs before parsing and, in the handler, before a database connection
opens, so it bounds what can reach the board's SQLite file.

Rule 4 is a repertoire rule rather than a shape rule, and it is checked in two
places for one reason: ``json.loads`` already rejects a RAW control character
inside a string, but it happily decodes a ``\\u001b`` escape into the parsed
value. So the raw envelope is scanned (which also covers control characters
outside strings, in the whitespace between tokens) and every decoded string --
object keys included -- is scanned as well. Without the second scan an
otherwise rules-valid envelope carries ANSI escape sequences into
``tasks.result`` and out to the operator's terminal and dashboard.

Everything else the envelope claims is the consumer's to validate. The broker
does not check that ``sources`` were fetched, that ``quotes`` appear in them,
or that ``answer`` is honest.
"""

from __future__ import annotations

import json

# Bounds what one tool call can write into the board's SQLite file.
MAX_ENVELOPE_BYTES = 65536

# The fail-closed contract itself. Anything else is a rejection.
ALLOWED_STATUSES = ("OK", "UNASSESSED")

# The two control characters a research answer legitimately contains.
ALLOWED_CONTROL_CHARS = ("\n", "\t")


class EnvelopeError(ValueError):
    """A rules violation. The message names the failing rule.

    The handler wraps this in the estate's retry framing; the message here
    describes only what is wrong with the envelope.
    """


def _first_control_char(text):
    """Return the first disallowed control character in ``text``, or None.

    Control character means Unicode category Cc -- U+0000..U+001F and
    U+007F..U+009F -- minus the two allowed above. That is the definition the
    rule names, and it is deliberately not widened to format characters.
    """
    for char in text:
        if char in ALLOWED_CONTROL_CHARS:
            continue
        code = ord(char)
        if code <= 0x1F or 0x7F <= code <= 0x9F:
            return char
    return None


def _walk_strings(value):
    """Yield every string inside a decoded JSON value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            for found in _walk_strings(item):
                yield found
    elif isinstance(value, list):
        for item in value:
            for found in _walk_strings(item):
                yield found


def validate_envelope(envelope):
    """Return the parsed envelope, or raise :class:`EnvelopeError`.

    The caller must treat a raise as "the task was not touched": this function
    performs no I/O and opens no connection.
    """
    # Rule 1 -- a string, and small enough to store.
    if not isinstance(envelope, str):
        raise EnvelopeError(
            "rule 1: envelope must be a string, got "
            "{0}".format(type(envelope).__name__)
        )
    size = len(envelope.encode("utf-8"))
    if size > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(
            "rule 1: envelope is {0} bytes of UTF-8, over the {1}-byte "
            "limit".format(size, MAX_ENVELOPE_BYTES)
        )

    # Rule 2 -- valid JSON, and an object.
    try:
        parsed = json.loads(envelope)
    except ValueError as exc:
        raise EnvelopeError("rule 2: envelope is not valid JSON: {0}".format(exc))
    if not isinstance(parsed, dict):
        raise EnvelopeError(
            "rule 2: envelope must be a JSON object, got "
            "{0}".format(type(parsed).__name__)
        )

    # Rule 3 -- the fail-closed status contract.
    if "status" not in parsed:
        raise EnvelopeError("rule 3: envelope has no status key")
    status = parsed["status"]
    if status not in ALLOWED_STATUSES:
        raise EnvelopeError(
            "rule 3: status must be one of {0}, got {1!r}".format(
                ", ".join(ALLOWED_STATUSES), status
            )
        )

    # Rule 4 -- no control characters, raw or escaped.
    bad = _first_control_char(envelope)
    if bad is None:
        for text in _walk_strings(parsed):
            bad = _first_control_char(text)
            if bad is not None:
                break
    if bad is not None:
        raise EnvelopeError(
            "rule 4: envelope contains control character U+{0:04X}; only "
            "newline and tab are allowed".format(ord(bad))
        )

    return parsed
