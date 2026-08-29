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

    * the symbol missing is a SOFT fail -- one log line and carry on. The guard
      is defence in depth behind the recorded tool-list diff, and an upstream
      rename must not take completion down with it;
    * the symbol present and showing an unexpected tool is a HARD refusal. The
      operator then sees it the first time a task runs, rather than whenever
      somebody next remembers a checklist.

    The refusal is on ANY widening rather than on a ``kanban_`` prefix: the
    routes that can widen this surface are not all kanban ones. A future
    release letting the force-append survive ``disabled_toolsets``, and the
    ``_RECENTLY_SHIPPED_TOOLSETS`` path, are the two known ones.
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
    try:
        kb, conn = _connect()
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error("safer_reader_task: task {0} not found".format(tid))
            return json.dumps(
                {"ok": True, "title": task.title, "body": task.body or ""},
                ensure_ascii=False,
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- a tool handler returns, never raises
        logger.exception("safer_reader_task failed")
        return tool_error("safer_reader_task: {0}".format(exc))


def handle_complete(args, **kwargs):
    """Complete this worker's own task with the envelope, or refuse it.

    Validation runs before any connection opens, so a rejection is never a
    partial write: the task is untouched and the model can correct the envelope
    inside the same run. The retry framing in the rejection is copied from
    upstream's ``HallucinatedCardsError`` reply, which spells it out because a
    model reading a bare tool error often treats it as terminal and stops.
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
        return tool_error(
            "safer_reader_complete rejected the envelope -- {0}. Your task is "
            "still in-flight (no state change). Fix the envelope and call "
            "safer_reader_complete again.".format(exc)
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
    try:
        kb, conn = _connect()
        try:
            ok = kb.complete_task(
                conn,
                tid,
                result=envelope,
                metadata=metadata,
                expected_run_id=run_id,
            )
            if not ok:
                return tool_error(
                    "safer_reader_complete: could not complete {0} (unknown "
                    "id, already terminal, or a different run holds it)".format(tid)
                )
            return json.dumps({"ok": True, "task_id": tid, "run_id": run_id})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- a tool handler returns, never raises
        logger.exception("safer_reader_complete failed")
        return tool_error("safer_reader_complete: {0}".format(exc))


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
