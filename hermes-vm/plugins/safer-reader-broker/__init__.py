"""safer-reader-broker: the two tools the quarantined reader profile keeps.

The ``safer_web_reader`` profile disables the whole ``kanban`` toolset, because
Hermes has no per-tool disable for built-ins and the dispatcher force-appends
the toolset to every worker it spawns. That disable costs the worker both its
completion path and its ability to read its own instruction -- the dispatcher's
prompt is ``work kanban task <id>`` and nothing else, and the task body reaches
the model only through a tool call. This plugin gives back exactly those two
capabilities and nothing else:

  ``safer_reader_task()``            title and body of this worker's own task.
  ``safer_reader_complete(envelope)`` completion, with one string parameter.

Every identifying value -- task id, run id, board, session id -- is read from
the worker's environment inside this module. None of it is expressible by the
model, which is the whole point: there is no ``board``, no ``task_id``, no
``created_cards``, no ``artifacts`` and no ``metadata`` on either schema.

Design: docs/superpowers/specs/2026-08-29-safer-reader-complete-broker-design.md
Canonical copy: hermes-vm/plugins/safer-reader-broker/ in the kubernetes_config
repository. Nothing enforces parity between that copy and this installed one.

Hermes imports are all top-level, deliberately: this plugin depends on six
underscore-prefixed helpers that carry no upstream stability promise, so a
rename must fail at worker start with one warning line in the per-task log,
not at tool-call time in the middle of a run.
"""

from __future__ import annotations

import json
import logging

import model_tools
from tools.kanban_tools import (
    _connect,
    _default_task_id,
    _enforce_worker_task_ownership,
    _reject_delegated_child_mutation,
    _stamp_worker_session_metadata,
    _worker_run_id,
)
from tools.registry import tool_error

from . import validate

logger = logging.getLogger(__name__)

# The reader's whole intended surface. Containment rests on
# ``agent.disabled_toolsets: [kanban]`` and on nothing else --
# ``platform_toolsets.cli: [web]`` is not an allowlist, because
# ``_get_platform_tools`` adds every non-configurable toolset back afterwards
# and ``kanban`` is not configurable. So the guard below checks that one
# mechanism, in process, before either tool does anything.
EXPECTED_TOOLS = frozenset(
    {
        "web_search",
        "web_extract",
        "safer_reader_task",
        "safer_reader_complete",
    }
)

# Tool Search progressive disclosure replaces plugin and MCP tools with these
# three bridges once the deferrable surface grows past roughly 10% of the
# context window. Two small schemas will not come close, but the bridges are
# not toolset tools and their arrival is not a containment failure.
INTERNALS_ALLOWED = frozenset({"tool_search", "tool_describe", "tool_call"})

SAFER_READER_TASK = {
    "name": "safer_reader_task",
    "description": (
        "Return your own task's title and body. Takes no arguments; the task "
        "is resolved from your worker environment. This is the only way your "
        "instruction and its URLs reach you, so call it first."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

SAFER_READER_COMPLETE = {
    "name": "safer_reader_complete",
    "description": (
        "Complete your own task with the research envelope. The envelope is a "
        "JSON object with status OK or UNASSESSED, at most 65536 bytes, and no "
        "control characters other than newline and tab. A rejected envelope "
        "leaves your task untouched and in flight, so you can correct it and "
        "call again. This is the only way to end your run successfully."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "envelope": {
                "type": "string",
                "description": (
                    "The research envelope as a JSON string, with all five "
                    "keys your instructions define."
                ),
            },
        },
        "required": ["envelope"],
    },
}


def _containment_check(tool_name):
    """Return a refusal string when the worker's tool surface has widened.

    ``_compute_tool_definitions`` writes the model's actual post-filter tool
    list to ``model_tools._last_resolved_tool_names`` before returning, in this
    process, before the first turn. That list is exactly what a containment
    failure would widen, so both handlers read it.

    Two opposite behaviours, both deliberate:

    * a list this guard cannot trust is a SOFT fail -- one log line and carry
      on. The guard is defence in depth behind the recorded tool-list diff, and
      neither an upstream rename nor a stale global may take completion down;
    * a trustworthy list showing an unexpected tool is a HARD refusal. The
      operator then sees it the first time a task runs, rather than whenever
      somebody next remembers a checklist.

    THREE STATES ARE UNTRUSTWORTHY, and only the third is obvious:

    1. the symbol is absent -- an upstream rename;
    2. the list is empty -- the module global is initialised to ``[]``, so
       "empty" is the never-populated state and NOT a four-tool surface. A
       plain ``is None`` test would sail past it and report containment intact
       on the strength of a list nobody ever wrote;
    3. the list does not contain the running tool's own name. A tool the model
       just called is in the surface the model was served, by construction, so
       its absence proves the global is stale (or belongs to another process's
       computation) rather than proving anything about containment.

    Only a populated list that names this tool is evidence, and only then is an
    unexpected name a refusal. The refusal is on ANY widening rather than on a
    ``kanban_`` prefix: the routes that can widen this surface are not all
    kanban ones. A future release letting the force-append survive
    ``disabled_toolsets``, and the ``_RECENTLY_SHIPPED_TOOLSETS`` path, are the
    two known ones.
    """
    resolved = getattr(model_tools, "_last_resolved_tool_names", None)
    if resolved is None:
        logger.warning(
            "safer-reader-broker: model_tools._last_resolved_tool_names is "
            "missing, so %s ran without the containment guard; the recorded "
            "tool-list diff is now the only detector",
            tool_name,
        )
        return None
    if not resolved:
        logger.warning(
            "safer-reader-broker: model_tools._last_resolved_tool_names is "
            "empty, which is its never-populated initial value, so %s ran "
            "without the containment guard; the recorded tool-list diff is "
            "now the only detector",
            tool_name,
        )
        return None
    if tool_name not in resolved:
        logger.warning(
            "safer-reader-broker: model_tools._last_resolved_tool_names does "
            "not contain %s, which the model has just called, so the list is "
            "stale and %s ran without the containment guard; the recorded "
            "tool-list diff is now the only detector",
            tool_name,
            tool_name,
        )
        return None
    unexpected = sorted(set(resolved) - EXPECTED_TOOLS - INTERNALS_ALLOWED)
    if unexpected:
        return tool_error(
            "{0} refused: this worker's tool surface has widened beyond the "
            "four tools the safer_web_reader profile allows. Unexpected "
            "tools: {1}. The profile's containment is broken; tell the "
            "operator. Nothing was written to the board.".format(
                tool_name, ", ".join(unexpected)
            )
        )
    return None


def _own_task_id(tool_name):
    """Return (task_id, None) or (None, error string) for this worker's task."""
    tid = _default_task_id(None)
    if not tid:
        return None, tool_error(
            "{0}: no task in scope (HERMES_KANBAN_TASK is unset, or this is "
            "not a dispatcher-owned worker)".format(tool_name)
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return None, ownership_err
    return tid, None


def _close_quietly(conn):
    """Close the board connection. A failure closing it must change no answer.

    This is the whole of the post-write-raise fix, and it is a function rather
    than a ``finally: conn.close()`` for one reason: a bare close inside a
    ``try`` hands its own exception to that ``try``'s handler, so a connection
    that fails to close AFTER ``complete_task`` has returned True turns a
    completed task into an error the model then retries. Swallowing the failure
    here is right because there is nothing else to do with it -- the write has
    already committed or already not, and the operator's copy is the log line.
    """
    try:
        conn.close()
    except Exception:  # noqa: BLE001 -- deliberately swallowed, see above
        logger.exception("safer-reader-broker: closing the board connection failed")


def _rejection(detail):
    """Build the one envelope-rejection message, with upstream's retry framing.

    The trailing two sentences are copied from upstream's
    ``HallucinatedCardsError`` reply, which spells out "still in-flight ...
    retry" precisely because a model reading a bare tool error often treats it
    as terminal and stops. Every rejection path here shares them, so no path
    can quietly lose them.
    """
    return (
        "safer_reader_complete rejected the envelope {0}. Your task is still "
        "in-flight (no state change). Fix the envelope and call "
        "safer_reader_complete again.".format(detail)
    )


def handle_task(args, **kwargs):
    """Return this worker's own task title and body, and nothing else.

    Deliberately narrower than ``kanban_show``, which it replaces: no
    ``task_id`` or ``board`` argument, and no call to ``build_worker_context``,
    so no comment thread, parent handoff or child card id enters the reader's
    context.
    """
    guard_err = _containment_check("safer_reader_task")
    if guard_err:
        return guard_err
    delegated_err = _reject_delegated_child_mutation("safer_reader_task")
    if delegated_err:
        return delegated_err
    tid, scope_err = _own_task_id("safer_reader_task")
    if scope_err:
        return scope_err
    # Each guarded region covers exactly one call that can fail meaningfully,
    # and the close goes through _close_quietly, which raises nothing. So the
    # response below is reachable whatever happens on the way out. Nothing here
    # mutates the board, so the shape is not load-bearing for the read tool --
    # it matches handle_complete, where it is.
    try:
        kb, conn = _connect()
    except Exception:  # noqa: BLE001 -- a tool handler returns, never raises
        # The exception text is for the worker log, never for the model: it can
        # carry a board path or a database message the reader has no business
        # seeing, and the model can do nothing with it either way.
        logger.exception("safer_reader_task could not connect to the board")
        return tool_error(
            "safer_reader_task could not open the board; the cause is in the "
            "worker log."
        )
    try:
        task = kb.get_task(conn, tid)
    except Exception:  # noqa: BLE001 -- a tool handler returns, never raises
        logger.exception("safer_reader_task failed")
        return tool_error(
            "safer_reader_task failed to read the task; the cause is in the "
            "worker log."
        )
    finally:
        _close_quietly(conn)
    if task is None:
        return tool_error("safer_reader_task: task {0} not found".format(tid))
    return json.dumps(
        {"ok": True, "title": task.title, "body": task.body or ""},
        ensure_ascii=False,
    )


def handle_complete(args, **kwargs):
    """Complete this worker's own task with the envelope, or refuse it.

    Validation runs before any connection opens, so a rejection is never a
    partial write: the task is untouched and the model can correct the envelope
    inside the same run. Every rejection goes through :func:`_rejection`, which
    carries the retry framing.
    """
    guard_err = _containment_check("safer_reader_complete")
    if guard_err:
        return guard_err
    delegated_err = _reject_delegated_child_mutation("safer_reader_complete")
    if delegated_err:
        return delegated_err

    envelope = (args or {}).get("envelope")
    try:
        validate.validate_envelope(envelope)
    except validate.EnvelopeError as exc:
        return tool_error(_rejection("-- {0}".format(exc)))
    except Exception:  # noqa: BLE001 -- see below
        # Not every refusal arrives as an EnvelopeError. A deeply nested
        # envelope raises RecursionError out of json.loads, and out of
        # _walk_strings on the decoded value; anything else the validator
        # manages to raise lands here too. Left uncaught it would escape to
        # registry.dispatch, which reports a tool failure WITHOUT the
        # still-in-flight framing -- and a model reading a bare failure treats
        # it as terminal and stops, on a task nothing has written to. So it is
        # a rejection like any other, with the cause kept in the log.
        logger.exception("safer_reader_complete: envelope validation raised")
        return tool_error(
            _rejection("-- it could not be validated; the cause is in the "
                       "worker log. Send a simpler, shallower envelope")
        )

    tid, scope_err = _own_task_id("safer_reader_complete")
    if scope_err:
        return scope_err

    # Upstream treats a missing run id as "no run to pin" and takes the
    # UNGUARDED update path, which completes a task in four states with no run
    # check. That tolerance exists for the human CLI and for orchestrators;
    # this broker is neither, and inside a dispatcher-spawned worker
    # HERMES_KANBAN_RUN_ID is always set.
    run_id = _worker_run_id(tid)
    if run_id is None:
        return tool_error(
            "safer_reader_complete refused: HERMES_KANBAN_RUN_ID is missing or "
            "not an integer, so the completion cannot be pinned to this run. "
            "Nothing was written to the board."
        )

    metadata = _stamp_worker_session_metadata(tid, None)
    # NOTHING RAISED AFTER complete_task MAY REACH A HANDLER THAT RETURNS AN
    # ERROR. Once that call has returned True the board has changed, and an
    # error return would have the model retry a task that is already done. Two
    # things give that property: the second try guards exactly the one call
    # that can fail meaningfully, and the close runs through _close_quietly,
    # which raises nothing. The success line below is therefore unreachable by
    # any post-write raise, and json.dumps over three scalars cannot raise
    # either. Widening either try back over the close would undo this.
    try:
        kb, conn = _connect()
    except Exception:  # noqa: BLE001 -- a tool handler returns, never raises
        # Log the cause; do not hand it to the model. A database message can
        # name the board file, and the model can act on none of it.
        logger.exception("safer_reader_complete could not connect to the board")
        return tool_error(
            "safer_reader_complete could not open the board, so nothing was "
            "written; the cause is in the worker log."
        )
    try:
        ok = kb.complete_task(
            conn,
            tid,
            result=envelope,
            metadata=metadata,
            expected_run_id=run_id,
        )
    except Exception:  # noqa: BLE001 -- a tool handler returns, never raises
        logger.exception("safer_reader_complete failed")
        return tool_error(
            "safer_reader_complete failed to write to the board; the cause is "
            "in the worker log. The task may or may not have been completed, "
            "so do not assume either."
        )
    finally:
        _close_quietly(conn)
    if not ok:
        return tool_error(
            "safer_reader_complete: could not complete {0} (unknown id, "
            "already terminal, or a different run holds it)".format(tid)
        )
    return json.dumps({"ok": True, "task_id": tid, "run_id": run_id})


def register(ctx) -> None:
    """Register both tools into the existing ``web`` toolset.

    ``web`` rather than a toolset of our own: the dispatcher resolves the
    ``--toolsets`` pin in the GATEWAY process, which does not load this
    profile's plugins, so a fresh plugin toolset name would be filtered out of
    that pin and the first dispatch after install would silently omit the
    broker. ``web`` has no such ordering problem, and it is what makes
    ``platform_toolsets.cli: [web]`` deliver all four tools.

    The ``None`` check is not decoration. ``PluginContext.register_tool``
    returns ``None`` and logs a warning when a name would shadow a global tool
    without ``override=True``; without this raise, ``register()`` would return
    normally, the plugin would report healthy with one tool instead of two, and
    the diagnosis path would show green.
    """
    task = ctx.register_tool(
        name="safer_reader_task",
        toolset="web",
        schema=SAFER_READER_TASK,
        handler=handle_task,
        emoji="📋",
    )
    complete = ctx.register_tool(
        name="safer_reader_complete",
        toolset="web",
        schema=SAFER_READER_COMPLETE,
        handler=handle_complete,
        emoji="✅",
    )
    if task is None or complete is None:
        raise RuntimeError("safer-reader-broker: a tool registration was refused")
