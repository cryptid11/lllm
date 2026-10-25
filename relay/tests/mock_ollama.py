#!/usr/bin/env python3
"""A fake Ollama server for testing the relay without a real model.

Streams back "echo: <last user message>" word by word using chunked NDJSON, like Ollama.
Messages containing "slow" stream slowly (for cancel tests); "boom" returns a backend error;
"markdown" replies with a sample of headings/lists/code; "think" streams reasoning first.

    python3 mock_ollama.py [port] [seconds-per-word]
"""
import json
import sys
import time

WORD_DELAY = 0.01
SAMPLE = """## Sample reply
Here is a list:
- **bold** item
- an item with `inline code`

```python
def hello(name):
    return f"hi {name}"
```
That's all."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chunk(self, obj):
        data = (json.dumps(obj) + "\n").encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/api/tags":
            self._json(200, {"models": [{"name": "qwen-mock:latest"}, {"name": "other:1b"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/api/chat":
            return self._json(404, {"error": "not found"})
        if req["model"] not in ("qwen-mock:latest", "other:1b"):
            return self._json(404, {"error": f"model '{req['model']}' not found"})
        last = req["messages"][-1]["content"]
        if "boom" in last:
            return self._json(500, {"error": "simulated backend failure"})

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        delay = 0.5 if "slow" in last else WORD_DELAY
        try:
            if req.get("think") or "think" in last:
                for word in "The user wants a reply. I will echo it back.".split(" "):
                    self._chunk({"message": {"role": "assistant", "content": "", "thinking": word + " "}, "done": False})
                    time.sleep(delay)
            n_msgs = len(req["messages"])
            text = SAMPLE if "markdown" in last else f"echo({n_msgs} msgs): {last}"
            for word in text.split(" "):
                self._chunk({"message": {"role": "assistant", "content": word + " "}, "done": False})
                time.sleep(delay)
            self._chunk({"message": {"role": "assistant", "content": ""}, "done": True,
                         "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 7,
                         "eval_duration": 700_000_000})
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # clients hanging up mid-stream (cancel tests) are expected


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    if len(sys.argv) > 2:
        WORD_DELAY = float(sys.argv[2])
    Server(("127.0.0.1", port), Handler).serve_forever()
