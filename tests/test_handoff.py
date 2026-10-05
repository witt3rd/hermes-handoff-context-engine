"""Regression tests for the handoff engine's trigger path.

Runs with plain ``python -m unittest`` (stdlib only). When hermes-agent is not
importable a minimal ``agent.context_engine`` stub stands in for the ABC; with
hermes-agent on PYTHONPATH the real base class is used.

The live failure these pin (Forge, Sep-Oct 2026): gateway *session hygiene*
fires at a hardcoded 85% of the window, BEFORE the agent turn, on a throwaway
AIAgent whose engine copy has never been consulted via should_compress(). It
calls compress() directly. With phase 'normal' the engine took the lossy
safety truncation every time, so the soft trigger (also 0.85, but only checked
at turn start) never got a turn: 37 hygiene truncations, 0 handoff requests.
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_HOME = tempfile.mkdtemp(prefix="handoff-test-home-")
os.environ["HERMES_HOME"] = _HOME  # never read a live profile's config.yaml

try:  # real ABC when hermes-agent is importable
    import agent.context_engine  # noqa: F401
except Exception:
    _pkg = types.ModuleType("agent")
    _pkg.__path__ = []
    _mod = types.ModuleType("agent.context_engine")

    class ContextEngine:  # minimal stand-in for the host ABC
        pass

    _mod.ContextEngine = ContextEngine
    sys.modules["agent"] = _pkg
    sys.modules["agent.context_engine"] = _mod


def _load_plugin():
    name = "handoff_plugin_under_test"
    spec = importlib.util.spec_from_file_location(
        name, REPO / "__init__.py", submodule_search_locations=[str(REPO)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return (mod, sys.modules[f"{name}.engine"], sys.modules[f"{name}.hook"],
            sys.modules[f"{name}.state"])


plugin, engine_mod, hook_mod, state_mod = _load_plugin()

CTX = 1_000_000


def _history(n=48):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"step {i}"})
        msgs.append({"role": "assistant", "content": f"did step {i} " + "x" * 200})
    return msgs


class HandoffTriggerTests(unittest.TestCase):
    def setUp(self):
        state_mod._STATE.clear()
        self.home = Path(tempfile.mkdtemp(prefix="handoff-test-profile-"))
        self.sid = f"sess-{self._testMethodName}"

    def _engine(self):
        """A fresh engine copy, as the host builds for every AIAgent."""
        e = engine_mod.HandoffContextEngine()
        e.on_session_start(self.sid, hermes_home=str(self.home))
        e.update_model(model="m", context_length=CTX)
        return e

    def _events(self):
        path = self.home / "handoffs" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _phase(self):
        return state_mod.HandoffStore().get_phase(self.sid)

    # -- the live failure ------------------------------------------------

    def test_out_of_turn_compaction_below_hard_requests_handoff_instead_of_truncating(self):
        """Gateway hygiene at 85%: must NOT chop; must request the handoff."""
        e = self._engine()  # throwaway hygiene agent: should_compress never called
        msgs = _history()
        out = e.compress(msgs, current_tokens=853_747)

        self.assertEqual(out, msgs, "below the hard net the transcript must survive")
        self.assertFalse(any("SAFETY TRUNCATION" in str(m.get("content")) for m in out))
        self.assertEqual(self._phase(), state_mod.PHASE_AUTHORING)

        # Same turn: the delivery hook now injects the instruction into the user turn.
        injected = hook_mod.pre_llm_call_handler(session_id=self.sid, user_message="hi")
        self.assertIsNotNone(injected)
        self.assertIn("finalize_handoff", injected["context"])

        kinds = [ev["event"] for ev in self._events()]
        self.assertIn("handoff_requested", kinds)
        self.assertIn("host_compaction_deferred", kinds)

    def test_repeated_out_of_turn_compaction_while_authoring_keeps_deferring(self):
        e = self._engine()
        msgs = _history()
        e.compress(msgs, current_tokens=853_747)
        again = self._engine().compress(msgs, current_tokens=870_000)
        self.assertEqual(again, msgs)
        self.assertEqual(self._phase(), state_mod.PHASE_AUTHORING)

    # -- the safety net must stay intact ---------------------------------

    def test_out_of_turn_compaction_at_hard_still_truncates(self):
        e = self._engine()
        out = e.compress(_history(), current_tokens=905_000)
        self.assertTrue(any("SAFETY TRUNCATION" in str(m.get("content")) for m in out))

    def test_in_turn_compaction_below_hard_still_truncates(self):
        """Error-recovery callers (413 / context overflow) run inside a turn,
        after the host consulted should_compress(). A no-op there is terminal
        ("cannot compress further"), so the engine must still shrink."""
        e = self._engine()
        e.should_compress(700_000)
        out = e.compress(_history(), current_tokens=700_000)
        self.assertTrue(any("SAFETY TRUNCATION" in str(m.get("content")) for m in out))

    def test_forced_manual_compress_still_truncates(self):
        e = self._engine()
        out = e.compress(_history(), current_tokens=853_747, force=True)
        self.assertTrue(any("SAFETY TRUNCATION" in str(m.get("content")) for m in out))

    # -- proven path ------------------------------------------------------

    def test_ready_handoff_swaps_from_out_of_turn_caller(self):
        e = self._engine()
        doc = self.home / "h.md"
        doc.write_text("## Objective\n- keep going\n")
        res = json.loads(e.handle_tool_call("finalize_handoff", {"confirm": True, "path": str(doc)}))
        self.assertEqual(res["status"], "ready")
        out = self._engine().compress(_history(), current_tokens=853_747)
        self.assertTrue(any(engine_mod.SWAP_MARKER in str(m.get("content")) for m in out))
        self.assertEqual(self._phase(), state_mod.PHASE_NORMAL)
        self.assertIn("handoff_swapped", [ev["event"] for ev in self._events()])

    # -- detection where the host actually looks -------------------------

    def test_should_compress_crossing_soft_requests_handoff(self):
        """should_compress() runs before EVERY API call (turn start and mid tool
        loop); the system_prompt hook only at turn start. Detect here too."""
        e = self._engine()
        self.assertFalse(e.should_compress(860_000))
        self.assertEqual(self._phase(), state_mod.PHASE_AUTHORING)

    def test_should_compress_below_soft_stays_normal(self):
        e = self._engine()
        self.assertFalse(e.should_compress(500_000))
        self.assertEqual(self._phase(), state_mod.PHASE_NORMAL)

    # -- loud failure ----------------------------------------------------

    def test_lossy_truncation_is_recorded_and_says_whether_a_handoff_was_requested(self):
        e = self._engine()
        e.should_compress(950_000)  # burst straight past soft AND hard
        out = e.compress(_history(), current_tokens=950_000)
        trunc = [ev for ev in self._events() if ev["event"] == "lossy_truncation"]
        self.assertEqual(len(trunc), 1)
        self.assertIn("handoff_requested", trunc[0])
        self.assertEqual(trunc[0]["tokens"], 950_000)
        note = next(m for m in out if "SAFETY TRUNCATION" in str(m.get("content")))
        self.assertNotIn("before a handoff document was completed", note["content"])


if __name__ == "__main__":
    unittest.main()
