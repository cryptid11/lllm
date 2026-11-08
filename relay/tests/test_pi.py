#!/usr/bin/env python3
"""The Pi engine: the relay driving `pi --mode rpc` (here relay/tests/mock_pi.py).

    python3 relay/tests/test_pi.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_relay import Client  # noqa: E402


class PiEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        self.pi_dir = Path(self.tmp.name) / "pi-sessions"
        key = Path(self.tmp.name) / "api.key"
        key.write_text("sk-test\n")
        cfg = {
            "engine": "pi",
            "pi": {"command": [sys.executable, str(HERE / "mock_pi.py")], "session_dir": str(self.pi_dir),
                   "abort_grace": 5},
            "default_provider": "or",
            "providers": {"or": {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "api_key_file": str(key),
                                 "pi_provider": "openrouter", "default_model": "vendor/model-a"}},
            "sessions_dir": f"{self.tmp.name}/sessions",
            "partial_save_interval": 0.2,
            "agent": {"workdir": str(self.work)},
        }
        cfg_path = Path(self.tmp.name) / "config.json"
        cfg_path.write_text(json.dumps(cfg))
        self.env = {**os.environ, "LLLM_CONFIG": str(cfg_path)}
        self.client = Client(self.env)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def session(self, mode="auto-read"):
        s = self.client.call("new_session")["data"]["session"]
        self.assertEqual(s["engine"], "pi")
        return self.client.call("update_session", session=s["id"], permission_mode=mode)["data"]["session"]

    def chat(self, sid, text):
        return self.client.events_until_end(self.client.send("chat", session=sid, text=text))

    def test_plain_reply_and_resume(self):
        s = self.session()
        events = self.chat(s["id"], "hello")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta").strip(), "pi-echo: hello")
        end = events[-1]
        self.assertEqual(end["status"], "done")
        self.assertEqual(end["message"]["stats"]["eval_count"], 5)
        self.assertIn("cost_usd", end["message"]["stats"])
        # The next message runs a new Pi process on the same Pi session, with model and key passed in.
        reply = "".join(e["text"] for e in self.chat(s["id"], "history") if e["type"] == "delta")
        self.assertIn("previous=1", reply)
        self.assertIn("model=vendor/model-a", reply)
        self.assertIn("provider=openrouter", reply)
        self.assertIn("key=sk-test", reply)
        self.assertIn("extension=True", reply)
        self.assertIn("tools=True", reply)

    def test_tool_flow_in_auto_mode(self):
        s = self.session("auto")
        events = self.chat(s["id"], 'tool: bash {"command": "ls"}')
        kinds = [e["type"] for e in events]
        call = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual((call["name"], call["arguments"], call["needs_approval"]), ("bash", {"command": "ls"}, False))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "tool_output"), "line1\nline2\n")
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertLess(kinds.index("tool_result"), kinds.index("turn"))
        self.assertIn("tool said: ran bash", "".join(e["text"] for e in events if e["type"] == "delta"))
        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(saved["messages"][1]["thinking"], "need a tool. ")
        self.assertEqual(json.loads(saved["messages"][1]["tool_calls"][0]["function"]["arguments"]), {"command": "ls"})
        self.assertEqual(saved["messages"][2]["approval"], "auto")

    def test_read_is_automatic_but_edit_asks_and_other_connection_denies(self):
        s = self.session("auto-read")
        read = next(e for e in self.chat(s["id"], 'tool: read {"path": "x"}') if e["type"] == "tool_call")
        self.assertFalse(read["needs_approval"])

        rid = self.client.send("chat", session=s["id"], text='tool: edit {"path": "x", "edits": []}')
        while True:
            ev = self.client.read()
            if ev.get("type") == "tool_call":
                break
        self.assertTrue(ev["needs_approval"])
        other = Client(self.env)
        try:
            for _ in range(50):
                pending = other.call("get_session", session=s["id"])["data"]["session"].get("pending_approval")
                if pending:
                    break
                time.sleep(0.1)
            other.call("approve", session=s["id"], call_id=pending["call_id"], decision="deny", note="not that file")
        finally:
            other.close()
        events = self.client.events_until_end(rid)
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertFalse(result["ok"])
        self.assertEqual(result["approval"], "deny")
        self.assertIn("not that file", result["output"])

    def test_stop_aborts_pi(self):
        s = self.session()
        rid = self.client.send("chat", session=s["id"], text="slow please")
        while self.client.read().get("type") != "delta":
            pass
        self.assertTrue(self.client.call("cancel", session=s["id"])["data"]["cancelled"])
        end = self.client.events_until_end(rid)[-1]
        self.assertEqual(end["status"], "cancelled")
        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertFalse(saved["generating"])
        self.assertEqual(saved["messages"][-1]["status"], "cancelled")

    def test_pi_crash_is_reported(self):
        s = self.session()
        end = self.chat(s["id"], "die")[-1]
        self.assertEqual(end["status"], "error")
        self.assertIn("simulated crash", end["error"])
        self.assertNotIn("partial", end["message"])

    def test_cwd_is_fixed_after_first_message_and_delete_cleans_up(self):
        s = self.session()
        (self.work / "elsewhere").mkdir()
        self.assertEqual(self.client.call("update_session", session=s["id"], cwd="elsewhere")["type"], "result")
        self.chat(s["id"], "hello")
        self.assertIn("keeps the folder", self.client.call("update_session", session=s["id"], cwd="..")["error"])
        self.assertTrue(list(self.pi_dir.glob(f"*{s['id']}*")))
        self.client.call("delete_session", session=s["id"])
        self.assertFalse(list(self.pi_dir.glob(f"*{s['id']}*")))

    def test_chat_only_session_disables_pi_tools(self):
        s = self.client.call("new_session", agent=False)["data"]["session"]
        reply = "".join(e["text"] for e in self.chat(s["id"], "history") if e["type"] == "delta")
        self.assertIn("tools=False", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
