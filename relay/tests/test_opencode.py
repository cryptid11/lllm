#!/usr/bin/env python3
"""The OpenCode engine: the relay in front of an OpenCode service (here mock_opencode.py).

    python3 relay/tests/test_opencode.py
"""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_relay import Client, free_port  # noqa: E402


class StreamClient(Client):
    """Keeps stream events that arrive while waiting for the reply to a request."""

    def __init__(self, env):
        self.stash = []
        super().__init__(env)

    def read(self, timeout=10):
        return self.stash.pop(0) if self.stash else super().read(timeout)

    def call(self, op, **fields):
        rid = self.send(op, **fields)
        while True:
            msg = Client.read(self)
            if msg.get("id") == rid and msg["type"] in ("result", "error"):
                return msg
            self.stash.append(msg)


class OpenCodeEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.mock = subprocess.Popen([sys.executable, str(HERE / "mock_opencode.py"), str(cls.port), "test-pass"])
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
        self.root = Path(self.tmp.name)
        (self.root / "work").mkdir()
        (self.root / "service.json").write_text(json.dumps({"password": "test-pass"}))
        self.env = self.write_config()
        self.client = StreamClient(self.env)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def write_config(self, **opencode):
        cfg = {
            "engine": "opencode",
            "opencode": {"url": f"http://127.0.0.1:{self.port}", "password_file": str(self.root / "service.json"),
                         "workdir": str(self.root / "work"), **opencode},
            "default_provider": "local",
            "providers": {"local": {"type": "ollama", "base_url": "http://127.0.0.1:9", "default_model": "m"}},
            "sessions_dir": str(self.root / "sessions"),
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(cfg))
        return {**os.environ, "LLLM_CONFIG": str(path)}

    def session(self, mode=None):
        s = self.client.call("new_session")["data"]["session"]
        if mode:
            s = self.client.call("update_session", session=s["id"], permission_mode=mode)["data"]["session"]
        return s

    def until(self, client, rid, kind):
        while True:
            ev = client.read()
            if ev.get("id") == rid and ev["type"] in (kind, "end", "error"):
                return ev

    def mock_state(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/test/state") as r:
            return json.load(r)

    def test_new_sessions_ask_before_acting_and_list_with_local_ones(self):
        s = self.session()
        self.assertEqual((s["engine"], s["provider"], s["permission_mode"]), ("opencode", "opencode", "auto-read"))
        self.assertEqual(s["cwd"], str(self.root / "work"))
        self.assertEqual(s["model"], "opencode/default-model")
        local = self.client.call("new_session", provider="local")["data"]["session"]
        self.assertEqual(local["engine"], "builtin")
        listed = {x["id"]: x for x in self.client.call("list_sessions")["data"]["sessions"]}
        self.assertEqual(listed[s["id"]]["preview"], str(self.root / "work"))
        self.assertIn(local["id"], listed)
        self.assertEqual(self.client.call("list_models")["data"]["models"], ["opencode/default-model", "x/chat-2"])

    def test_engine_choice_and_folder_listing(self):
        self.assertEqual(self.client.hello["engines"], ["opencode", "builtin"])
        chosen = self.client.call("new_session", engine="builtin")["data"]["session"]
        self.assertEqual(chosen["engine"], "builtin")
        self.assertFalse(chosen["id"].startswith("ses"))
        self.assertEqual(self.client.call("new_session", engine="opencode")["data"]["session"]["engine"], "opencode")
        self.assertIn("engine must be", self.client.call("new_session", engine="nope")["error"])
        (self.root / "work" / "b").mkdir()
        (self.root / "work" / "A").mkdir()
        (self.root / "work" / ".hidden").mkdir()
        (self.root / "work" / "file.txt").write_text("x")
        data = self.client.call("list_dirs", path=str(self.root / "work"))["data"]
        self.assertEqual(data, {"path": str(self.root / "work"), "parent": str(self.root), "dirs": ["A", "b"]})
        self.assertIn("cannot open", self.client.call("list_dirs", path=str(self.root / "nope"))["error"])

    def test_chat_streams_and_session_is_read_back(self):
        s = self.session()
        events = self.client.events_until_end(self.client.send("chat", session=s["id"], text="hello there"))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta").strip(), "oc-echo: hello there")
        self.assertEqual(events[-1]["status"], "done")
        self.assertEqual(events[-1]["message"]["stats"]["eval_count"], 5)
        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertFalse(saved["generating"])
        self.assertEqual([m["role"] for m in saved["messages"]], ["user", "assistant"])

    def test_read_runs_alone_but_write_waits_for_any_connection(self):
        s = self.session()
        events = self.client.events_until_end(
            self.client.send("chat", session=s["id"], text='tool: read {"path": "a.txt"}'))
        self.assertEqual([e["needs_approval"] for e in events if e["type"] == "tool_call"], [False])

        rid = self.client.send("chat", session=s["id"], text='tool: write {"path": "b.txt", "content": "x"}')
        while True:
            call = self.until(self.client, rid, "tool_call")
            if call.get("needs_approval"):
                break
        self.assertEqual(call["arguments"]["path"], "b.txt")
        self.assertEqual(call["save"], ["*"])
        other = StreamClient(self.env)
        try:
            pending = other.call("get_session", session=s["id"])["data"]["session"]["pending_approval"]
            self.assertEqual((pending["call_id"], pending["name"]), (call["call_id"], "write"))
            self.assertEqual(other.call("approve", session=s["id"], call_id="nope", decision="allow")["type"], "error")
            other.call("approve", session=s["id"], call_id=call["call_id"], decision="allow_session")
        finally:
            other.close()
        events = self.client.events_until_end(rid)
        self.assertIn("approval_resolved", [e["type"] for e in events])
        result = next(e for e in events if e["type"] == "tool_result")
        self.assertEqual((result["ok"], result["approval"]), (True, "allow_session"))
        self.assertEqual(self.mock_state()["replies"][-1], {"decision": "always"})

    def test_deny_sends_the_note(self):
        s = self.session("ask")
        rid = self.client.send("chat", session=s["id"], text='tool: shell {"command": "rm x"}')
        while True:
            call = self.until(self.client, rid, "tool_call")
            if call.get("needs_approval"):
                break
        self.client.call("approve", session=s["id"], call_id=call["call_id"], decision="deny", note="keep it")
        result = self.until(self.client, rid, "tool_result")
        self.assertEqual((result["ok"], result["approval"], result["output"]), (False, "deny", "keep it"))
        self.client.events_until_end(rid)
        self.assertEqual(self.mock_state()["replies"][-1], {"decision": "reject", "message": "keep it"})

    def test_shell_output_streams_live(self):
        s = self.session("auto")
        events = self.client.events_until_end(
            self.client.send("chat", session=s["id"], text='tool: shell {"command": "ticks"}'))
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "tool_output"), "tick 1\n")

    def test_subagent_approval_reaches_the_parent_session(self):
        s = self.session("ask")
        rid = self.client.send("chat", session=s["id"], text='sub: shell {"command": "ls"}')
        call = self.until(self.client, rid, "tool_call")
        while not call.get("needs_approval"):
            call = self.until(self.client, rid, "tool_call")
        self.client.call("approve", session=s["id"], call_id=call["call_id"], decision="allow")
        self.assertEqual(self.client.events_until_end(rid)[-1]["status"], "done")

    def test_question_form(self):
        s = self.session()
        rid = self.client.send("chat", session=s["id"], text="question")
        form = self.until(self.client, rid, "form")["form"]
        pending = self.client.call("get_session", session=s["id"])["data"]["session"]["pending_form"]
        self.assertEqual(pending["id"], form["id"])
        self.client.call("form_reply", session=form["sessionID"], form_id=form["id"], answer={"q0": "Blue"})
        events = self.client.events_until_end(rid)
        self.assertIn("form_resolved", [e["type"] for e in events])
        self.assertIn("Blue", "".join(e["text"] for e in events if e["type"] == "delta"))

    def test_stop_and_failure_are_reported(self):
        s = self.session()
        rid = self.client.send("chat", session=s["id"], text="slow")
        self.until(self.client, rid, "delta")
        self.assertTrue(self.client.call("cancel", session=s["id"])["data"]["cancelled"])
        self.assertEqual(self.client.events_until_end(rid)[-1]["status"], "cancelled")
        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertEqual(saved["messages"][-1]["status"], "cancelled")
        end = self.client.events_until_end(self.client.send("chat", session=s["id"], text="boom"))[-1]
        self.assertEqual((end["status"], end["error"]), ("error", "simulated failure"))

    def test_lost_event_stream_still_finishes(self):
        s = self.session()
        end = self.client.events_until_end(self.client.send("chat", session=s["id"], text="drop"))[-1]
        self.assertEqual(end["status"], "done")
        self.assertIn("finished unseen", end["message"]["content"])

    def test_disconnect_leaves_the_run_going_and_the_relay_exits(self):
        s = self.session()
        rid = self.client.send("chat", session=s["id"], text="slow")
        self.until(self.client, rid, "delta")
        started = time.time()
        self.client.close()
        self.assertLess(time.time() - started, 5)
        self.client = StreamClient(self.env)
        session = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertTrue(session["generating"])
        self.assertTrue(session["messages"][-1]["partial"])
        self.client.call("cancel", session=s["id"])

    def test_session_settings(self):
        s = self.session()
        sid = s["id"]
        update = lambda **f: self.client.call("update_session", session=sid, **f)  # noqa: E731
        self.assertEqual(update(title="  My   chat ")["data"]["session"]["title"], "My chat")
        self.assertEqual(update(model="x/chat-2")["data"]["session"]["model"], "x/chat-2")
        self.assertEqual(update(permission_mode="auto")["data"]["session"]["permission_mode"], "auto")
        (self.root / "work" / "sub").mkdir()
        self.assertEqual(update(cwd="sub")["data"]["session"]["cwd"], str(self.root / "work" / "sub"))
        self.assertIn("not a directory", update(cwd="nope")["error"])
        self.assertIn("always have tools", update(agent=False)["error"])
        self.assertTrue(update(archived=True)["data"]["session"]["archived"])
        listed = [x for x in self.client.call("list_sessions")["data"]["sessions"] if x["id"] == sid]
        self.assertTrue(listed[0]["archived"])
        self.client.call("delete_session", session=sid)
        self.assertEqual(self.client.call("get_session", session=sid)["type"], "error")

    def test_wrong_password_warns_but_local_sessions_still_list(self):
        (self.root / "service.json").write_text(json.dumps({"password": "wrong"}))
        data = self.client.call("list_sessions")["data"]
        self.assertIn("Authentication required", data["warning"])
        self.assertEqual(data["sessions"], [])

    def test_service_url_is_discovered_from_the_cli(self):
        fake = self.root / "opencode"
        fake.write_text(f"#!/bin/sh\necho http://127.0.0.1:{self.port}\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = self.write_config(url="", command=str(fake))
        client = StreamClient(env)
        try:
            self.assertEqual(client.call("new_session")["type"], "result")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
