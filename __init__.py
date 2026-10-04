"""Herdr pane integration for Hermes Agent.

Claims the Herdr pane Hermes runs in and keeps it current with:

* **state** - ``working`` while a turn runs, ``idle`` when Hermes is ready for
  input, ``blocked`` while Hermes waits for an answer or a command approval
  (the reason rides along as Herdr's ``--message``).
* **resume** - the command that reopens the current session, so a Herdr server
  restart brings the same conversation back into the same pane.
* **release** - on exit, so the pane drops back to a plain shell.

This is the agent-owned integration from
https://herdr.dev/docs/add-herdr-support/ : reports go through
``$HERDR_BIN_PATH`` and only when ``HERDR_ENV=1``. Hermes is otherwise a
"supported by Herdr" agent, i.e. Herdr reads its state off the screen; while
this plugin reports, Herdr uses the reports instead.

Two Herdr-side facts worth knowing:

* A pane is owned by one reporting source. This plugin supersedes the
  session-only integration that ``herdr integration install hermes`` writes to
  ``~/.hermes/plugins/herdr-agent-state`` - keep that one disabled.
* Herdr identifies an agent by its foreground process, and Hermes' launcher
  execs into ``python3``, so Herdr cannot see it as ``hermes`` by itself. Launch
  Hermes as ``HERDR_AGENT=hermes hermes`` inside a pane: that is what gives
  Herdr the process identity it needs, including for ``herdr agent prompt``.

Outside Herdr every callback is a no-op.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import subprocess
import sys
import threading
import time

_log = logging.getLogger("plugins.herdr-pane-state")

_SOURCE = "hermes:pane-state"  # stable, unique, never "herdr:"-prefixed
_AGENT = "hermes"  # the label Herdr shows in the sidebar
_TIMEOUT = 1.0  # reports are best-effort; never let one slow Hermes down
_MAX_MESSAGE = 180
_INTERACTIVE_PLATFORMS = {"cli", "tui", "desktop", "acp"}
_SESSION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

_lock = threading.Lock()
_send_lock = threading.Lock()  # serializes reports: release never overtakes one
_pending: tuple | None = None
_wake = threading.Event()
_worker: threading.Thread | None = None
_seq = 0
_gen = 0  # bumped by every release; drops reports queued before it
_claimed = False
_released = False
_last_session: str | None = None
_surface = "cli"


# --------------------------------------------------------------------------- #
# Herdr plumbing
# --------------------------------------------------------------------------- #
def _bin() -> str:
    return os.environ.get("HERDR_BIN_PATH", "")


def _pane() -> str:
    return os.environ.get("HERDR_PANE_ID", "")


def _in_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1" and bool(_bin()) and bool(_pane())


def _next_seq() -> int:
    """Report sequence; nanoseconds keep it rising across process restarts."""
    global _seq
    with _lock:
        _seq = max(_seq + 1, time.time_ns())
        return _seq


def _clean(text: str) -> str:
    return " ".join(str(text).split())[:_MAX_MESSAGE]


def _valid_session_id(session_id: object) -> str | None:
    if isinstance(session_id, str) and _SESSION_ID_RE.match(session_id):
        return session_id
    return None


def _resume_argv(session_id: str | None) -> list[str]:
    """Command Herdr runs to reopen this session (first word: a plain name)."""
    if not session_id:
        return []
    if _surface == "tui":
        return ["hermes", "--tui", "--resume", session_id]
    return ["hermes", "--resume", session_id]


def _run(argv: list) -> None:
    """Fire one Herdr report; a failure is logged at debug and dropped."""
    try:
        subprocess.run(
            argv, check=False, timeout=_TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        _log.debug("herdr report failed: %r (%s)", exc, argv)


def _send(op: tuple) -> None:
    global _claimed, _released, _last_session
    if op[0] == "release":
        _released = True
        _run([
            _bin(), "pane", "release-agent", _pane(),
            "--source", _SOURCE, "--agent", _AGENT, "--seq", str(_next_seq()),
        ])
        return
    _, state, message, session_id = op
    resume = _resume_argv(session_id)
    argv = [
        _bin(), "pane", "report-agent", _pane(),
        "--source", _SOURCE, "--agent", _AGENT,
        "--seq", str(_next_seq()), "--state", state,
    ]
    if message:
        argv += ["--message", message]
    if session_id:
        argv += ["--agent-session-id", session_id]
    if resume:
        argv += ["--", *resume]
    _claimed = True
    _released = False
    _run(argv)
    # Session identity rides on its own report: a state report carries the
    # resume command (that is what Herdr restores from), the session id is
    # extra. Current Herdr versions record sessions only from their own
    # "herdr:"-prefixed integrations, so this is best-effort - send it anyway so
    # nothing has to change the day Herdr accepts third-party session ids.
    if session_id and session_id != _last_session:
        _last_session = session_id
        argv = [
            _bin(), "pane", "report-agent-session", _pane(),
            "--source", _SOURCE, "--agent", _AGENT,
            "--seq", str(_next_seq()), "--agent-session-id", session_id,
        ]
        if resume:
            argv += ["--", *resume]
        _run(argv)


def _drain() -> None:
    """Send pending reports; a newer report always replaces an unsent one."""
    global _pending
    while True:
        try:
            with _lock:
                item = _pending
                _pending = None
                _wake.clear()
            if item is None:
                return
            gen, op = item
            with _send_lock:
                if op[0] == "release" or gen == _gen:
                    _send(op)  # stale state (a release happened after it) is dropped
                else:
                    _log.debug("dropping stale herdr report %r", op)
        except Exception:  # never surface a reporting problem inside a turn
            _log.debug("herdr report failed", exc_info=True)
            return


def _loop() -> None:
    while True:
        _wake.wait()
        _drain()


def _submit(op: tuple) -> None:
    global _pending, _worker, _gen
    if not _in_herdr():
        return
    with _lock:
        if op[0] == "release":
            if _released or (_pending and _pending[1][0] == "release"):
                return
            _gen += 1
            _pending = None  # old-era reports must not re-claim the pane
        _pending = (_gen, op)
        start = _worker is None
        if start:
            _worker = threading.Thread(target=_loop, name="herdr-pane-state", daemon=True)
    _wake.set()
    if start:
        _worker.start()


def _report(state: str, *, message: str | None = None, session_id: object = None) -> None:
    _submit(("state", state, _clean(message) if message else None, _valid_session_id(session_id)))


def _release_now() -> None:
    """Hand the pane back on the way out - synchronously, worker or not."""
    global _pending, _gen
    with _lock:
        if _released or not _claimed:
            return
        _gen += 1
        _pending = None  # queued reports must not re-claim the pane after this
    if not _in_herdr():
        return
    try:
        with _send_lock:
            _send(("release",))
    except Exception:
        _log.debug("herdr release failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Hook callbacks (keyword-only; never raise, never return a directive)
# --------------------------------------------------------------------------- #
def _note_surface(platform: object) -> bool:
    """Remember the surface (it picks the resume command) and gate reporting."""
    global _surface
    if isinstance(platform, str) and platform:
        _surface = platform
    return platform is None or platform in _INTERACTIVE_PLATFORMS


def _turn_start(session_id=None, platform=None, **_kw) -> None:
    if _note_surface(platform):
        _report("working", session_id=session_id)


def _turn_end(session_id=None, platform=None, **_kw) -> None:
    if _note_surface(platform):
        _report("idle", session_id=session_id)


def _tool_start(tool_name=None, args=None, session_id=None, **_kw) -> None:
    if tool_name != "clarify":
        return
    question = args.get("question") if isinstance(args, dict) else None
    _report(
        "blocked",
        message=question if isinstance(question, str) and question else "waiting for your answer",
        session_id=session_id,
    )


def _tool_end(tool_name=None, session_id=None, **_kw) -> None:
    if tool_name == "clarify":
        _report("working", session_id=session_id)


def _approval_requested(command=None, description=None, **_kw) -> None:
    reason = description or command
    _report("blocked", message=reason if isinstance(reason, str) and reason else "waiting for approval")


def _approval_answered(**_kw) -> None:
    _report("working")


def _loop_stopped(**_kw) -> None:
    _report("idle")


def _session_started(session_id=None, platform=None, **_kw) -> None:
    if _note_surface(platform):
        _report("idle", session_id=session_id)


def _session_reset(session_id=None, new_session_id=None, platform=None, **_kw) -> None:
    if _note_surface(platform):
        _report("idle", session_id=session_id or new_session_id)


def _session_finalize(**_kw) -> None:
    _release_now()


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def _interactive_launch(argv: list) -> bool:
    """True for a launch that sits at a prompt, not a one-shot subcommand."""
    rest = list(argv[1:])
    if not rest:
        return True
    first = rest[0]
    if first.startswith("-"):
        return True  # hermes, hermes --tui, hermes -c, hermes --resume ...
    return first in {"chat", "desktop"}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _turn_start)
    ctx.register_hook("post_llm_call", _turn_end)
    ctx.register_hook("on_session_end", _turn_end)
    ctx.register_hook("pre_tool_call", _tool_start)
    ctx.register_hook("post_tool_call", _tool_end)
    ctx.register_hook("pre_approval_request", _approval_requested)
    ctx.register_hook("post_approval_response", _approval_answered)
    ctx.register_hook("agent_loop_stopped", _loop_stopped)
    ctx.register_hook("on_session_start", _session_started)
    ctx.register_hook("on_session_reset", _session_reset)
    ctx.register_hook("on_session_finalize", _session_finalize)
    atexit.register(_release_now)
    # Claim the pane as soon as an interactive Hermes starts, so the sidebar
    # shows the agent before its first turn.
    if _in_herdr() and _interactive_launch(sys.argv):
        _report("idle")
