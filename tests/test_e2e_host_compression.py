"""End-to-end: the handoff engine inside a real hermes-agent compression path.

Runs the engine as ``AIAgent.context_compressor`` and drives the host's real
``AIAgent._compress_context`` (compression facade -> ``compress_context`` in
``agent.conversation_compression``: commit fence, progress timeout, anti-growth
guard, session commit) with a scripted fake model. Nothing about the host is
mocked except the model: no network, no summarizer call.

Needs hermes-agent importable (``PYTHONPATH=<checkout>``) and its runtime
dependencies; skipped otherwise. The stdlib-only unit tests live in
``test_handoff.py``.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_handoff as T  # noqa: E402  (also loads the plugin under test)

try:
    import agent.conversation_compression as host_cc
    import run_agent  # noqa: F401
    from hermes_state import SessionDB
    HOST = True
except Exception:  # hermes-agent (or a dependency) is absent
    HOST = False

CTX = 100_000
STALE = "STALE-REQUEST-do-not-replay"
LIVE = "LIVE-REQUEST-finish-the-migration"
HANDOFF = "## Objective\n- finish the migration\n## Decisions\n- AGENT-AUTHORED-MARKER\n"


def _tc(call_id, name, args="{}"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": args}}]}


def _tokens(msgs):
    return T.engine_mod._request_tokens(msgs)


class ScriptedModel:
    """A fake model with a script, not a heuristic. Each call looks at what the
    host would send (messages plus the pre_llm_call context) and answers with
    the next scripted action: a read tool call that returns a big result, or,
    once the delivered instruction asks for a handoff, a write plus a
    ``finalize_handoff`` call."""

    def __init__(self, home, engine):
        self.home, self.engine = home, engine
        self.calls = 0

    def step(self, injected):
        self.calls += 1
        if injected and "finalize_handoff" in injected["context"]:
            doc = self.home / "authored-handoff.md"
            doc.write_text(HANDOFF)  # the agent writes it with its own tools
            return _tc(f"fin{self.calls}", "finalize_handoff",
                       json.dumps({"confirm": True, "path": str(doc)}))
        return _tc(f"read{self.calls}", "read_file")

    def tool_result(self, call):
        name = call["tool_calls"][0]["function"]["name"]
        if name == "finalize_handoff":
            args = json.loads(call["tool_calls"][0]["function"]["arguments"])
            return self.engine.handle_tool_call(name, args)
        return "file body " + "z" * 12_000  # ~3k tokens per read


@unittest.skipUnless(HOST, "hermes-agent not importable (set PYTHONPATH)")
class HostCompressionE2E(unittest.TestCase):
    def setUp(self):
        T.state_mod._STATE.clear()
        self.home = Path(tempfile.mkdtemp(prefix="handoff-e2e-"))
        self.sid = f"e2e-{self._testMethodName}"
        self.db = SessionDB(db_path=self.home / "state.db")
        self.db.create_session(session_id=self.sid, source="cli", model="m")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            self.agent = AIAgent(
                api_key="test-key", base_url="https://openrouter.ai/api/v1",
                model="test/model", quiet_mode=True, session_db=self.db,
                session_id=self.sid, skip_context_files=True, skip_memory=True)
        self.engine = T.engine_mod.HandoffContextEngine()
        self.engine.on_session_start(self.sid, hermes_home=str(self.home))
        self.engine.update_model(model="m", context_length=CTX)
        self.agent.context_compressor = self.engine
        self.agent._compression_feasibility_checked = True
        self.store = T.state_mod.HandoffStore()

    def _events(self):
        path = self.home / "handoffs" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    def _kinds(self):
        return [e["event"] for e in self._events()]

    def _compress(self, msgs):
        return self.agent._compress_context(
            list(msgs), "sys", approx_tokens=_tokens(msgs))

    def _drive_to_finalize(self):
        """Run turns until the scripted agent has called finalize_handoff.
        Returns the transcript ending on that call's in-flight tool result."""
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": STALE},
                {"role": "assistant", "content": "done with that."},
                {"role": "user", "content": LIVE}]
        model = ScriptedModel(self.home, self.engine)
        for _ in range(60):
            used = _tokens(msgs)
            self.engine.select_context(msgs)
            self.engine.update_from_response({"prompt_tokens": used})
            self.assertFalse(self.engine.should_compress(used) and
                             self.store.get_phase(self.sid) != T.state_mod.PHASE_READY,
                             "hard net must not fire before the handoff lands")
            injected = T.hook_mod.pre_llm_call_handler(
                session_id=self.sid, user_message=LIVE)
            call = model.step(injected)
            msgs.append(call)
            msgs.append({"role": "tool", "tool_call_id": call["tool_calls"][0]["id"],
                         "content": model.tool_result(call)})
            if call["tool_calls"][0]["function"]["name"] == "finalize_handoff":
                return msgs
        self.fail("scripted model never reached finalize_handoff")

    def test_long_session_handoff_replaces_transcript(self):
        msgs = self._drive_to_finalize()
        self.assertGreater(_tokens(msgs), CTX * 0.85 - 10_000)  # past soft
        self.assertEqual(self.store.get_phase(self.sid), T.state_mod.PHASE_READY)
        self.assertTrue(self.engine.should_compress(_tokens(msgs)))

        out, _ = self._compress(msgs)

        text = json.dumps(out)
        self.assertIn("AGENT-AUTHORED-MARKER", text, "the handoff is the seed")
        self.assertNotIn(STALE, text)
        self.assertLess(_tokens(out), _tokens(msgs) * 0.5)
        self.assertEqual(out[0]["role"], "system")
        # In-flight tool result survives, last, with its call still paired.
        self.assertEqual(out[-1]["role"], "tool")
        self.assertIn("Handoff accepted", out[-1]["content"])
        ids = {tc["id"] for m in out for tc in (m.get("tool_calls") or [])}
        self.assertIn(out[-1]["tool_call_id"], ids)
        # No stale user request replayed after the tool result; no user turn follows
        # the in-flight call.
        last_user = max(i for i, m in enumerate(out) if m["role"] == "user")
        self.assertLess(last_user, len(out) - 2)
        users = [m["content"] for m in out if m["role"] == "user"]
        self.assertEqual(sum(1 for u in users if u == LIVE), 1)  # once, in place
        self.assertTrue(all(u == LIVE or "AGENT-AUTHORED-MARKER" in u for u in users))
        # Logged, committed, and phase back to normal.
        kinds = self._kinds()
        for k in ("handoff_requested", "handoff_finalized", "handoff_swapped"):
            self.assertIn(k, kinds)
        self.assertNotIn("lossy_truncation", kinds)
        self.assertEqual(self.store.get_phase(self.sid), T.state_mod.PHASE_NORMAL)
        self.assertIsNone(self.store.get_pending_swap(self.sid))

    def test_refused_swap_rearms_the_handoff(self):
        """Host anti-growth guard: a handoff larger than the transcript is
        refused. The authored document must survive for the next attempt."""
        msgs = T._history(3) + [{"role": "user", "content": LIVE},
                                _tc("c1", "read_file"),
                                {"role": "tool", "tool_call_id": "c1", "content": "RESULT"}]
        doc = self.home / "big.md"
        doc.write_text("## Objective\n" + "- go on\n" * 5000)
        self.engine.handle_tool_call("finalize_handoff", {"confirm": True, "path": str(doc)})

        out, _ = self._compress(msgs)
        self.assertEqual(out, msgs, "refused: transcript unchanged")
        self.assertIsNotNone(self.store.get_pending_swap(self.sid))

        # Asked again in the unchanged session: re-armed, not re-requested.
        self.assertFalse(self.engine.request_handoff(0.9, "should_compress", 90_000))
        self.assertIn("swap_restored", self._kinds())
        self.assertEqual(self.store.get_phase(self.sid), T.state_mod.PHASE_READY)
        self.assertEqual(self.store.get_swap_count(self.sid), 0)

    def test_timed_out_swap_rearms_then_commits(self):
        msgs = self._drive_to_finalize()
        real = self.engine.compress

        def late(*a, **kw):
            result = real(*a, **kw)
            time.sleep(2.5)  # past the host's progress timeout
            return result

        with patch.object(self.engine, "compress", late), \
                patch.object(host_cc, "resolve_context_compression_timeouts",
                             lambda: (0.4, 0.8)):
            out, _ = self._compress(msgs)
        self.assertNotIn("AGENT-AUTHORED-MARKER", json.dumps(out))
        self.assertEqual(len(out), len(msgs), "timed out: transcript unchanged")
        time.sleep(3)  # let the late worker finish and be fenced off

        # The host never committed; the next ask re-arms and swaps for real.
        out, _ = self._compress(msgs)
        self.assertIn("AGENT-AUTHORED-MARKER", json.dumps(out))
        self.assertIn("swap_restored", self._kinds())
        self.assertEqual(out[-1]["role"], "tool")
        self.assertNotIn("lossy_truncation", self._kinds())


if __name__ == "__main__":
    unittest.main()
