#!/usr/bin/env python3
"""End-to-end tests: run `lllm-relay serve` as a subprocess against the mock Ollama.

    python3 relay/tests/test_relay.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELAY = HERE.parent / "lllm_relay.py"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """Minimal protocol client, the same shape the Android app will have."""

    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, str(RELAY), "serve"], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.next_id = 0
        self.hello = self.read()

    def read(self, timeout=10):
        result = {}

        def reader():
            result["line"] = self.proc.stdout.readline()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout)
        if "line" not in result or not result["line"]:
            raise AssertionError("no message from relay")
        return json.loads(result["line"])

    def send(self, op, **fields):
        self.next_id += 1
        self.proc.stdin.write((json.dumps({"id": self.next_id, "op": op, **fields}) + "\n").encode())
        self.proc.stdin.flush()
        return self.next_id

    def call(self, op, **fields):
        rid = self.send(op, **fields)
        msg = self.read()
        assert msg["id"] == rid, msg
        return msg

    def events_until_end(self, rid):
        events = []
        while True:
            ev = self.read()
            if ev.get("id") == rid:
                events.append(ev)
                if ev["type"] in ("end", "error"):
                    return events

    def close(self, wait=True):
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        if wait:
            self.proc.wait(15)
            self.proc.stdout.close()


class RelayTest(unittest.TestCase):
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
        cfg = {
            "providers": {"ollama": {"type": "ollama", "base_url": f"http://127.0.0.1:{self.port}"}},
            "sessions_dir": f"{self.tmp.name}/sessions",
            "partial_save_interval": 0.2,
        }
        cfg_path = Path(self.tmp.name) / "config.json"
        cfg_path.write_text(json.dumps(cfg))
        self.env = {**os.environ, "LLLM_CONFIG": str(cfg_path)}
        self.client = Client(self.env)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def new_session(self):
        return self.client.call("new_session")["data"]["session"]

    def test_hello_and_models(self):
        self.assertEqual(self.client.hello["type"], "hello")
        self.assertEqual(self.client.hello["protocol"], 1)
        models = self.client.call("list_models")["data"]["models"]
        self.assertIn("qwen-mock:latest", models)

    def test_chat_streams_and_persists(self):
        s = self.new_session()
        self.assertEqual(s["model"], "qwen-mock:latest")  # first installed model
        rid = self.client.send("chat", session=s["id"], text="hello there")
        events = self.client.events_until_end(rid)
        self.assertEqual(events[0]["type"], "start")
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        self.assertEqual(text.strip(), "echo(2 msgs): hello there")  # system + user
        end = events[-1]
        self.assertEqual(end["status"], "done")
        self.assertEqual(end["message"]["stats"]["tokens_per_sec"], 10.0)

        # Second turn sees the history; a fresh connection sees everything.
        rid = self.client.send("chat", session=s["id"], text="again")
        text = "".join(e["text"] for e in self.client.events_until_end(rid) if e["type"] == "delta")
        self.assertIn("echo(4 msgs)", text)
        other = Client(self.env)
        try:
            listed = other.call("list_sessions")["data"]["sessions"]
            self.assertEqual(listed[0]["title"], "hello there")
            full = other.call("get_session", session=s["id"])["data"]["session"]
            self.assertEqual([m["role"] for m in full["messages"]], ["user", "assistant"] * 2)
            self.assertFalse(full["generating"])
        finally:
            other.close()

    def test_cancel_keeps_partial_reply(self):
        s = self.new_session()
        rid = self.client.send("chat", session=s["id"], text="please be slow about it")
        while self.client.read()["type"] != "delta":
            pass
        self.client.call("cancel", target=rid)
        end = self.client.events_until_end(rid)[-1]
        self.assertEqual(end["status"], "cancelled")
        self.assertTrue(end["message"]["content"].startswith("echo"))
        saved = self.client.call("get_session", session=s["id"])["data"]["session"]
        self.assertEqual(saved["messages"][-1]["status"], "cancelled")

    def test_backend_error_is_reported(self):
        s = self.new_session()
        end = self.client.events_until_end(self.client.send("chat", session=s["id"], text="boom"))[-1]
        self.assertEqual(end["status"], "error")
        self.assertIn("simulated backend failure", end["error"])
        bad = self.client.call("update_session", session=s["id"], model="nope")
        self.assertEqual(bad["type"], "result")
        end = self.client.events_until_end(self.client.send("chat", session=s["id"], text="hi"))[-1]
        self.assertIn("not found", end["error"])

    def test_generation_survives_disconnect(self):
        s = self.new_session()
        rid = self.client.send("chat", session=s["id"], text="slow one two three")
        while self.client.read()["type"] != "delta":
            pass
        dropped = self.client
        dropped.close(wait=False)  # phone drops off; the relay should finish and save
        other = Client(self.env)
        try:
            mid = other.call("get_session", session=s["id"])["data"]["session"]
            self.assertTrue(mid["generating"])
            self.assertTrue(mid["messages"][-1].get("partial"))
            deadline = time.time() + 15
            while time.time() < deadline:
                full = other.call("get_session", session=s["id"])["data"]["session"]
                if not full["generating"]:
                    break
                time.sleep(0.3)
            last = full["messages"][-1]
            self.assertNotIn("partial", last)
            self.assertIn("slow one two three", last["content"])
        finally:
            other.close()
        dropped.proc.wait(15)
        dropped.proc.stdout.close()
        self.client = Client(self.env)  # for tearDown

    def test_openai_provider_with_key_file(self):
        key_file = Path(self.tmp.name) / "api.key"
        key_file.write_text("test-key\n")
        cfg = json.loads(Path(self.env["LLLM_CONFIG"]).read_text())
        cfg["providers"]["remote"] = {"type": "openai", "base_url": f"http://127.0.0.1:{self.port}/v1",
                                      "api_key_file": str(key_file)}
        cfg["providers"]["curated"] = {**cfg["providers"]["remote"], "models": ["pick/one", "pick/two"]}
        Path(self.env["LLLM_CONFIG"]).write_text(json.dumps(cfg))
        client = Client(self.env)
        try:
            self.assertEqual(client.call("list_models", provider="remote")["data"]["models"],
                             ["vendor/model-a", "vendor/model-b"])
            self.assertEqual(client.call("list_models", provider="curated")["data"]["models"],
                             ["pick/one", "pick/two"])
            s = client.call("new_session", provider="remote")["data"]["session"]
            self.assertEqual(s["model"], "vendor/model-a")
            events = client.events_until_end(client.send("chat", session=s["id"], text="hi"))
            self.assertEqual("".join(e["text"] for e in events if e["type"] == "thinking"), "pondering. ")
            self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta").strip(), "openai-echo: hi")
            stats = events[-1]["message"]["stats"]
            self.assertEqual(stats["eval_count"], 4)
            self.assertGreater(stats["tokens_per_sec"], 0)
            self.assertIn("ttft_sec", stats)

            key_file.write_text("wrong-key")  # read per request, so rotating the key needs no restart
            end = client.events_until_end(client.send("chat", session=s["id"], text="hi"))[-1]
            self.assertEqual(end["status"], "error")
            self.assertIn("invalid api key", end["error"])
        finally:
            client.close()

    def test_bad_requests(self):
        self.assertEqual(self.client.call("nope")["type"], "error")
        self.assertIn("invalid session id", self.client.call("get_session", session="../etc")["error"])
        self.client.proc.stdin.write(b"not json\n")
        self.client.proc.stdin.flush()
        self.assertEqual(self.client.read()["type"], "error")
        self.assertEqual(self.client.call("ping")["type"], "result")


if __name__ == "__main__":
    unittest.main(verbosity=2)
