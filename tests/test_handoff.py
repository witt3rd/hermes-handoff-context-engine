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

    def test_out_of_turn_count_triggered_compaction_below_soft_keeps_transcript_without_request(self):
        """Hygiene's message-count valve fires at any token level; a 30% session
        must neither be chopped nor told to hand off early."""
        e = self._engine()
        msgs = _history()
        out = e.compress(msgs, current_tokens=300_000)
        self.assertEqual(out, msgs)
        self.assertEqual(self._phase(), state_mod.PHASE_NORMAL)
        self.assertIsNone(hook_mod.pre_llm_call_handler(session_id=self.sid, user_message="hi"))

    def test_out_of_turn_compaction_with_unknown_size_still_truncates(self):
        e = self._engine()
        out = e.compress(_history())
        self.assertTrue(any("SAFETY TRUNCATION" in str(m.get("content")) for m in out))

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


class _FakeAgent:
    """What the system_prompt hook sees: an agent exposing its engine copy."""

    def __init__(self, engine):
        self.context_compressor = engine
        self._persist_disabled = False


def _big_tool_round(i, args_chars=0, result_chars=0, api_content_chars=0):
    """An assistant tool call + its result. The bulk can sit in fields other
    than ``content`` (tool_call arguments, the api_content sidecar) — the host
    sends and counts those, so the tail budget must too."""
    call = {"id": f"call_{i}", "type": "function",
            "function": {"name": "write_file", "arguments": "a" * args_chars}}
    asst = {"role": "assistant", "content": "", "tool_calls": [call]}
    res = {"role": "tool", "tool_call_id": f"call_{i}", "content": "r" * result_chars}
    if api_content_chars:
        asst["api_content"] = "z" * api_content_chars
    return [asst, res]


def _tokens(msgs):
    return sum(engine_mod._message_tokens(m) for m in msgs if m.get("role") != "system")


class BoundedTailTests(unittest.TestCase):
    """Forge, 2026-10-05: handoff 1 swapped 102 messages for 13 'recent' ones
    logged as ~8,000 tokens; minutes later that session was 16 messages and
    ~1.19M tokens. The tail budget counted only ``content``, so the retained
    tail restarted the session at or above soft and handoffs chained."""

    def setUp(self):
        state_mod._STATE.clear()
        self.home = Path(tempfile.mkdtemp(prefix="handoff-test-profile-"))
        self.sid = f"sess-{self._testMethodName}"
        self.e = engine_mod.HandoffContextEngine()
        self.e.on_session_start(self.sid, hermes_home=str(self.home))
        self.e.update_model(model="m", context_length=CTX)
        self.doc = self.home / "h.md"
        self.doc.write_text("## Objective\n- go\n")
        self.e.handle_tool_call("finalize_handoff", {"confirm": True, "path": str(self.doc)})

    def _swap(self, msgs):
        return self.e.compress(msgs, current_tokens=853_747)

    def _events(self, kind):
        path = self.home / "handoffs" / "events.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        return [r for r in rows if r["event"] == kind]

    def test_tail_bulk_outside_content_is_counted(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(8):  # tiny content, ~100k tokens of tool_call arguments each
            msgs += _big_tool_round(i, args_chars=400_000)
        out = self._swap(msgs)
        self.assertLessEqual(_tokens(out), self.e.keep_tokens + 5_000,
                             "retained tail must be bounded by its REAL size")

    def test_tail_sidecar_content_is_counted(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(8):
            msgs += _big_tool_round(i, api_content_chars=400_000)
        out = self._swap(msgs)
        self.assertLessEqual(_tokens(out), self.e.keep_tokens + 5_000)

    def test_single_oversized_message_is_not_force_kept(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "x" * 4_000_000}]
        out = self._swap(msgs)
        self.assertLessEqual(_tokens(out), self.e.keep_tokens + 5_000)
        self.assertTrue(any(engine_mod.SWAP_MARKER in str(m.get("content")) for m in out))

    def test_tail_never_starts_with_orphaned_tool_result(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        msgs += _big_tool_round(0, args_chars=200_000)  # call too big to keep...
        msgs += _big_tool_round(1)                      # ...its sibling rounds fit
        out = self._swap(msgs)
        body = [m for m in out if m.get("role") != "system"]
        self.assertNotEqual(body[0].get("role"), "tool")
        ids = {c["id"] for m in body for c in (m.get("tool_calls") or [])}
        for m in body:
            if m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], ids)

    def test_small_tail_is_still_retained(self):
        out = self._swap(_history(6))
        self.assertGreater(len([m for m in out if m.get("role") != "system"]), 2)

    def test_swap_event_reports_real_tail_size(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(8):
            msgs += _big_tool_round(i, args_chars=400_000)
        self._swap(msgs)
        ev = self._events("handoff_swapped")[0]
        self.assertIn("tail_tokens", ev)
        self.assertLessEqual(ev["tail_tokens"], self.e.keep_tokens)
        self.assertGreater(ev["tail_dropped"], 0)

    def test_safety_truncation_actually_shrinks_huge_tail(self):
        """16 messages -> 17 at ~1.19M tokens is what Forge logged: the net must
        bound the tail by size, not just count."""
        e = engine_mod.HandoffContextEngine()
        e.on_session_start(self.sid + "-t", hermes_home=str(self.home))
        e.update_model(model="m", context_length=CTX)
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(8):
            msgs += _big_tool_round(i, args_chars=400_000)
        out = e.compress(msgs, current_tokens=1_186_051)
        self.assertLess(_tokens(out), CTX * 0.10 + 5_000)
        body = [m for m in out if m.get("role") != "system"]
        ids = {c["id"] for m in body for c in (m.get("tool_calls") or [])}
        for m in body:
            if m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], ids)


class InstructionTextTests(unittest.TestCase):
    """Forge saw 'about 6%' and 'about 62%' while the log said 85% and 86%: the
    text was refreshed from a rough estimate on a fresh engine copy and silently
    replaced the host's measured figure."""

    def setUp(self):
        state_mod._STATE.clear()
        self.home = Path(tempfile.mkdtemp(prefix="handoff-test-profile-"))
        self.sid = f"sess-{self._testMethodName}"

    def _engine(self):
        e = engine_mod.HandoffContextEngine()
        e.on_session_start(self.sid, hermes_home=str(self.home))
        e.update_model(model="m", context_length=CTX)
        return e

    def _deliver(self):
        return hook_mod.pre_llm_call_handler(session_id=self.sid, user_message="hi")["context"]

    def test_text_states_tokens_and_window_plainly(self):
        self._engine().should_compress(852_535)
        text = self._deliver()
        self.assertIn("852,535", text)
        self.assertIn("1,000,000", text)
        self.assertIn("85%", text)
        self.assertIn("context window", text)

    def test_fresh_engine_copy_cannot_lower_the_request_time_figure(self):
        self._engine().should_compress(852_535)
        fresh = self._engine()  # next turn's agent: no preflight number yet
        self.assertEqual(fresh.last_preflight_tokens, 0)
        tiny = [{"role": "user", "content": "hi"}]
        hook_mod.system_prompt_handler(_FakeAgent(fresh), self.sid, tiny)
        text = self._deliver()
        self.assertIn("852,535", text)
        self.assertNotRegex(text, r"~\d%\b")

    def test_live_preflight_may_raise_the_figure(self):
        self._engine().should_compress(852_535)
        fresh = self._engine()
        fresh.should_compress(880_000)
        hook_mod.system_prompt_handler(_FakeAgent(fresh), self.sid, [])
        self.assertIn("880,000", self._deliver())

    def test_estimate_only_request_is_labelled_as_an_estimate(self):
        e = self._engine()
        e.request_handoff(0.86, "host_compaction", 857_625)
        text = self._deliver()
        self.assertIn("857,625", text)
        self.assertIn("estimate", text.lower())

    def test_text_without_any_token_figure_does_not_invent_one(self):
        e = self._engine()
        e.request_handoff(0.9, "turn_start", 0)
        text = self._deliver()
        self.assertIn("90%", text)
        self.assertNotIn("0 tokens", text)


class SessionRotationTests(unittest.TestCase):
    """The host rotates session_id on every compaction and calls
    on_session_start(new, boundary_reason='compression', old_session_id=old).
    State keyed by session_id must follow it or the layered prior is lost."""

    def setUp(self):
        state_mod._STATE.clear()
        self.home = Path(tempfile.mkdtemp(prefix="handoff-test-profile-"))

    def test_layered_prior_and_swap_count_survive_session_rotation(self):
        e = engine_mod.HandoffContextEngine()
        e.on_session_start("old", hermes_home=str(self.home))
        e.update_model(model="m", context_length=CTX)
        doc = self.home / "h.md"
        doc.write_text("## Objective\n- PRIOR-MARKER\n")
        e.handle_tool_call("finalize_handoff", {"confirm": True, "path": str(doc)})
        e.compress(_history(4), current_tokens=853_747)
        e.on_session_start("new", hermes_home=str(self.home),
                           boundary_reason="compression", old_session_id="old")
        store = state_mod.HandoffStore()
        self.assertIn("PRIOR-MARKER", store.get_last_handoff("new") or "")
        self.assertEqual(store.get_swap_count("new"), 1)
        e.request_handoff(0.86, "should_compress", 860_000)
        text = hook_mod.pre_llm_call_handler(session_id="new", user_message="hi")["context"]
        self.assertIn("PRIOR-MARKER", text)

    def test_unrelated_new_session_starts_clean(self):
        e = engine_mod.HandoffContextEngine()
        e.on_session_start("a", hermes_home=str(self.home))
        state_mod.HandoffStore().set_last_handoff("a", "stale")
        e.on_session_start("b", hermes_home=str(self.home))
        self.assertIsNone(state_mod.HandoffStore().get_last_handoff("b"))

    def test_request_soon_after_a_swap_records_the_chain(self):
        e = engine_mod.HandoffContextEngine()
        e.on_session_start("old", hermes_home=str(self.home))
        e.update_model(model="m", context_length=CTX)
        doc = self.home / "h.md"
        doc.write_text("## Objective\n- x\n")
        e.handle_tool_call("finalize_handoff", {"confirm": True, "path": str(doc)})
        e.compress(_history(4), current_tokens=853_747)
        e.on_session_start("new", hermes_home=str(self.home),
                           boundary_reason="compression", old_session_id="old")
        e.request_handoff(0.86, "should_compress", 860_000)
        rows = [json.loads(x) for x in
                (self.home / "handoffs" / "events.jsonl").read_text().splitlines()]
        req = [r for r in rows if r["event"] == "handoff_requested"][-1]
        self.assertEqual(req["swaps_in_lineage"], 1)
        self.assertIn("seconds_since_swap", req)


if __name__ == "__main__":
    unittest.main()
