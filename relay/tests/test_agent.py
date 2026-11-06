#!/usr/bin/env python3
"""Agent mode: tool loop, approvals across connections, and the tools themselves.

    python3 relay/tests/test_agent.py
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_relay import Client, free_port  # noqa: E402

spec = importlib.util.spec_from_file_location("lllm_relay", HERE.parent / "lllm_relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class AgentLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.mock = subprocess.Popen([sys.executable, str(HERE / "mock_ollama.py"), str(cls.port)])
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.mock.terminate()
        cls.mock.wait()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name) / "work"
        (self.work / "sub").mkdir(parents=True)
        key = Path(self.tmp.name) / "api.key"
        key.write_text("test-key")
        cfg = {
            "default_provider": "remote",
            "providers": {"remote": {"type": "openai", "base_url": f"http://127.0.0.1:{self.port}/v1",
                                     "api_key_file": str(key), "default_model": "vendor/model-a"}},
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

    def session(self, mode):
        s = self.client.call("new_session")["data"]["session"]
        self.assertTrue(s["agent"])
        self.assertEqual(s["cwd"], str(self.work))
        return self.client.call("update_session", session=s["id"], permission_mode=mode)["data"]["session"]

    def read_until(self, client, rid, kind):
        while True:
            ev = client.read()
            if ev.get("id") == rid and ev["type"] in (kind, "end", "error"):
                return ev

    def wait_pending(self, client, sid):
        for _ in range(50):
            s = client.call("get_session", session=sid)["data"]["session"]
            if s.get("pending_approval"):
                return s["pending_approval"]
            time.sleep(0.1)
        self.fail("no pending approval appeared")

    def test_auto_mode_runs_bash_and_keeps_cwd(self):
        s = self.session("auto")
        rid = self.client.send("chat", session=s["id"], text='tool: bash {"command": "cd sub && echo hi-there"}')
        events = self.client.events_until_end(rid)
        kinds = [e["type"] for e in events]
        call = next(e for e in events if e["type"] == "tool_call")
        self.assertEqual(call["arguments"], {"command": "cd sub && echo hi-there"})  # fragments joined
        self.assertFalse(call["needs_approval"])
        self.assertIn("hi-there", "".join(e["text"] for e in events if e["type"] == "tool_output"))
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertTrue(result["ok"])
        self.assertIn("[exit code 0]", result["output"])
        self.assertLess(kinds.index("tool_result"), kinds.index("turn"))
        self.assertEqual(events[-1]["status"], "done")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta").strip(), "result: hi-there")

        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(saved["messages"][1]["tool_calls"][0]["function"]["name"], "bash")
        self.assertEqual(saved["messages"][2]["tool_call_id"], "call_1")
        self.assertEqual(saved["cwd"], str((self.work / "sub").resolve()))

    def test_auto_read_needs_approval_for_writes_and_other_connection_approves(self):
        s = self.session("auto-read")
        target = self.work / "new.txt"
        args = json.dumps({"path": "new.txt", "content": "hello\n"})
        rid = self.client.send("chat", session=s["id"], text=f"tool: write_file {args}")
        call = self.read_until(self.client, rid, "tool_call")
        self.assertTrue(call["needs_approval"])
        self.assertFalse(target.exists())

        other = Client(self.env)  # e.g. the phone after reconnecting
        try:
            pending = self.wait_pending(other, s["id"])
            self.assertEqual(pending["name"], "write_file")
            self.assertEqual(other.call("approve", session=s["id"], call_id="bogus", decision="allow")["type"], "error")
            self.assertEqual(other.call("approve", session=s["id"], call_id=pending["call_id"],
                                        decision="allow")["type"], "result")
        finally:
            other.close()
        result = self.read_until(self.client, rid, "tool_result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["approval"], "allow")
        self.assertEqual(target.read_text(), "hello\n")
        self.assertEqual(self.client.events_until_end(rid)[-1]["status"], "done")

        # Reading is automatic in auto-read.
        rid = self.client.send("chat", session=s["id"], text='tool: read_file {"path": "new.txt"}')
        call = self.read_until(self.client, rid, "tool_call")
        self.assertFalse(call["needs_approval"])
        self.assertIn("hello", self.read_until(self.client, rid, "tool_result")["output"])
        self.client.events_until_end(rid)

    def test_deny_and_allow_for_session(self):
        s = self.session("ask")
        rid = self.client.send("chat", session=s["id"], text='tool: bash {"command": "touch should-not-exist"}')
        call = self.read_until(self.client, rid, "tool_call")
        self.wait_pending(self.client, s["id"])
        self.client.call("approve", session=s["id"], call_id=call["call_id"], decision="deny", note="not now")
        result = self.read_until(self.client, rid, "tool_result")
        self.assertFalse(result["ok"])
        self.assertIn("not now", result["output"])
        self.assertFalse((self.work / "should-not-exist").exists())
        self.client.events_until_end(rid)

        rid = self.client.send("chat", session=s["id"], text='tool: bash {"command": "echo one"}')
        call = self.read_until(self.client, rid, "tool_call")
        self.wait_pending(self.client, s["id"])
        self.client.call("approve", session=s["id"], call_id=call["call_id"], decision="allow_session")
        self.client.events_until_end(rid)
        rid = self.client.send("chat", session=s["id"], text='tool: bash {"command": "echo two"}')
        self.assertFalse(self.read_until(self.client, rid, "tool_call")["needs_approval"])
        self.client.events_until_end(rid)

    def test_stop_from_another_connection_while_waiting_for_approval(self):
        s = self.session("ask")
        rid = self.client.send("chat", session=s["id"], text='tool: bash {"command": "echo never"}')
        self.read_until(self.client, rid, "tool_call")
        other = Client(self.env)
        try:
            self.wait_pending(other, s["id"])
            self.assertTrue(other.call("cancel", session=s["id"])["data"]["cancelled"])
        finally:
            other.close()
        end = self.client.events_until_end(rid)[-1]
        self.assertEqual(end["status"], "cancelled")
        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertNotIn("pending_approval", saved)
        self.assertIn("stopped", saved["messages"][-1]["content"])
        # The conversation stays valid for the next message.
        end = self.client.events_until_end(self.client.send("chat", session=s["id"], text="hello"))[-1]
        self.assertEqual(end["status"], "done")

    def test_chat_only_session_sends_no_tools(self):
        s = self.client.call("new_session", agent=False)["data"]["session"]
        events = self.client.events_until_end(self.client.send("chat", session=s["id"], text='tool: bash {"command": "echo x"}'))
        self.assertNotIn("tool_call", [e["type"] for e in events])

    def test_history_fills_in_results_for_interrupted_calls(self):
        engine = relay.Engine(relay.load_config(Path(self.env["LLLM_CONFIG"])))
        session = {"messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
        ], "agent": True}
        msgs = engine._history(session, 3)
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual({m["tool_call_id"] for m in msgs if m["role"] == "tool"}, {"a", "b"})
        self.assertIsNone(msgs[2]["content"])


class ToolRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tools = relay.ToolRunner(str(self.dir))

    def tearDown(self):
        self.tmp.cleanup()

    def run_ok(self, name, **args):
        ok, out = self.tools.run(name, args)
        self.assertTrue(ok, out)
        return out

    def run_fail(self, name, **args):
        ok, out = self.tools.run(name, args)
        self.assertFalse(ok, out)
        return out

    def test_write_read_edit(self):
        self.assertIn("Created", self.run_ok("write_file", path="a/b.txt", content="one\ntwo\ntwo\n"))
        self.assertIn("     2\ttwo", self.run_ok("read_file", path="a/b.txt"))
        self.assertIn("appears 2 times", self.run_fail("edit_file", path="a/b.txt", old_string="two", new_string="2"))
        self.assertIn("not found", self.run_fail("edit_file", path="a/b.txt", old_string="zzz", new_string="2"))
        self.run_ok("edit_file", path="a/b.txt", old_string="one", new_string="1")
        self.run_ok("edit_file", path="a/b.txt", old_string="two", new_string="2", replace_all=True)
        self.assertEqual((self.dir / "a/b.txt").read_text(), "1\n2\n2\n")
        self.assertIn("1 more lines", self.run_ok("read_file", path="a/b.txt", limit=2))

    def test_list_and_grep(self):
        (self.dir / "src").mkdir()
        (self.dir / "src/x.py").write_text("def f():\n    return 42\n")
        (self.dir / "notes.md").write_text("42 is the answer\n")
        (self.dir / "node_modules").mkdir()
        (self.dir / "node_modules/y.py").write_text("42\n")
        listing = self.run_ok("list_dir")
        self.assertIn("src/", listing)
        self.assertIn("notes.md", listing)
        hits = self.run_ok("grep", pattern=r"\b42\b")
        self.assertIn("src/x.py:2:", hits)
        self.assertIn("notes.md:1:", hits)
        self.assertNotIn("node_modules", hits)
        self.assertNotIn("notes.md", self.run_ok("grep", pattern="42", glob="*.py"))
        self.assertIn("invalid regular expression", self.run_fail("grep", pattern="("))

    def test_bash_cwd_exit_code_and_timeout(self):
        (self.dir / "sub").mkdir()
        out = self.run_ok("bash", command="cd sub; echo out; echo err >&2; exit 3")
        self.assertIn("out", out)
        self.assertIn("err", out)
        self.assertTrue(out.endswith("[exit code 3]"))
        self.assertEqual(self.tools.cwd, str((self.dir / "sub").resolve()))
        self.assertIn("timed out after 1s", self.run_ok("bash", command="sleep 5", timeout=1))

    def test_bash_output_is_truncated(self):
        out = self.run_ok("bash", command="seq 1 100000")
        self.assertIn("characters omitted", out)
        self.assertIn("100000", out)  # the tail is kept

    def test_stop_kills_command(self):
        stopped = relay.ToolRunner(str(self.dir), stop=lambda: True)
        ok, out = stopped.run("bash", {"command": "sleep 10"})
        self.assertIn("cancelled", out)

    def test_bad_calls(self):
        self.assertIn("Unknown tool", self.run_fail("rm_rf", path="/"))
        self.assertIn("Bad arguments", self.run_fail("read_file", nope=1))
        self.assertIn("No such file", self.run_fail("read_file", path="missing.txt"))

    def test_confine(self):
        confined = relay.ToolRunner(str(self.dir), confine=str(self.dir))
        ok, out = confined.run("read_file", {"path": "/etc/hostname"})
        self.assertFalse(ok)
        self.assertIn("outside the allowed folder", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
