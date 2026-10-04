"""Tests for the Herdr pane-state plugin.

No Herdr and no Hermes runtime required: ``HERDR_BIN_PATH`` points at a fake
binary that records every report, so the tests assert on the exact argv the
plugin sends.

Run:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "__init__.py"
SEP = "\x1f"

FAKE_BIN = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write("\\x1f".join(sys.argv[1:]) + "\\n")
"""


def load_plugin(name: str):
    """Import a fresh copy so module globals never leak between tests."""
    spec = importlib.util.spec_from_file_location(name, PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Ctx:
    def __init__(self) -> None:
        self.hooks: dict = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback
        return None


class FakeHerdr:
    """A HERDR_BIN_PATH stand-in that records the argv of every report."""

    def __init__(self, tmpdir: str) -> None:
        self.dir = Path(tmpdir)
        self.log = self.dir / "reports.log"
        self.bin = self.dir / "herdr"
        self.bin.write_text(FAKE_BIN, encoding="utf-8")
        self.bin.chmod(0o755)
        os.environ.update({
            "FAKE_LOG": str(self.log),
            "HERDR_ENV": "1",
            "HERDR_BIN_PATH": str(self.bin),
            "HERDR_PANE_ID": "w1:p1",
        })

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [line.split(SEP) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def wait_for(self, predicate, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for argv in self.calls():
                if predicate(argv):
                    return argv
            time.sleep(0.02)
        return None


def clear_herdr_env() -> None:
    for key in ("HERDR_ENV", "HERDR_BIN_PATH", "HERDR_PANE_ID", "FAKE_LOG"):
        os.environ.pop(key, None)


def register(mod) -> Ctx:
    ctx = Ctx()
    mod.register(ctx)
    return ctx


def state_call(state: str):
    def match(argv):
        return ("--state" in argv
                and argv[:2] == ["pane", "report-agent"]
                and argv[argv.index("--state") + 1] == state)
    return match


def release_call(argv):
    return argv[:2] == ["pane", "release-agent"]


class OutsideHerdrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        clear_herdr_env()
        self.env_backup = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def test_no_reports_outside_herdr(self) -> None:
        herdr = FakeHerdr(self.tmp.name)
        # ...but pretend the pane variables are missing, as outside Herdr.
        for key in ("HERDR_ENV", "HERDR_BIN_PATH", "HERDR_PANE_ID"):
            os.environ.pop(key, None)
        ctx = register(load_plugin("plugin_outside"))
        for name, kwargs in (
            ("pre_llm_call", {"session_id": "20261004_101010_aaa111", "platform": "cli"}),
            ("pre_approval_request", {"command": "rm -rf /tmp/x"}),
            ("on_session_finalize", {}),
        ):
            ctx.hooks[name](**kwargs)
        time.sleep(0.4)
        self.assertEqual(herdr.calls(), [])


class InHerdrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env_backup = dict(os.environ)
        self.argv_backup = sys.argv
        # An interactive launch, as in a pane (unittest's own argv is not one).
        sys.argv = ["hermes"]
        self.herdr = FakeHerdr(self.tmp.name)
        self.mod = load_plugin("plugin_" + self._testMethodName)
        self.ctx = register(self.mod)

    def tearDown(self) -> None:
        sys.argv = self.argv_backup
        os.environ.clear()
        os.environ.update(self.env_backup)
        self.tmp.cleanup()

    def test_claims_pane_before_first_turn(self) -> None:
        argv = self.herdr.wait_for(state_call("idle"))
        self.assertIsNotNone(argv, "no idle report was sent at startup")
        self.assertEqual(argv[0], "pane")
        self.assertIn("--source", argv)
        self.assertEqual(argv[argv.index("--source") + 1], "hermes:pane-state")
        self.assertEqual(argv[argv.index("--agent") + 1], "hermes")
        self.assertEqual(argv[2], "w1:p1")

    def test_turn_reports_state_and_resume_command(self) -> None:
        session = "20261004_101010_aaa111"
        self.ctx.hooks["pre_llm_call"](session_id=session, platform="cli")
        argv = self.herdr.wait_for(state_call("working"))
        self.assertIsNotNone(argv)
        self.assertEqual(argv[argv.index("--agent-session-id") + 1], session)
        self.assertEqual(argv[argv.index("--") + 1:], ["hermes", "--resume", session])

        self.ctx.hooks["post_llm_call"](session_id=session, platform="cli")
        self.assertIsNotNone(self.herdr.wait_for(state_call("idle")))

    def test_tui_resumes_in_the_tui(self) -> None:
        self.ctx.hooks["pre_llm_call"](session_id="20261004_101010_aaa111", platform="tui")
        argv = self.herdr.wait_for(state_call("working"))
        self.assertEqual(
            argv[argv.index("--") + 1:],
            ["hermes", "--tui", "--resume", "20261004_101010_aaa111"],
        )

    def test_approval_blocks_with_the_reason(self) -> None:
        self.ctx.hooks["pre_approval_request"](
            command="rm -rf /tmp/probe", description="Delete the probe directory"
        )
        argv = self.herdr.wait_for(state_call("blocked"))
        self.assertIsNotNone(argv)
        self.assertEqual(argv[argv.index("--message") + 1], "Delete the probe directory")
        self.ctx.hooks["post_approval_response"](choice="once")
        self.assertIsNotNone(self.herdr.wait_for(state_call("working")))

    def test_long_message_is_truncated(self) -> None:
        self.ctx.hooks["pre_approval_request"](command="x" * 500, description=None)
        argv = self.herdr.wait_for(state_call("blocked"))
        self.assertLessEqual(len(argv[argv.index("--message") + 1]), 180)

    def test_clarify_tool_blocks_and_unblocks(self) -> None:
        self.ctx.hooks["pre_tool_call"](tool_name="clarify", args={"question": "继续吗?"})
        argv = self.herdr.wait_for(state_call("blocked"))
        self.assertEqual(argv[argv.index("--message") + 1], "继续吗?")
        self.ctx.hooks["post_tool_call"](tool_name="clarify")
        self.assertIsNotNone(self.herdr.wait_for(state_call("working")))
        # An ordinary tool must not disturb the state.
        before = len(self.herdr.calls())
        self.ctx.hooks["pre_tool_call"](tool_name="read_file", args={"path": "x"})
        time.sleep(0.3)
        self.assertEqual(len(self.herdr.calls()), before)

    def test_bad_session_id_is_dropped(self) -> None:
        self.ctx.hooks["pre_llm_call"](session_id="bad id'; rm -rf /", platform="cli")
        argv = self.herdr.wait_for(state_call("working"))
        self.assertNotIn("--", argv)
        self.assertNotIn("--agent-session-id", argv)

    def test_non_interactive_surface_does_not_report(self) -> None:
        self.herdr.wait_for(state_call("idle"))
        before = len(self.herdr.calls())
        self.ctx.hooks["pre_llm_call"](session_id="20261004_101010_aaa111", platform="telegram")
        time.sleep(0.3)
        self.assertEqual(len(self.herdr.calls()), before)

    def test_finalize_releases_the_pane(self) -> None:
        self.herdr.wait_for(state_call("idle"))
        self.ctx.hooks["on_session_finalize"](session_id="20261004_101010_aaa111", reason="exit")
        argv = self.herdr.wait_for(release_call)
        self.assertIsNotNone(argv, "pane was never released")
        self.assertEqual(argv[argv.index("--source") + 1], "hermes:pane-state")

    def test_only_interactive_launches_claim_a_pane(self) -> None:
        interactive = self.mod._interactive_launch
        self.assertTrue(interactive(["hermes"]))
        self.assertTrue(interactive(["hermes", "--tui", "--resume", "latest"]))
        self.assertTrue(interactive(["hermes", "-c"]))
        self.assertTrue(interactive(["hermes", "chat"]))
        self.assertFalse(interactive(["hermes", "sessions", "list"]))
        self.assertFalse(interactive(["hermes", "cron", "list"]))

    def test_no_report_overtakes_a_release(self) -> None:
        session = "20261004_101010_aaa111"
        for _ in range(2):
            self.ctx.hooks["pre_llm_call"](session_id=session, platform="cli")
            self.ctx.hooks["post_llm_call"](session_id=session, platform="cli")
        self.ctx.hooks["on_session_finalize"]()
        self.assertIsNotNone(self.herdr.wait_for(release_call))
        time.sleep(0.3)
        calls = self.herdr.calls()
        seqs = [int(a[a.index("--seq") + 1]) for a in calls]
        # Reports coalesce by design (newest state wins), so the count varies -
        # but every seq must be unique and rising, and the release must be the
        # last thing Herdr hears.
        self.assertGreaterEqual(len(seqs), 2)
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        self.assertTrue(release_call(calls[-1]), calls[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
