#!/usr/bin/env python3
"""lllm-relay: bridge between a remote client (talking over SSH) and a local LLM backend.

The client (e.g. the Android app) opens an SSH exec channel running `lllm-relay serve`
and speaks newline-delimited JSON over stdin/stdout (see docs/PROTOCOL.md).
Conversations are stored as JSON files on this machine, so any client can resume them.

Backends are pluggable: native Ollama, or anything OpenAI-compatible
(llama.cpp server, vLLM, LM Studio, OpenRouter, ...). Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import codecs
import fnmatch
import getpass
import http.client
import json
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

VERSION = "0.3.0"
PROTOCOL_VERSION = 2

CONFIG_PATH = Path(os.environ.get("LLLM_CONFIG", "~/.config/lllm/config.json")).expanduser()

DEFAULT_CONFIG = {
    "default_provider": "ollama",
    "providers": {
        "ollama": {
            "type": "ollama",
            "base_url": "http://127.0.0.1:11434",
            # Empty means: use the first model the backend reports.
            "default_model": "",
        },
    },
    "system_prompt": "You are a helpful assistant.",
    "sessions_dir": "~/.local/share/lllm/sessions",
    # Seconds to wait for each read from the backend (prompt processing on a Pi can be slow).
    "request_timeout": 900,
    # Seconds between saves of a reply that is still being generated.
    "partial_save_interval": 2.0,
    # Agent engine for new sessions: "builtin" (this file's tool loop), "pi" (https://pi.dev)
    # or "opencode" (the OpenCode service on this machine; its sessions appear in the app).
    "engine": "builtin",
    # With "engine": "opencode": the engine of the app's other tab ("builtin" or "pi").
    "local_engine": "builtin",
    "opencode": {
        "command": "opencode",
        # Empty: ask `opencode service status`, starting the service if needed.
        "url": "",
        "username": "opencode",
        "password_file": "~/.config/opencode/service.json",
        # Folder and approval mode of sessions created from the app.
        "workdir": "~",
        "permission_mode": "auto-read",
    },
    "pi": {
        # How to start Pi (here: Node 22 installed under ~/.local/node22).
        "command": ["~/.local/node22/bin/node", "~/.local/node22/bin/pi"],
        "args": [],
        "session_dir": "~/.local/share/lllm/pi-sessions",
        "append_system_prompt": "The user reads your replies on a phone: keep them short.",
        # Seconds to wait for Pi to stop after an abort before killing it.
        "abort_grace": 10,
    },
    "agent": {
        # New sessions get tools (shell, files) on this machine.
        "enabled": True,
        # ask: approve every tool call; auto-read: reading/searching is automatic; auto: nothing asks.
        "permission_mode": "auto-read",
        # Initial working directory of new sessions.
        "workdir": "~",
        # If set, file tools refuse paths outside this folder (bash is not confined).
        "confine_to": None,
        # Model turns per message before the relay stops the loop.
        "max_steps": 40,
        # Seconds a tool call waits for approval before it is refused.
        "approval_timeout": 3600,
    },
}


class RelayError(Exception):
    """An error caused by the request (bad id, unknown session, ...)."""


class BackendError(Exception):
    """The LLM backend failed or could not be reached."""


class BackendUnreachable(BackendError):
    """The backend could not be contacted at all."""


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- config


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise RelayError(f"{path}: config must be a JSON object")
        cfg.update(user)
        cfg["agent"] = {**DEFAULT_CONFIG["agent"], **(user.get("agent") or {})}
        cfg["pi"] = {**DEFAULT_CONFIG["pi"], **(user.get("pi") or {})}
        cfg["opencode"] = {**DEFAULT_CONFIG["opencode"], **(user.get("opencode") or {})}
    return cfg


# --------------------------------------------------------------------------- HTTP


class CancelToken:
    """Cancels a generation, unblocking any in-progress socket read."""

    def __init__(self):
        self._event = threading.Event()
        self._sock = None
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def attach(self, sock):
        with self._lock:
            self._sock = sock
            if self._event.is_set():
                self._shutdown()

    def cancel(self):
        with self._lock:
            self._event.set()
            self._shutdown()

    def _shutdown(self):
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _error_detail(body: str) -> str:
    try:
        obj = json.loads(body)
    except ValueError:
        return body.strip()[:500]
    err = obj.get("error", obj) if isinstance(obj, dict) else obj
    if isinstance(err, dict):
        err = err.get("message", err)
    return str(err)[:500]


def http_request(method, url, body=None, headers=None, timeout=30.0, token=None):
    """Send a request and return (connection, response) once the status is known to be OK."""
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=timeout)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    try:
        conn.request(method, path, body=data, headers=hdrs)
        if token is not None:
            token.attach(conn.sock)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        if token is not None and token.cancelled:
            raise Cancelled() from None
        raise BackendUnreachable(f"cannot reach backend at {u.scheme}://{u.netloc}: {e}") from None
    if resp.status >= 400:
        detail = _error_detail(resp.read().decode("utf-8", "replace"))
        conn.close()
        raise BackendError(f"backend returned HTTP {resp.status}: {detail}")
    return conn, resp


def http_json(method, url, body=None, headers=None, timeout=30.0):
    conn, resp = http_request(method, url, body, headers, timeout)
    try:
        return json.loads(resp.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise BackendError(f"bad response from backend: {e}") from None
    finally:
        conn.close()


def iter_lines(resp, token):
    try:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.strip()
            if line:
                yield line.decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        if token.cancelled:
            raise Cancelled() from None
        raise BackendError(f"connection to backend lost: {e}") from None
    if token.cancelled:
        raise Cancelled()


# --------------------------------------------------------------------------- providers
#
# A provider turns OpenAI-format messages (and optional OpenAI-format tool specs) into a stream of
# ("content", text), ("thinking", text), ("tool_call", {id, name, arguments}) and ("stats", dict).


def _new_call_id():
    return f"call_{uuid.uuid4().hex[:16]}"


class OllamaProvider:
    def __init__(self, name, cfg, timeout):
        self.name = name
        self.cfg = cfg
        self.timeout = timeout
        self.base = cfg.get("base_url", "http://127.0.0.1:11434").rstrip("/")

    def list_models(self):
        if self.cfg.get("models"):
            return list(self.cfg["models"])
        data = http_json("GET", f"{self.base}/api/tags", timeout=15)
        return [m["name"] for m in data.get("models", [])]

    @staticmethod
    def _messages(messages):
        """Ollama wants tool arguments as objects and tool results tagged with the tool's name."""
        names, out = {}, []
        for m in messages:
            m = dict(m)
            if m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    names[c["id"]] = c["function"]["name"]
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    calls.append({"function": {"name": c["function"]["name"], "arguments": args}})
                m["tool_calls"] = calls
            if m.get("role") == "tool":
                m = {"role": "tool", "content": m.get("content", ""), "tool_name": names.get(m.get("tool_call_id"), "")}
            if m.get("content") is None:
                m["content"] = ""
            out.append(m)
        return out

    def stream_chat(self, model, messages, opts, token, tools=None):
        payload = {"model": model, "messages": self._messages(messages), "stream": True}
        if tools:
            payload["tools"] = tools
        options = {**self.cfg.get("options", {}), **opts.get("options", {})}
        if options:
            payload["options"] = options
        think = opts.get("think", self.cfg.get("think"))
        if think is not None:
            payload["think"] = think
        if "keep_alive" in self.cfg:
            payload["keep_alive"] = self.cfg["keep_alive"]

        conn, resp = http_request("POST", f"{self.base}/api/chat", payload,
                                  timeout=self.timeout, token=token)
        try:
            for line in iter_lines(resp, token):
                obj = json.loads(line)
                if obj.get("error"):
                    raise BackendError(str(obj["error"]))
                msg = obj.get("message") or {}
                if msg.get("thinking"):
                    yield "thinking", msg["thinking"]
                if msg.get("content"):
                    yield "content", msg["content"]
                for c in msg.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    yield "tool_call", {"id": c.get("id") or _new_call_id(), "name": fn.get("name", ""),
                                        "arguments": json.dumps(fn.get("arguments") or {})}
                if obj.get("done"):
                    yield "stats", self._stats(obj)
                    return
        finally:
            conn.close()

    @staticmethod
    def _stats(obj):
        stats = {k: obj[k] for k in ("prompt_eval_count", "eval_count", "done_reason") if k in obj}
        if obj.get("eval_count") and obj.get("eval_duration"):
            stats["tokens_per_sec"] = round(obj["eval_count"] / (obj["eval_duration"] / 1e9), 2)
        return stats


class OpenAIProvider:
    def __init__(self, name, cfg, timeout):
        self.name = name
        self.cfg = cfg
        self.timeout = timeout
        self.base = cfg.get("base_url", "http://127.0.0.1:8080/v1").rstrip("/")

    def _api_key(self):
        if self.cfg.get("api_key"):
            return self.cfg["api_key"]
        if self.cfg.get("api_key_file"):
            path = Path(self.cfg["api_key_file"]).expanduser()
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise BackendError(f"provider {self.name!r}: cannot read api_key_file {path}: {e.strerror}") from None
        return os.environ.get(self.cfg.get("api_key_env", ""), "")

    def _headers(self):
        key = self._api_key()
        headers = dict(self.cfg.get("headers", {}))
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def list_models(self):
        if self.cfg.get("models"):
            return list(self.cfg["models"])
        data = http_json("GET", f"{self.base}/models", headers=self._headers(), timeout=15)
        return [m["id"] for m in data.get("data", [])]

    def stream_chat(self, model, messages, opts, token, tools=None):
        payload = {"model": model, "messages": messages, "stream": True,
                   **self.cfg.get("params", {}), **opts.get("params", {})}
        if tools:
            payload["tools"] = tools
        if self.cfg.get("include_usage", True):
            payload["stream_options"] = {"include_usage": True}

        conn, resp = http_request("POST", f"{self.base}/chat/completions", payload,
                                  headers=self._headers(), timeout=self.timeout, token=token)
        stats = {}
        calls = {}  # index -> {id, name, arguments}; arguments arrive in fragments
        first = last = None
        try:
            for line in iter_lines(resp, token):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("error"):
                    raise BackendError(_error_detail(json.dumps(obj)))
                if obj.get("usage"):
                    u = obj["usage"]
                    stats["prompt_eval_count"] = u.get("prompt_tokens")
                    stats["eval_count"] = u.get("completion_tokens")
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning or delta.get("content") or delta.get("tool_calls"):
                        last = time.monotonic()
                        first = first or last
                    if reasoning:
                        yield "thinking", reasoning
                    if delta.get("content"):
                        yield "content", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        call = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            call["id"] = tc["id"]
                        if fn.get("name") and not call["name"]:
                            call["name"] = fn["name"]
                        if fn.get("arguments"):
                            call["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        stats["done_reason"] = choice["finish_reason"]
        finally:
            conn.close()
        for i in sorted(calls):
            call = calls[i]
            yield "tool_call", {**call, "id": call["id"] or _new_call_id()}
        if stats.get("eval_count") and first and last and last > first:
            stats["tokens_per_sec"] = round(stats["eval_count"] / (last - first), 2)
        yield "stats", stats


PROVIDER_TYPES = {"ollama": OllamaProvider, "openai": OpenAIProvider}


# --------------------------------------------------------------------------- agent tools

READ_ONLY_TOOLS = {"read_file", "list_dir", "grep", "read", "find", "ls"}  # builtin and Pi names
PERMISSION_MODES = ("ask", "auto-read", "auto")
APPROVAL_DECISIONS = ("allow", "allow_session", "deny")
MAX_TOOL_OUTPUT = 30_000  # characters of a tool result sent to the model
MAX_LIVE_OUTPUT = 100_000  # characters of bash output streamed to the client per call
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".gradle", "build", "dist"}

AGENT_PROMPT = """You are an agent with tools on the machine "{host}" (user {user}, {os}).
Working directory: {cwd}
Today is {date}.

Use the tools to look things up and make changes instead of guessing: read files before editing them,
and run commands to check your work. Use edit_file for changes to existing files and write_file for new ones.
The user may have to approve each command or change from their phone, so avoid unnecessary calls,
and never run destructive commands (deleting data, force-pushing, killing services...) unless asked.
Keep answers short: the user reads them on a phone. Put code in fenced code blocks."""


def _fn(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOL_SPECS = [
    _fn("bash", "Run a bash command on the server. Returns stdout and stderr together, then the exit code. "
                "Runs in the working directory, and `cd` persists between calls. There is no stdin or TTY, "
                "so do not start interactive programs. Long output is truncated in the middle.",
        {"command": {"type": "string", "description": "The command to run"},
         "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120, max 600)"}},
        ["command"]),
    _fn("read_file", "Read a text file. Returns numbered lines; use offset and limit for large files.",
        {"path": {"type": "string"},
         "offset": {"type": "integer", "description": "First line to read, 1-based"},
         "limit": {"type": "integer", "description": "Maximum number of lines (default 2000)"}},
        ["path"]),
    _fn("write_file", "Create or overwrite a file with the given content. Parent directories are created.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"]),
    _fn("edit_file", "Replace an exact piece of text in a file. old_string must appear exactly once "
                     "(including whitespace) unless replace_all is true. Read the file first.",
        {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean"}},
        ["path", "old_string", "new_string"]),
    _fn("list_dir", "List a directory; directory names end with /.",
        {"path": {"type": "string", "description": "Defaults to the working directory"}},
        []),
    _fn("grep", "Search file contents recursively with a Python regular expression.",
        {"pattern": {"type": "string"},
         "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
         "glob": {"type": "string", "description": "Only search files whose name matches, e.g. *.py"}},
        ["pattern"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SPECS}


def truncate(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-half:]}"


class ToolError(Exception):
    """A tool failed in a way the model should be told about."""


class ToolRunner:
    """Executes tool calls in a working directory (`cwd`, updated by `cd` in bash)."""

    def __init__(self, cwd, stop=lambda: False, on_output=None, confine=None):
        self.cwd = cwd
        self.stop = stop
        self.on_output = on_output or (lambda text: None)
        self.confine = Path(confine).expanduser().resolve() if confine else None

    def run(self, name, args):
        """Returns (ok, output); tool failures become ok=False with an explanation."""
        fn = getattr(self, f"tool_{name}", None) if name in TOOL_NAMES else None
        if fn is None:
            return False, f"Unknown tool: {name}"
        if not isinstance(args, dict):
            return False, "Tool arguments must be a JSON object"
        try:
            return True, fn(**args)
        except ToolError as e:
            return False, str(e)
        except TypeError as e:
            return False, f"Bad arguments for {name}: {e}"
        except (OSError, ValueError) as e:
            return False, f"{type(e).__name__}: {e}"

    def _path(self, path, must_exist=False):
        if not isinstance(path, str) or not path:
            raise ToolError("path is required")
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = Path(self.cwd) / p
        p = Path(os.path.normpath(p))
        if self.confine:
            real = p.resolve()
            if real != self.confine and self.confine not in real.parents:
                raise ToolError(f"{p} is outside the allowed folder {self.confine}")
        if must_exist and not p.exists():
            raise ToolError(f"No such file or directory: {p}")
        return p

    @staticmethod
    def _kill(proc):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                return
            try:
                proc.wait(2)
                return
            except subprocess.TimeoutExpired:
                continue

    def tool_bash(self, command, timeout=120):
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command is required")
        try:
            timeout = max(1, min(int(timeout), 600))
        except (TypeError, ValueError):
            timeout = 120
        fd, pwd_file = tempfile.mkstemp(prefix="lllm-pwd-")
        os.close(fd)
        # The EXIT trap records where the command ended up, so `cd` carries over to the next call.
        script = f"trap 'pwd -P > {shlex.quote(pwd_file)}' EXIT\n{command}"
        env = {**os.environ, "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1"}
        head, tail, limit = bytearray(), bytearray(), MAX_TOOL_OUTPUT * 4
        try:
            proc = subprocess.Popen(["bash", "-c", script], cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

            def pump():
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                streamed = 0
                while True:
                    data = proc.stdout.read1(65536)
                    if not data:
                        break
                    if len(head) < limit:
                        head.extend(data)
                    else:
                        tail.extend(data)
                        del tail[:-limit]
                    if streamed < MAX_LIVE_OUTPUT:
                        text = decoder.decode(data)
                        streamed += len(text)
                        self.on_output(text)

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            stopped = None
            while proc.poll() is None:
                if self.stop():
                    stopped = "cancelled by the user"
                elif time.monotonic() > deadline:
                    stopped = f"killed: timed out after {timeout}s"
                if stopped:
                    self._kill(proc)
                    break
                time.sleep(0.1)
            proc.wait()
            reader.join(2)  # a background job may keep the pipe open; don't wait for it
            try:
                new_cwd = Path(pwd_file).read_text().strip()
                if new_cwd and os.path.isdir(new_cwd):
                    self.cwd = new_cwd
            except OSError:
                pass
        finally:
            try:
                os.unlink(pwd_file)
            except OSError:
                pass
        out = head.decode("utf-8", "replace")
        if tail:
            out += "\n[... output truncated ...]\n" + tail.decode("utf-8", "replace")
        out = truncate(out.rstrip("\n"))
        status = f"[{stopped}]" if stopped else f"[exit code {proc.returncode}]"
        return f"{out}\n{status}" if out else status

    def tool_read_file(self, path, offset=1, limit=2000):
        p = self._path(path, must_exist=True)
        if p.is_dir():
            raise ToolError(f"{p} is a directory; use list_dir")
        if p.stat().st_size > 20_000_000:
            raise ToolError(f"{p} is too large to read ({p.stat().st_size} bytes); use bash with head/grep")
        data = p.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolError(f"{p} looks like a binary file")
        lines = data.decode("utf-8", "replace").splitlines()
        if not lines:
            return f"({p} is empty)"
        offset = max(1, int(offset or 1))
        limit = max(1, min(int(limit or 2000), 5000))
        chunk = lines[offset - 1:offset - 1 + limit]
        body = "\n".join(f"{n:>6}\t{line[:2000]}" for n, line in enumerate(chunk, start=offset))
        remaining = len(lines) - (offset - 1 + len(chunk))
        if remaining > 0:
            body += f"\n[{remaining} more lines; continue with offset={offset + len(chunk)}]"
        return truncate(body, 100_000)

    def tool_write_file(self, path, content):
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        p = self._path(path)
        if p.is_dir():
            raise ToolError(f"{p} is a directory")
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {p} ({len(content.splitlines())} lines, {len(content.encode())} bytes)"

    def tool_edit_file(self, path, old_string, new_string, replace_all=False):
        if not isinstance(old_string, str) or not isinstance(new_string, str) or not old_string:
            raise ToolError("old_string (non-empty) and new_string must be strings")
        p = self._path(path, must_exist=True)
        text = p.read_text(encoding="utf-8")
        count = text.count(old_string)
        if count == 0:
            raise ToolError("old_string was not found; it must match the file exactly, including whitespace")
        if count > 1 and not replace_all:
            raise ToolError(f"old_string appears {count} times; include more context or set replace_all")
        p.write_text(text.replace(old_string, new_string, -1 if replace_all else 1), encoding="utf-8")
        return f"Edited {p} ({count if replace_all else 1} replacement{'s' if replace_all and count > 1 else ''})"

    def tool_list_dir(self, path="."):
        p = self._path(path or ".", must_exist=True)
        if not p.is_dir():
            raise ToolError(f"{p} is not a directory")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        for e in entries[:1000]:
            try:
                lines.append(f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} bytes)")
            except OSError:
                lines.append(e.name)
        more = f"\n[{len(entries) - 1000} more entries]" if len(entries) > 1000 else ""
        return f"{p}:\n" + ("\n".join(lines) or "(empty)") + more

    def tool_grep(self, pattern, path=".", glob=None):
        try:
            rx = re.compile(pattern)
        except (re.error, TypeError) as e:
            raise ToolError(f"invalid regular expression: {e}")
        base = self._path(path or ".", must_exist=True)
        files = [base] if base.is_file() else self._walk(base, glob)
        hits = []
        for f in files:
            if self.stop():
                break
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            name = os.path.relpath(f, base) if base.is_dir() else f.name
            for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{name}:{n}: {line.strip()[:300]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n[stopped at 200 matches]"
        return "\n".join(hits) or "No matches"

    @staticmethod
    def _walk(base, glob):
        seen = 0
        for root, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                if glob and not fnmatch.fnmatch(name, glob):
                    continue
                seen += 1
                if seen > 20_000:
                    return
                yield Path(root) / name


class StopCheck:
    """True once the user stopped this reply: locally (token) or from any connection (session flag)."""

    def __init__(self, store, sid, token):
        self.store, self.sid, self.token = store, sid, token
        self._next_check = 0.0

    def __call__(self):
        if self.token.cancelled:
            return True
        now = time.monotonic()
        if now >= self._next_check:
            self._next_check = now + 1.0
            try:
                flagged = bool(self.store.load(self.sid).get("cancel_requested"))
            except RelayError:  # deleted
                flagged = True
            if flagged:
                self.token.cancel()
                return True
        return False


# --------------------------------------------------------------------------- Pi engine

# Pi extension that asks the relay about every tool call. The relay applies the session's
# permission mode and answers at once, or after the user decides on their phone.
# Request: an `input` dialog whose title is JSON. Reply: "allow" or "deny:<reason for the model>".
PI_EXTENSION = """// Written by lllm-relay; changes are overwritten.
export default function (pi: any) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    const request = JSON.stringify({ lllm: "approve", id: event.toolCallId, tool: event.toolName, input: event.input });
    const answer = await ctx.ui.input(request, "");
    if (answer === "allow") return;
    const reason = typeof answer === "string" && answer.startsWith("deny:") ? answer.slice(5).trim() : "";
    return { block: true, reason: reason || "The user did not approve this tool call." };
  });
}
"""

# Environment variable Pi reads each provider's key from.
PI_KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
              "google": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "mistral": "MISTRAL_API_KEY", "xai": "XAI_API_KEY"}


def pi_extension_path():
    """Writes the approval extension next to the sessions (keeps the relay a single file)."""
    path = Path("~/.local/share/lllm/pi/lllm-approval.ts").expanduser()
    if not path.exists() or path.read_text(encoding="utf-8") != PI_EXTENSION:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PI_EXTENSION, encoding="utf-8")
    return path


def pi_provider_name(provider):
    """Pi's name for one of our providers: explicit `pi_provider`, else guessed from the backend."""
    if provider.cfg.get("pi_provider"):
        return provider.cfg["pi_provider"]
    if provider.cfg.get("type") == "ollama":
        return "ollama"
    if "openrouter.ai" in provider.base:
        return "openrouter"
    return None


def _pi_text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def pi_assistant(msg):
    """(text, thinking, tool_calls in OpenAI shape) from a Pi assistant message."""
    text, thinking, calls = [], [], []
    content = msg.get("content")
    if isinstance(content, str):
        text.append(content)
    for block in content if isinstance(content, list) else []:
        kind = block.get("type")
        if kind == "text":
            text.append(block.get("text", ""))
        elif kind == "thinking":
            thinking.append(block.get("thinking", ""))
        elif kind == "toolCall":
            calls.append({"id": block.get("id"), "type": "function",
                          "function": {"name": block.get("name"), "arguments": json.dumps(block.get("arguments") or {})}})
    return "".join(text), "".join(thinking), calls


# --------------------------------------------------------------------------- sessions

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def is_generating(session) -> bool:
    gen = session.get("generating")
    return bool(gen) and _pid_alive(gen.get("pid"))


def summarize(session) -> dict:
    msgs = session.get("messages", [])
    last = next((m for m in reversed(msgs) if m.get("content")), None)
    return {
        "id": session["id"],
        "title": session.get("title") or "",
        "created": session.get("created"),
        "updated": session.get("updated"),
        "provider": session.get("provider"),
        "model": session.get("model"),
        "message_count": len(msgs),
        "preview": (last["content"][:120] if last else ""),
        "generating": is_generating(session),
        "archived": bool(session.get("archived")),
        "agent": bool(session.get("agent")),
        "engine": session.get("engine") or "builtin",
    }


def make_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else line[:57].rstrip() + "..."


class SessionStore:
    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def _path(self, sid) -> Path:
        if not isinstance(sid, str) or not ID_RE.match(sid):
            raise RelayError(f"invalid session id: {sid!r}")
        return self.root / f"{sid}.json"

    def exists(self, sid) -> bool:
        return self._path(sid).exists()

    def load(self, sid) -> dict:
        try:
            with open(self._path(sid), encoding="utf-8") as f:
                session = json.load(f)
        except FileNotFoundError:
            raise RelayError(f"no such session: {sid}") from None
        self._heal(session)
        return session

    @staticmethod
    def _heal(session):
        """Mark a reply as interrupted if the process generating it died."""
        if session.get("generating") and not is_generating(session):
            session.pop("generating", None)
            for m in session.get("messages", []):
                if m.pop("partial", None):
                    m["status"] = "interrupted"
        if not session.get("generating"):
            session.pop("pending_approval", None)
            session.pop("cancel_requested", None)

    def save(self, session, durable=True, touch=True):
        """Write atomically. `touch` bumps `updated`, which orders the session list by activity."""
        if touch:
            session["updated"] = time.time()
        path = self._path(session["id"])
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False, indent=1)
                if durable:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, sid):
        try:
            self._path(sid).unlink()
        except FileNotFoundError:
            raise RelayError(f"no such session: {sid}") from None

    def list(self):
        out = []
        for p in self.root.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as f:
                    out.append(summarize(json.load(f)))
            except (OSError, ValueError, KeyError):
                continue
        out.sort(key=lambda s: s.get("updated") or 0, reverse=True)
        return out


# --------------------------------------------------------------------------- OpenCode engine


def _rule(action, effect, resource="*"):
    return {"action": action, "resource": resource, "effect": effect}


# OpenCode allows everything by default, so approvals need session rules (the last match wins).
# These keep the guards of its default agents, which a catch-all rule would otherwise override.
_OC_KEEP = [_rule("question", "allow"), _rule("browser", "deny"), _rule("skill", "deny", "browser")]
OPENCODE_RULES = {
    "auto": [],
    "ask": [_rule("*", "ask"), *_OC_KEEP],
    "auto-read": [_rule("*", "ask"), _rule("read", "allow"), _rule("glob", "allow"), _rule("grep", "allow"),
                  _rule("read", "ask", "*.env"), _rule("read", "ask", "*.env.*"),
                  _rule("read", "allow", "*.env.example"), *_OC_KEEP],
}
OPENCODE_REPLIES = {"allow": "once", "allow_session": "always", "deny": "reject"}
OPENCODE_DECISIONS = {"once": "allow", "always": "allow_session", "reject": "deny"}


def is_opencode_id(sid):
    return isinstance(sid, str) and sid.startswith("ses")


def _oc_text(content):
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("name") or block.get("uri"):
            parts.append(f"[file {block.get('name') or block.get('uri')}]")
    return "".join(parts)


def _oc_model(ref):
    return f"{ref['providerID']}/{ref['id']}" if ref else ""


def _oc_tool_output(state):
    text = _oc_text(state.get("content"))
    if state.get("status") == "error":
        message = (state.get("error") or {}).get("message", "")
        text = message if not text or text in message else f"{message}\n{text}"
    return truncate(text)


def _oc_stats(m):
    tokens, t = m.get("tokens") or {}, m.get("time") or {}
    stats = {"prompt_eval_count": tokens.get("input"), "eval_count": tokens.get("output"),
             "done_reason": m.get("finish"), "cost_usd": m.get("cost")}
    if t.get("completed"):
        stats["wall_sec"] = round((t["completed"] - t["created"]) / 1000, 2)
    if t.get("firstToken"):
        stats["ttft_sec"] = round((t["firstToken"] - t["created"]) / 1000, 2)
        if tokens.get("output") and t.get("streamed", 0) > t["firstToken"]:
            stats["tokens_per_sec"] = round(tokens["output"] / ((t["streamed"] - t["firstToken"]) / 1000), 2)
    return {k: v for k, v in stats.items() if v is not None}


def opencode_messages(raw, running=False):
    """OpenCode's messages in the relay's transcript format (assistant + tool messages)."""
    out, last = [], None
    for m in raw:
        kind, ts = m.get("type"), (m.get("time") or {}).get("created", 0) / 1000
        if kind == "user":
            out.append({"role": "user", "content": m.get("text", ""), "ts": ts})
        elif kind == "assistant":
            text, thinking, calls, results = [], [], [], []
            for part in m.get("content") or []:
                if part.get("type") == "text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "reasoning":
                    thinking.append(part.get("text", ""))
                elif part.get("type") == "tool":
                    state = part.get("state") or {}
                    args = state.get("input")
                    calls.append({"id": part.get("id"), "type": "function", "function": {
                        "name": part.get("name"), "arguments": json.dumps(args if isinstance(args, dict) else {})}})
                    if state.get("status") in ("completed", "error"):
                        results.append({"role": "tool", "tool_call_id": part.get("id"), "name": part.get("name"),
                                        "content": _oc_tool_output(state), "ok": state["status"] == "completed",
                                        "approval": "auto", "ts": ts})
            last = {"role": "assistant", "content": "".join(text), "ts": ts, "model": _oc_model(m.get("model")),
                    "stats": _oc_stats(m)}
            if thinking:
                last["thinking"] = "".join(thinking)
            if calls:
                last["tool_calls"] = calls
            if m.get("error"):
                last["status"], last["error"] = "error", m["error"].get("message", "error")
            if running and not (m.get("time") or {}).get("completed"):
                last["partial"] = True
            out.append(last)
            out.extend(results)
        elif kind == "shell":
            cid = m.get("id")
            last = {"role": "assistant", "content": "", "ts": ts, "tool_calls": [{
                "id": cid, "type": "function",
                "function": {"name": "shell", "arguments": json.dumps({"command": m.get("command", "")})}}]}
            out.append(last)
            if m.get("status") != "running":
                out.append({"role": "tool", "tool_call_id": cid, "name": "shell", "approval": "auto", "ts": ts,
                            "content": truncate((m.get("output") or {}).get("output", "")),
                            "ok": m.get("exit") in (0, None)})
        elif kind == "idle" and last is not None:
            if m.get("outcome") == "interrupted":
                last["status"] = "cancelled"
                last.pop("error", None)
            elif m.get("outcome") == "failed":
                last["status"], last["error"] = "error", (m.get("error") or {}).get("message", "failed")
    return out


class OpenCode:
    """Client for the OpenCode service on this machine, shaped like the relay's session API."""

    STREAM_TIMEOUT = 20.0  # silent event streams are re-opened, so nothing can hang forever

    def __init__(self, cfg):
        self.cfg = {**DEFAULT_CONFIG["opencode"], **(cfg or {})}
        self._url = (self.cfg.get("url") or "").rstrip("/") or None
        self._parents = {}
        self._default_model = None

    # ------------------------------------------------------------------ transport

    def _cli(self):
        extra = [os.path.expanduser(p) for p in ("~/.npm-global/bin", "~/.local/bin", "~/.opencode/bin")]
        path = os.pathsep.join([os.environ.get("PATH", ""), *extra])
        exe = shutil.which(os.path.expanduser(self.cfg["command"]), path=path)
        if not exe:
            raise BackendError("OpenCode is not installed (command not found: opencode)")
        return exe, {**os.environ, "PATH": path}

    def base_url(self):
        if self._url:
            return self._url
        exe, env = self._cli()
        for attempt in range(2):
            try:
                r = subprocess.run([exe, "service", "status"], env=env, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=30)
                found = re.search(r"https?://[^\s\"']+", r.stdout + r.stderr)
                if r.returncode == 0 and found:
                    self._url = found.group(0).rstrip("/")
                    return self._url
                if attempt == 0:
                    subprocess.run([exe, "service", "start"], env=env, stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise BackendError(f"cannot query the OpenCode service: {e}") from None
        raise BackendError("the OpenCode service is not running and could not be started")

    def _headers(self):
        try:
            with open(os.path.expanduser(self.cfg["password_file"]), encoding="utf-8") as f:
                password = json.load(f).get("password", "")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            raise BackendError(f"cannot read the OpenCode password file: {e}") from None
        pair = f"{self.cfg['username']}:{password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()} if password else {}

    def _connect(self, method, path, body, query, timeout, token=None):
        qs = "?" + urllib.parse.urlencode(query) if query else ""
        for attempt in range(2):
            try:
                return http_request(method, self.base_url() + path + qs, body, self._headers(), timeout, token)
            except BackendUnreachable:
                if attempt or self.cfg.get("url"):
                    raise
                self._url = None  # the service may have restarted on another port

    def request(self, method, path, body=None, query=None, timeout=30):
        conn, resp = self._connect(method, path, body, query, timeout)
        try:
            raw = resp.read()
            return json.loads(raw) if raw else None
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise BackendError(f"bad response from OpenCode: {e}") from None
        finally:
            conn.close()

    # ------------------------------------------------------------------ sessions

    def _info(self, sid):
        return self.request("GET", f"/api/session/{sid}")["data"]

    def active(self):
        return set(self.request("GET", "/api/session/active")["data"])

    def default_model(self):
        if self._default_model is None:
            model = self.request("GET", "/api/model/default").get("data") or {}
            self._default_model = _oc_model({"providerID": model["providerID"], "id": model["id"]}) if model else ""
        return self._default_model

    def _all_sessions(self, **query):
        out, cursor = [], None
        for _ in range(20):
            q = {"limit": "200", "parentID": "null", **query, **({"cursor": cursor} if cursor else {})}
            page = self.request("GET", "/api/session", query=q)
            out += page.get("data") or []
            cursor = (page.get("cursor") or {}).get("next")
            if not cursor or not page.get("data"):
                break
        return out

    def _summary(self, info, active):
        t = info.get("time") or {}
        return {
            "id": info["id"], "title": info.get("title") or "", "created": t.get("created", 0) / 1000,
            "updated": t.get("updated", 0) / 1000, "provider": "opencode",
            "model": _oc_model(info.get("model")) or self.default_model(), "message_count": 0,
            "preview": (info.get("location") or {}).get("directory", ""), "generating": info["id"] in active,
            "archived": bool(t.get("archived")), "agent": True, "engine": "opencode",
        }

    def list_sessions(self):
        active = self.active()
        seen = {s["id"]: s for s in self._all_sessions() + self._all_sessions(archived="true")}
        return [self._summary(s, active) for s in seen.values()]

    def list_models(self):
        models = self.request("GET", "/api/model")["data"]
        return {"provider": "opencode",
                "models": [_oc_model(m) for m in models if m.get("type", "chat") == "chat"]}

    @staticmethod
    def _mode(info):
        rules = info.get("permissions") or []
        return next((name for name, preset in OPENCODE_RULES.items() if preset == rules), "custom")

    def _family(self, sid):
        """The session and its sub-agent sessions: those can ask for approval too."""
        out = [sid]
        for child in self._all_sessions(parentID=sid):
            self._parents[child["id"]] = sid
            out.append(child["id"])
        return out

    def _belongs(self, root, sid):
        for _ in range(8):
            if sid == root:
                return True
            if not sid:
                return False
            if sid not in self._parents:
                try:
                    self._parents[sid] = self._info(sid).get("parentID")
                except BackendError:
                    return False
            sid = self._parents[sid]
        return False

    def _permissions(self, sid):
        return [p for member in self._family(sid)
                for p in self.request("GET", f"/api/session/{member}/permission").get("data") or []]

    def _forms(self, sid):
        return [f for member in self._family(sid)
                for f in self.request("GET", f"/api/session/{member}/form").get("data") or []]

    @staticmethod
    def _approval(req, tools):
        cid = (req.get("source") or {}).get("id") or req["id"]
        tool = tools.get(cid) or {}
        resources = req.get("resources") or []
        fallback = {"command": " && ".join(resources)} if req.get("action") == "shell" else {"resources": resources}
        return {"call_id": cid, "name": tool.get("name") or req.get("action"),
                "arguments": tool.get("input") or fallback, "save": req.get("save") or []}

    def _messages_raw(self, sid):
        out, cursor = [], None
        for _ in range(100):
            q = {"order": "asc", "limit": "200", **({"cursor": cursor} if cursor else {})}
            page = self.request("GET", f"/api/session/{sid}/message", query=q)
            data = page.get("data") or []
            out += data
            cursor = (page.get("cursor") or {}).get("next")
            if not cursor or len(data) < 200:
                break
        return out

    def get_session(self, sid, info=None):
        info = info or self._info(sid)
        running = sid in self.active()
        raw = self._messages_raw(sid)
        session = {
            **self._summary(info, {sid} if running else ()),
            "system": None, "options": {}, "permission_mode": self._mode(info),
            "cwd": (info.get("location") or {}).get("directory", ""),
            "messages": opencode_messages(raw, running),
        }
        if running:
            tools = {}
            for m in raw:
                for part in m.get("content") or [] if m.get("type") == "assistant" else []:
                    if part.get("type") == "tool":
                        tools[part.get("id")] = {"name": part.get("name"), "input": (part.get("state") or {}).get("input")}
            permissions = self._permissions(sid)
            if permissions:
                session["pending_approval"] = self._approval(permissions[0], tools)
            forms = self._forms(sid)
            if forms:
                session["pending_form"] = forms[0]
        return session

    @staticmethod
    def _model_ref(model, current=None):
        if not isinstance(model, str) or not model:
            raise RelayError("invalid model")
        provider, sep, name = model.partition("/")
        if sep:
            return {"providerID": provider, "id": name}
        if current:
            return {"providerID": current["providerID"], "id": model}
        raise RelayError("model must look like provider/name")

    def new_session(self, title=None, model=None):
        mode = self.cfg.get("permission_mode", "auto-read")
        if mode not in OPENCODE_RULES:
            raise RelayError(f"opencode.permission_mode must be one of {', '.join(OPENCODE_RULES)}")
        workdir = os.path.expanduser(self.cfg.get("workdir") or "~")
        body = {"location": {"directory": os.path.abspath(workdir)}, "permissions": OPENCODE_RULES[mode]}
        if title:
            body["title"] = title
        if model:
            body["model"] = self._model_ref(model)
        info = self.request("POST", "/api/session", body)["data"]
        return self.get_session(info["id"], info)

    def update_session(self, sid, **changes):
        info = self._info(sid)
        patch = {}
        for key, value in changes.items():
            if key == "title":
                if not isinstance(value, str):
                    raise RelayError("title must be a string")
                patch["title"] = " ".join(value.split())[:200]
            elif key == "archived":
                if not isinstance(value, bool):
                    raise RelayError("archived must be true or false")
                patch["archived"] = value
            elif key == "permission_mode":
                if value not in OPENCODE_RULES:
                    raise RelayError(f"permission_mode must be one of {', '.join(OPENCODE_RULES)}")
                patch["permissions"] = OPENCODE_RULES[value]
            elif key == "model":
                self.request("POST", f"/api/session/{sid}/model",
                             {"model": self._model_ref(value, info.get("model"))})
            elif key == "cwd":
                if not isinstance(value, str):
                    raise RelayError("cwd must be a string")
                base = (info.get("location") or {}).get("directory") or os.path.expanduser("~")
                target = os.path.normpath(os.path.join(base, os.path.expanduser(value)))
                if not os.path.isdir(target):
                    raise RelayError(f"not a directory: {target}")
                self.request("POST", f"/api/session/{sid}/move", {"directory": target})
            elif key == "agent":
                if value is not True:
                    raise RelayError("OpenCode sessions always have tools; use /mode to control approvals")
            else:
                raise RelayError(f"cannot change {key!r} on an OpenCode session")
        if patch:
            self.request("PATCH", f"/api/session/{sid}", patch)
        return self.get_session(sid)

    def delete_session(self, sid):
        self.request("DELETE", f"/api/session/{sid}")

    def request_cancel(self, sid):
        return bool((self.request("POST", f"/api/session/{sid}/interrupt") or {}).get("interrupted"))

    def approve(self, sid, call_id, decision, note=None):
        if decision not in OPENCODE_REPLIES:
            raise RelayError(f"decision must be one of {', '.join(OPENCODE_REPLIES)}")
        for req in self._permissions(sid):
            if call_id in (req["id"], (req.get("source") or {}).get("id")):
                body = {"decision": OPENCODE_REPLIES[decision]}
                if note and decision == "deny":
                    body["message"] = str(note)[:2000]
                self.request("POST", f"/api/session/{req['sessionID']}/permission/{req['id']}/reply", body)
                return
        raise RelayError("no pending approval for that tool call")

    def reply_form(self, sid, form_id, answer):
        if not isinstance(answer, dict):
            raise RelayError("answer must be an object")
        self.request("POST", f"/api/session/{sid}/form/{form_id}/reply", {"answer": answer})

    def cancel_form(self, sid, form_id):
        self.request("DELETE", f"/api/session/{sid}/form/{form_id}")

    # ------------------------------------------------------------------ chat

    def _watch_shell(self, shell_id, cid, cwd, emit, done):
        """Streams a running command's output; OpenCode events only carry the shell's id."""
        cursor, sent = 0, 0
        while not done.is_set() and sent < MAX_LIVE_OUTPUT:
            try:
                out = self.request("GET", f"/api/shell/{shell_id}/output",
                                   query={"location[directory]": cwd, "cursor": str(cursor)})["data"]
            except (BackendError, KeyError, TypeError):
                return
            if out.get("output"):
                sent += len(out["output"])
                emit({"type": "tool_output", "call_id": cid, "text": out["output"]})
            cursor = out.get("cursor", cursor)
            done.wait(0.5)

    def chat(self, sid, text, emit, token):
        """Send a prompt and translate OpenCode's events until the run ends.

        The work belongs to the OpenCode service: if this connection goes away it simply
        carries on, and a reconnecting client reads the state back with get_session.
        """
        if not isinstance(text, str) or not text.strip():
            raise RelayError("empty message")
        info = self._info(sid)
        if sid in self.active():
            raise RelayError("this session is already generating a reply")
        cwd = (info.get("location") or {}).get("directory", "")
        # Subscribe first: events are not replayed, so none may fire before we listen.
        conn, resp = self._connect("GET", "/api/event", None, None, self.STREAM_TIMEOUT, token)
        try:
            self.request("POST", f"/api/session/{sid}/prompt", {"text": text})
        except BackendError:
            conn.close()
            raise
        emit({"type": "start", "session": sid, "index": 0, "provider": "opencode",
              "model": _oc_model(info.get("model")) or self.default_model()})

        tools, announced, approvals, asked, permission_calls, forms, shells = {}, set(), {}, set(), {}, set(), {}
        step, steps, started = None, 0, False
        status, error, finished = "done", None, False
        posted = time.monotonic()

        def announce(cid, needs=False, save=None):
            tool = tools.get(cid) or {}
            if cid in announced and not needs:
                return
            announced.add(cid)
            emit({"type": "tool_call", "call_id": cid, "name": tool.get("name", ""), "arguments": tool.get("input") or {},
                  "needs_approval": needs, "save": save or []})

        def ask_permission(req):
            if req["id"] in asked:
                return
            asked.add(req["id"])
            view = self._approval(req, tools)
            permission_calls[req["id"]] = view["call_id"]
            tools.setdefault(view["call_id"], {"name": view["name"], "input": view["arguments"]})
            announce(view["call_id"], True, view["save"])

        def show_form(form):
            if form["id"] not in forms:
                forms.add(form["id"])
                emit({"type": "form", "form": form})

        def sync():
            for req in self._permissions(sid):
                ask_permission(req)
            for form in self._forms(sid):
                show_form(form)

        def handle(ev):
            nonlocal step, steps, started, status, error, finished
            kind, d = ev.get("type", ""), ev.get("data") or {}
            owner = d.get("sessionID")
            if kind == "permission.asked" and self._belongs(sid, owner):
                ask_permission(d)
            elif kind == "permission.replied" and d.get("requestID") in permission_calls:
                cid = permission_calls[d["requestID"]]
                approvals[cid] = OPENCODE_DECISIONS.get(d.get("reply"), "allow")
                emit({"type": "approval_resolved", "call_id": cid})
            elif kind == "form.created" and self._belongs(sid, (d.get("form") or {}).get("sessionID")):
                show_form(d["form"])
            elif kind in ("form.replied", "form.cancelled") and d.get("id") in forms:
                emit({"type": "form_resolved", "form_id": d["id"]})
            elif kind == "session.tool.input.started":
                tools.setdefault(d.get("id"), {})["name"] = d.get("name")
            elif kind == "session.tool.called":
                tools.setdefault(d.get("id"), {})["input"] = d.get("input")
            if owner != sid:
                return
            if kind == "session.execution.started":
                started = True
            elif kind == "session.step.started":
                started = True
                if d.get("assistantMessageID") != step:
                    if step is not None:
                        steps += 1
                        emit({"type": "turn", "index": steps})
                    step = d.get("assistantMessageID")
            elif kind == "session.text.delta":
                emit({"type": "delta", "text": d.get("delta", "")})
            elif kind == "session.reasoning.delta":
                emit({"type": "thinking", "text": d.get("delta", "")})
            elif kind == "session.tool.called":
                announce(d.get("id"))
            elif kind == "session.tool.progress":
                shell = (d.get("metadata") or {}).get("shellID")
                if shell and d.get("id") not in shells:
                    shells[d["id"]] = threading.Event()
                    threading.Thread(target=self._watch_shell, daemon=True,
                                     args=(shell, d["id"], cwd, emit, shells[d["id"]])).start()
            elif kind in ("session.tool.success", "session.tool.failed"):
                cid = d.get("id")
                announce(cid)
                if cid in shells:
                    shells[cid].set()
                ok = kind.endswith("success")
                state = {"status": "completed" if ok else "error", "content": d.get("content"), "error": d.get("error")}
                emit({"type": "tool_result", "call_id": cid, "ok": ok, "approval": approvals.get(cid, "auto"),
                      "output": truncate(_oc_tool_output(state), 4000)})
            elif kind == "session.execution.succeeded":
                finished = True
            elif kind == "session.execution.failed":
                status, error, finished = "error", (d.get("error") or {}).get("message", "failed"), True
            elif kind == "session.execution.interrupted":
                status, finished = "cancelled", True

        try:
            while not finished:
                try:
                    for line in iter_lines(resp, token):
                        if line.startswith("data:"):
                            try:
                                handle(json.loads(line[5:]))
                            except ValueError:
                                continue
                            if finished:
                                break
                except BackendError:
                    pass  # silence or a dropped stream: re-open below and catch up
                conn.close()
                if finished:
                    break
                if token.cancelled:
                    raise Cancelled()
                try:
                    conn, resp = self._connect("GET", "/api/event", None, None, self.STREAM_TIMEOUT, token)
                    running = sid in self.active()
                    if running:
                        started = True
                        sync()
                    elif started or time.monotonic() - posted > 15:
                        raw = self._messages_raw(sid)
                        idle = next((m for m in reversed(raw) if m.get("type") == "idle"), {})
                        if idle.get("outcome") == "failed":
                            status, error = "error", (idle.get("error") or {}).get("message", "failed")
                        elif idle.get("outcome") == "interrupted":
                            status = "cancelled"
                        finished = True
                except BackendError:
                    if time.monotonic() - posted > 3600:
                        raise
                    token._event.wait(2)
        except Cancelled:
            return None  # this client left; OpenCode keeps working
        except BackendError as e:
            status, error = "error", str(e)
        finally:
            conn.close()
            for done in shells.values():
                done.set()

        session = self.get_session(sid)
        replies = [m for m in session["messages"] if m["role"] == "assistant"]
        message = replies[-1] if replies else None
        emit({"type": "end", "status": status, "error": error, "index": steps, "message": message})
        return message


# --------------------------------------------------------------------------- engine


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = SessionStore(cfg["sessions_dir"])
        timeout = float(cfg.get("request_timeout", 900))
        self.providers = {}
        for name, pcfg in cfg.get("providers", {}).items():
            cls = PROVIDER_TYPES.get(pcfg.get("type"))
            if cls is None:
                raise RelayError(f"provider {name!r}: unknown type {pcfg.get('type')!r} "
                                 f"(expected one of {', '.join(PROVIDER_TYPES)})")
            self.providers[name] = cls(name, pcfg, timeout)
        if not self.providers:
            raise RelayError("no providers configured")
        self._opencode = None

    @property
    def uses_opencode(self):
        return self.cfg.get("engine") == "opencode"

    @property
    def opencode(self):
        if self._opencode is None:
            self._opencode = OpenCode(self.cfg.get("opencode"))
        return self._opencode

    @property
    def local_engine(self):
        return self.cfg.get("local_engine") or "builtin"

    @property
    def engines(self):
        """Engines new sessions can use; the app offers a choice when there are several."""
        if self.uses_opencode:
            return ["opencode", self.local_engine]
        return [self.cfg.get("engine") or "builtin"]

    def list_sessions(self):
        """Saved sessions of every engine, newest activity first."""
        result = {"sessions": self.store.list()}
        if self.uses_opencode:
            try:
                result["sessions"] = result["sessions"] + self.opencode.list_sessions()
            except BackendError as e:
                result["warning"] = f"OpenCode: {e}"
            result["sessions"].sort(key=lambda s: s.get("updated") or 0, reverse=True)
        return result

    def get_session(self, sid):
        if is_opencode_id(sid):
            return self.opencode.get_session(sid)
        session = self.store.load(sid)
        session["generating"] = is_generating(session)
        return session

    def reply_form(self, sid, form_id, answer):
        self.opencode.reply_form(sid, form_id, answer)

    def cancel_form(self, sid, form_id):
        self.opencode.cancel_form(sid, form_id)

    def provider(self, name=None):
        name = name or self.cfg.get("default_provider") or next(iter(self.providers))
        try:
            return self.providers[name]
        except KeyError:
            raise RelayError(f"unknown provider: {name!r}") from None

    def default_model(self, provider):
        model = provider.cfg.get("default_model")
        if model:
            return model
        models = provider.list_models()
        if not models:
            raise BackendError(f"provider {provider.name!r} has no models installed")
        return models[0]

    def info(self):
        return {
            "version": VERSION,
            "protocol": PROTOCOL_VERSION,
            "host": socket.gethostname(),
            "default_provider": self.provider().name,
            "engine": self.cfg.get("engine") or "builtin",
            "engines": self.engines,
            "providers": {n: {"type": p.cfg["type"], "default_model": p.cfg.get("default_model") or None}
                          for n, p in self.providers.items()},
        }

    def list_models(self, provider=None):
        if provider == "opencode" or (provider is None and self.uses_opencode):
            return self.opencode.list_models()
        p = self.provider(provider)
        return {"provider": p.name, "models": p.list_models()}

    @property
    def agent_cfg(self):
        return self.cfg.get("agent") or DEFAULT_CONFIG["agent"]

    @property
    def pi_cfg(self):
        return {**DEFAULT_CONFIG["pi"], **(self.cfg.get("pi") or {})}

    def new_session(self, title=None, provider=None, model=None, system=None, options=None, agent=None,
                    engine=None):
        if engine is not None and engine not in self.engines:
            raise RelayError(f"engine must be one of {', '.join(self.engines)}")
        if self.uses_opencode and provider in (None, "opencode") and engine in (None, "opencode"):
            return self.opencode.new_session(title, model)
        p = self.provider(provider)
        now = time.time()
        session = {
            "id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "title": title or "",
            "created": now,
            "updated": now,
            "provider": p.name,
            "model": model or self.default_model(p),
            "system": system,
            "options": options or {},
            "agent": bool(self.agent_cfg.get("enabled")) if agent is None else bool(agent),
            "permission_mode": self.agent_cfg.get("permission_mode", "auto-read"),
            "cwd": self._default_cwd(),
            "engine": self.local_engine if self.uses_opencode else (self.cfg.get("engine") or "builtin"),
            "messages": [],
        }
        with self.store.lock:
            self.store.save(session)
        return session

    def _default_cwd(self):
        cwd = os.path.expanduser(self.agent_cfg.get("workdir") or "~")
        return cwd if os.path.isdir(cwd) else os.path.expanduser("~")

    def update_session(self, sid, **changes):
        if is_opencode_id(sid):
            return self.opencode.update_session(sid, **changes)
        allowed = {"title", "provider", "model", "system", "options", "archived",
                   "agent", "permission_mode", "cwd", "allowed_tools"}
        with self.store.lock:
            session = self.store.load(sid)
            for key, value in changes.items():
                if key == "_internal":
                    continue
                if key not in allowed:
                    raise RelayError(f"cannot change {key!r}")
                if key == "provider":
                    self.provider(value)
                if key == "title":
                    if not isinstance(value, str):
                        raise RelayError("title must be a string")
                    value = " ".join(value.split())[:200]
                if key in ("archived", "agent") and not isinstance(value, bool):
                    raise RelayError(f"{key} must be true or false")
                if key == "permission_mode" and value not in PERMISSION_MODES:
                    raise RelayError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
                if key == "cwd":
                    if not isinstance(value, str):
                        raise RelayError("cwd must be a string")
                    if session.get("engine") == "pi" and session.get("messages") and not changes.get("_internal"):
                        raise RelayError("a Pi session keeps the folder it started in; start a new session to work elsewhere")
                    base = session.get("cwd") or self._default_cwd()
                    value = os.path.normpath(os.path.join(base, os.path.expanduser(value)))
                    if not os.path.isdir(value):
                        raise RelayError(f"not a directory: {value}")
                if key == "allowed_tools" and not (isinstance(value, list) and all(isinstance(t, str) for t in value)):
                    raise RelayError("allowed_tools must be a list of tool names")
                session[key] = value
            # Renaming or archiving is not activity: keep the session's place in the list.
            self.store.save(session, touch=False)
        return session

    def _system_prompt(self, session):
        system = session.get("system")
        if system is None:
            system = self.cfg.get("system_prompt")
        if session.get("agent"):
            agent = AGENT_PROMPT.format(
                host=socket.gethostname(), user=getpass.getuser(),
                os=f"{platform.system()} {platform.release()} {platform.machine()}",
                cwd=session.get("cwd") or self._default_cwd(), date=time.strftime("%Y-%m-%d"),
            )
            system = f"{system}\n\n{agent}" if system else agent
        return system

    def _history(self, session, upto):
        """Messages before index `upto`, in OpenAI chat format."""
        system = self._system_prompt(session)
        msgs = [{"role": "system", "content": system}] if system else []
        stored = session["messages"][:upto]
        answered = {m.get("tool_call_id") for m in stored if m.get("role") == "tool"}
        for m in stored:
            role = m.get("role")
            if role == "user" and m.get("content"):
                msgs.append({"role": "user", "content": m["content"]})
            elif role == "assistant":
                calls = m.get("tool_calls") or []
                if not m.get("content") and not calls:
                    continue
                msg = {"role": "assistant", "content": m.get("content") or None}
                if calls:
                    msg["tool_calls"] = calls
                msgs.append(msg)
                # A reply interrupted between a call and its result: every call needs a result.
                for c in calls:
                    if c["id"] not in answered:
                        msgs.append({"role": "tool", "tool_call_id": c["id"], "content": "[no result: interrupted]"})
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": m.get("tool_call_id"), "content": m.get("content", "")})
        return msgs

    @staticmethod
    def _placeholder(model):
        return {"role": "assistant", "content": "", "ts": time.time(), "model": model, "partial": True}

    def chat(self, sid, text, emit, token):
        """Run one user message to completion: model turns, and tool calls in agent sessions.

        Everything is saved as it happens, and the work continues if the client disconnects,
        so a reconnecting client finds the result (or a pending approval) in the session.
        """
        if is_opencode_id(sid):
            return self.opencode.chat(sid, text, emit, token)
        if not isinstance(text, str) or not text.strip():
            raise RelayError("empty message")
        with self.store.lock:
            session = self.store.load(sid)
            if is_generating(session):
                raise RelayError("this session is already generating a reply")
            provider = self.provider(session.get("provider"))
            model = session.get("model") or self.default_model(provider)
            session["messages"].append({"role": "user", "content": text, "ts": time.time()})
            session["messages"].append(self._placeholder(model))
            if not session.get("title"):
                session["title"] = make_title(text)
            session["generating"] = {"pid": os.getpid(), "since": time.time()}
            session.pop("cancel_requested", None)
            session.pop("pending_approval", None)
            self.store.save(session)
            index = len(session["messages"]) - 1

        emit({"type": "start", "session": sid, "index": index, "model": model, "provider": provider.name})
        stop = StopCheck(self.store, sid, token)
        if session.get("engine") == "pi":
            status, error, index = self._pi_chat(sid, index, text, provider, model, emit, stop)
            message = self._finish(sid, index, status, error)
            emit({"type": "end", "status": status, "error": error, "index": index, "message": message})
            return message
        max_steps = int(self.agent_cfg.get("max_steps", 40))
        steps = 0
        while True:
            steps += 1
            status, error, calls = self._model_turn(sid, index, provider, model, emit, token, stop)
            if status != "done" or not calls:
                break
            status = self._run_tools(sid, calls, emit, stop)
            if status != "done":
                break
            if steps >= max_steps:
                status, error = "error", f"stopped after {max_steps} steps (agent.max_steps)"
                break
            with self.store.lock:
                session = self.store.load(sid)
                session["messages"].append(self._placeholder(model))
                self.store.save(session)
                index = len(session["messages"]) - 1
            emit({"type": "turn", "index": index})

        message = self._finish(sid, index, status, error)
        emit({"type": "end", "status": status, "error": error, "index": index, "message": message})
        return message

    def _model_turn(self, sid, index, provider, model, emit, token, stop):
        """Stream one model response into messages[index]. Returns (status, error, tool_calls)."""
        with self.store.lock:
            session = self.store.load(sid)
        history = self._history(session, index)
        tools = TOOL_SPECS if session.get("agent") else None
        content, thinking, calls, stats = [], [], [], {}
        status, error = "done", None
        started = last_save = time.monotonic()
        first_token = None
        interval = float(self.cfg.get("partial_save_interval", 2.0))
        try:
            for kind, value in provider.stream_chat(model, history, session.get("options") or {}, token, tools=tools):
                if first_token is None and kind in ("content", "thinking", "tool_call"):
                    first_token = time.monotonic()
                if kind == "content":
                    content.append(value)
                    emit({"type": "delta", "text": value})
                elif kind == "thinking":
                    thinking.append(value)
                    emit({"type": "thinking", "text": value})
                elif kind == "tool_call":
                    calls.append(value)
                elif kind == "stats":
                    stats.update(value)
                if stop():
                    raise Cancelled()
                if time.monotonic() - last_save >= interval:
                    self._store_reply(sid, index, "".join(content), "".join(thinking))
                    last_save = time.monotonic()
        except Cancelled:
            status = "cancelled"
        except BackendError as e:
            status, error = "error", str(e)
        except Exception as e:  # keep the session consistent whatever happens
            status, error = "error", f"{type(e).__name__}: {e}"

        stats["wall_sec"] = round(time.monotonic() - started, 2)
        if first_token is not None:
            stats["ttft_sec"] = round(first_token - started, 2)
        tool_calls = [{"id": c["id"], "type": "function",
                       "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                      for c in calls] if status == "done" else []
        self._store_reply(sid, index, "".join(content), "".join(thinking), final=True,
                          status=status, error=error, stats=stats, tool_calls=tool_calls)
        return status, error, tool_calls

    def _needs_approval(self, session, name, known=TOOL_NAMES):
        if known is not None and name not in known:
            return False  # fails without running anything
        mode = session.get("permission_mode") or self.agent_cfg.get("permission_mode", "auto-read")
        if mode == "auto" or (mode == "auto-read" and name in READ_ONLY_TOOLS):
            return False
        return name not in (session.get("allowed_tools") or [])

    def _run_tools(self, sid, calls, emit, stop):
        """Run (or refuse) each tool call and store its result. Returns "done" or "cancelled"."""
        status = "done"
        for call in calls:
            cid, name = call["id"], call["function"]["name"]
            raw = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = None
            if status != "done":
                self._add_tool_result(sid, cid, name, False, "[not run: the user stopped the reply]", "cancelled")
                continue
            with self.store.lock:
                session = self.store.load(sid)
            needs = isinstance(args, dict) and self._needs_approval(session, name)
            emit({"type": "tool_call", "call_id": cid, "name": name,
                  "arguments": args if isinstance(args, dict) else raw, "needs_approval": needs})
            approval = "auto"
            if not isinstance(args, dict):
                ok, output = False, f"Invalid tool arguments (expected a JSON object): {raw[:500]}"
            else:
                if needs:
                    approval, note = self._await_approval(sid, cid, name, args, stop)
                if approval == "cancelled":
                    status = "cancelled"
                    ok, output = False, "[not run: the user stopped the reply]"
                elif approval == "deny":
                    ok, output = False, "The user denied this tool call." + (f" They said: {note}" if note else "")
                elif approval == "timeout":
                    ok, output = False, "Nobody approved this tool call in time, so it was not run."
                else:
                    cwd = session.get("cwd") or self._default_cwd()
                    if not os.path.isdir(cwd):
                        cwd = os.path.expanduser("~")
                    runner = ToolRunner(cwd, stop, confine=self.agent_cfg.get("confine_to"),
                                        on_output=lambda text, cid=cid: emit(
                                            {"type": "tool_output", "call_id": cid, "text": text}))
                    ok, output = runner.run(name, args)
                    if runner.cwd != cwd:
                        self.update_session(sid, cwd=runner.cwd, _internal=True)
                    if stop():
                        status = "cancelled"
            self._add_tool_result(sid, cid, name, ok, output, approval)
            emit({"type": "tool_result", "call_id": cid, "ok": ok, "approval": approval,
                  "output": truncate(output, 4000)})
        return status

    def _add_tool_result(self, sid, cid, name, ok, output, approval):
        with self.store.lock:
            try:
                session = self.store.load(sid)
            except RelayError:
                return
            session["messages"].append({"role": "tool", "tool_call_id": cid, "name": name, "content": output,
                                        "ok": ok, "approval": approval, "ts": time.time()})
            self.store.save(session)

    def _await_approval(self, sid, cid, name, args, stop):
        """Publish a pending approval in the session and wait for any connection to decide.

        Returns (decision, note): allow, allow_session, deny, timeout or cancelled.
        """
        with self.store.lock:
            session = self.store.load(sid)
            session["pending_approval"] = {"call_id": cid, "name": name, "arguments": args, "since": time.time()}
            self.store.save(session, touch=False)
        deadline = time.monotonic() + float(self.agent_cfg.get("approval_timeout", 3600))
        while True:
            cancelled = stop()
            with self.store.lock:
                try:
                    session = self.store.load(sid)
                except RelayError:
                    return "cancelled", None
                pending = session.get("pending_approval") or {}
                decision = pending.get("decision") if pending.get("call_id") == cid else None
                if cancelled:
                    decision = "cancelled"
                elif decision is None and time.monotonic() > deadline:
                    decision = "timeout"
                if decision:
                    session.pop("pending_approval", None)
                    if decision == "allow_session":
                        allowed = session.setdefault("allowed_tools", [])
                        if name not in allowed:
                            allowed.append(name)
                    self.store.save(session, touch=False)
                    return decision, pending.get("note")
            time.sleep(0.3)

    def approve(self, sid, call_id, decision, note=None):
        if is_opencode_id(sid):
            return self.opencode.approve(sid, call_id, decision, note)
        if decision not in APPROVAL_DECISIONS:
            raise RelayError(f"decision must be one of {', '.join(APPROVAL_DECISIONS)}")
        with self.store.lock:
            session = self.store.load(sid)
            pending = session.get("pending_approval")
            if not pending or pending.get("call_id") != call_id:
                raise RelayError("no pending approval for that tool call")
            pending["decision"] = decision
            if note:
                pending["note"] = str(note)[:2000]
            self.store.save(session, touch=False)

    def request_cancel(self, sid):
        """Ask whichever process is generating in this session to stop. Returns False if idle."""
        if is_opencode_id(sid):
            return self.opencode.request_cancel(sid)
        with self.store.lock:
            session = self.store.load(sid)
            if not is_generating(session):
                return False
            session["cancel_requested"] = True
            self.store.save(session, touch=False)
        return True

    # ------------------------------------------------------------------ Pi

    def _pi_command(self, session, provider, model):
        pc = self.pi_cfg
        cmd = [os.path.expanduser(c) for c in pc["command"]]
        session_dir = Path(pc["session_dir"]).expanduser()
        session_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["--mode", "rpc", "--session-dir", str(session_dir), "--session-id", session["id"],
                "-e", str(pi_extension_path())]
        name = pi_provider_name(provider)
        if name:
            cmd += ["--provider", name]
        cmd += ["--model", model]
        if pc.get("append_system_prompt"):
            cmd += ["--append-system-prompt", pc["append_system_prompt"]]
        if not session.get("agent"):
            cmd.append("--no-tools")
        return cmd + [str(a) for a in pc.get("args") or []]

    def _pi_env(self, provider):
        env = dict(os.environ)
        node_dir = os.path.dirname(os.path.expanduser(self.pi_cfg["command"][0]))
        if node_dir:
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")  # tools Pi starts get the same Node
        name = pi_provider_name(provider)
        var = provider.cfg.get("pi_key_env") or PI_KEY_ENV.get(name)
        if var and isinstance(provider, OpenAIProvider):
            key = provider._api_key()
            if key:
                env[var] = key
        return env

    def _pi_chat(self, sid, index, text, provider, model, emit, stop):
        """Run one message through `pi --mode rpc`; mirror its events into the session and emit().

        Returns (status, error, index of the last assistant message).
        """
        with self.store.lock:
            session = self.store.load(sid)
        cwd = session.get("cwd") or self._default_cwd()
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        try:
            proc = subprocess.Popen(self._pi_command(session, provider, model), cwd=cwd, env=self._pi_env(provider),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
        except OSError as e:
            return "error", f"cannot start Pi ({self.pi_cfg['command'][-1]}): {e}", index

        events, stderr_tail = queue.Queue(), []

        def read_stdout():
            for raw in proc.stdout:  # binary lines split on LF only, as Pi's RPC framing requires
                events.put(raw)
            events.put(None)

        def read_stderr():
            for raw in proc.stderr:
                stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
                del stderr_tail[:-20]

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        send_lock = threading.Lock()

        def send(obj):
            with send_lock:
                try:
                    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        send({"id": "prompt", "type": "prompt", "message": text})
        status, error = "done", None
        content, thinking = [], []
        turns, announced, approvals, live = 0, set(), {}, {}
        turn_started = first_token = None
        last_save = time.monotonic()
        interval = float(self.cfg.get("partial_save_interval", 2.0))
        abort_deadline = None
        settled = False
        try:
            while not settled:
                if abort_deadline is None and stop():
                    send({"id": "abort", "type": "abort"})
                    status = "cancelled"
                    abort_deadline = time.monotonic() + float(self.pi_cfg.get("abort_grace", 10))
                if abort_deadline is not None and time.monotonic() > abort_deadline:
                    break
                try:
                    raw = events.get(timeout=0.3)
                except queue.Empty:
                    continue
                if raw is None:  # Pi exited
                    if status == "done":
                        status = "error"
                        error = "Pi exited unexpectedly: " + (" | ".join(stderr_tail[-3:]) or "no output")
                    break
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                kind = ev.get("type")

                if kind == "response" and ev.get("id") == "prompt" and ev.get("success") is False:
                    status, error = "error", f"Pi rejected the message: {ev.get('error')}"
                    break
                elif kind == "turn_start":
                    if turns:  # every turn after the first gets a new assistant message
                        with self.store.lock:
                            s = self.store.load(sid)
                            s["messages"].append(self._placeholder(model))
                            self.store.save(s)
                            index = len(s["messages"]) - 1
                        emit({"type": "turn", "index": index})
                    turns += 1
                    content, thinking = [], []
                    turn_started, first_token = time.monotonic(), None
                elif kind == "message_update":
                    part = ev.get("assistantMessageEvent") or {}
                    if part.get("type") in ("text_delta", "thinking_delta") and first_token is None:
                        first_token = time.monotonic()
                    if part.get("type") == "text_delta":
                        content.append(part.get("delta", ""))
                        emit({"type": "delta", "text": part.get("delta", "")})
                    elif part.get("type") == "thinking_delta":
                        thinking.append(part.get("delta", ""))
                        emit({"type": "thinking", "text": part.get("delta", "")})
                    if time.monotonic() - last_save >= interval:
                        self._store_reply(sid, index, "".join(content), "".join(thinking))
                        last_save = time.monotonic()
                elif kind == "message_end" and (ev.get("message") or {}).get("role") == "assistant":
                    msg = ev["message"]
                    body, thoughts, calls = pi_assistant(msg)
                    # The final message is authoritative, but keep what streamed if it left something out.
                    body, thoughts = body or "".join(content), thoughts or "".join(thinking)
                    usage = msg.get("usage") or {}
                    stats = {"prompt_eval_count": usage.get("input"), "eval_count": usage.get("output"),
                             "done_reason": msg.get("stopReason")}
                    if (usage.get("cost") or {}).get("total") is not None:
                        stats["cost_usd"] = usage["cost"]["total"]
                    now = time.monotonic()
                    if turn_started is not None:
                        stats["wall_sec"] = round(now - turn_started, 2)
                        if first_token is not None:
                            stats["ttft_sec"] = round(first_token - turn_started, 2)
                            if usage.get("output") and now > first_token:
                                stats["tokens_per_sec"] = round(usage["output"] / (now - first_token), 2)
                    turn_status, turn_error = "done", None
                    if msg.get("stopReason") == "error":
                        turn_status, turn_error = "error", msg.get("errorMessage") or "model error"
                    elif msg.get("stopReason") == "aborted":
                        turn_status = "cancelled"
                    self._store_reply(sid, index, body, thoughts, final=True, status=turn_status,
                                      error=turn_error, stats=stats, tool_calls=calls)
                    if turn_status == "error":
                        status, error = "error", turn_error
                    elif status == "error":
                        status, error = "done", None  # Pi retried successfully
                elif kind == "extension_ui_request":
                    self._pi_ui_request(ev, sid, emit, stop, send, announced, approvals)
                elif kind == "tool_execution_update":
                    cid = ev.get("toolCallId")
                    textnow = _pi_text((ev.get("partialResult") or {}).get("content"))
                    before = live.get(cid, "")
                    if textnow.startswith(before) and len(textnow) > len(before):
                        emit({"type": "tool_output", "call_id": cid, "text": textnow[len(before):]})
                        live[cid] = textnow
                elif kind == "tool_execution_end":
                    cid, name = ev.get("toolCallId"), ev.get("toolName")
                    if (cid or "").count("/"):
                        continue  # nested call made by another tool: not part of the transcript
                    if cid not in announced:
                        emit({"type": "tool_call", "call_id": cid, "name": name, "arguments": ev.get("args") or {},
                              "needs_approval": False})
                        announced.add(cid)
                    output = _pi_text((ev.get("result") or {}).get("content"))
                    ok = not ev.get("isError")
                    approval = approvals.pop(cid, "auto")
                    self._add_tool_result(sid, cid, name, ok, output, approval)
                    emit({"type": "tool_result", "call_id": cid, "ok": ok, "approval": approval,
                          "output": truncate(output, 4000)})
                elif kind == "agent_settled":
                    settled = True
                    if ev.get("aborted"):
                        status = "cancelled"
                elif kind == "extension_error":
                    stderr_tail.append(f"extension error: {ev.get('error')}")
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
        return status, error, index

    def _pi_ui_request(self, ev, sid, emit, stop, send, announced, approvals):
        """Answer Pi's dialogs: our extension's approval questions; anything else is cancelled."""
        method, rid = ev.get("method"), ev.get("id")
        if method not in ("select", "confirm", "input", "editor"):
            return  # notify, setStatus, ...: nothing to answer
        try:
            request = json.loads(ev.get("title") or "") if method == "input" else None
        except ValueError:
            request = None
        if not (isinstance(request, dict) and request.get("lllm") == "approve"):
            send({"type": "extension_ui_response", "id": rid, "cancelled": True})
            return
        cid, name, args = request.get("id"), request.get("tool"), request.get("input") or {}
        with self.store.lock:
            session = self.store.load(sid)
        needs = self._needs_approval(session, name, known=None)  # Pi's tools: everything is gated
        emit({"type": "tool_call", "call_id": cid, "name": name, "arguments": args, "needs_approval": needs})
        announced.add(cid)
        decision, note = ("auto", None)
        if needs:
            decision, note = self._await_approval(sid, cid, name, args, stop)
        approvals[cid] = decision
        if decision in ("auto", "allow", "allow_session"):
            value = "allow"
        elif decision == "deny":
            value = "deny:The user denied this tool call." + (f" They said: {note}" if note else "")
        elif decision == "timeout":
            value = "deny:Nobody approved this tool call in time, so it was not run."
        else:
            value = "deny:The user stopped the reply."
        send({"type": "extension_ui_response", "id": rid, "value": value})

    def delete_session(self, sid):
        if is_opencode_id(sid):
            return self.opencode.delete_session(sid)
        with self.store.lock:
            self.store.delete(sid)
        pi_dir = Path(self.pi_cfg["session_dir"]).expanduser()
        if pi_dir.is_dir():
            for f in pi_dir.rglob(f"*{sid}*.jsonl"):
                try:
                    f.unlink()
                except OSError:
                    pass

    def _store_reply(self, sid, index, content, thinking, final=False, status=None, error=None, stats=None,
                     tool_calls=None):
        with self.store.lock:
            try:
                session = self.store.load(sid)
            except RelayError:  # deleted while generating
                return None
            msgs = session["messages"]
            if index >= len(msgs) or msgs[index].get("role") != "assistant":
                return None
            msg = msgs[index]
            msg["content"] = content
            if thinking:
                msg["thinking"] = thinking
            if final:
                msg.pop("partial", None)
                if status != "done":
                    msg["status"] = status
                if error:
                    msg["error"] = error
                if stats:
                    msg["stats"] = stats
                if tool_calls:
                    msg["tool_calls"] = tool_calls
            self.store.save(session, durable=final)
            return msg

    def _finish(self, sid, index, status, error):
        with self.store.lock:
            try:
                session = self.store.load(sid)
            except RelayError:
                return None
            for key in ("generating", "cancel_requested", "pending_approval"):
                session.pop(key, None)
            msg = session["messages"][index] if index < len(session["messages"]) else None
            if msg is not None and msg.pop("partial", None) and status == "done":
                status = "error"  # the engine ended without finishing this message
                error = error or "the reply ended unexpectedly"
            if msg is not None and status != "done" and not msg.get("status"):
                msg["status"] = status
                if error:
                    msg["error"] = error
            self.store.save(session)
            return msg


# --------------------------------------------------------------------------- serve (JSON lines)


class Server:
    """Handles one client connection over stdin/stdout."""

    def __init__(self, engine, inp, out):
        self.engine = engine
        self.inp = inp
        self.out = out
        self.out_lock = threading.Lock()
        self.detached = False
        self.active = {}  # request id -> (CancelToken, session id)
        self.active_lock = threading.Lock()

    def send(self, obj):
        if self.detached:
            return
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self.out_lock:
            try:
                self.out.write(data)
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                self.detached = True

    def run(self):
        self.send({"type": "hello", **self.engine.info()})
        for raw in self.inp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("request must be a JSON object")
            except ValueError as e:
                self.send({"type": "error", "id": None, "error": f"bad request: {e}"})
                continue
            self.dispatch(req)
        # Client went away. Running generations keep going (non-daemon threads)
        # so their replies are saved; output is simply dropped.
        self.detached = True
        # OpenCode runs on in its own service, so there is nothing to wait for.
        with self.active_lock:
            tokens = [t for t, sid in self.active.values() if is_opencode_id(sid)]
        for t in tokens:
            t.cancel()

    def dispatch(self, req):
        rid, op = req.get("id"), req.get("op")
        remote = is_opencode_id(req.get("session")) or (op == "list_sessions" and self.engine.uses_opencode)
        if op in ("chat", "list_models", "new_session") or remote:  # may block on the backend
            threading.Thread(target=self._run_op, args=(rid, op, req), daemon=(op != "chat")).start()
        else:
            self._run_op(rid, op, req)

    def _run_op(self, rid, op, req):
        try:
            handler = getattr(self, f"op_{op}", None) if isinstance(op, str) else None
            if handler is None:
                raise RelayError(f"unknown op: {op!r}")
            result = handler(rid, req)
            if result is not None:
                self.send({"type": "result", "id": rid, "data": result})
        except (RelayError, BackendError) as e:
            self.send({"type": "error", "id": rid, "error": str(e)})
        except Exception as e:
            self.send({"type": "error", "id": rid, "error": f"internal error: {type(e).__name__}: {e}"})

    # Each op returns the result payload, or None if it already replied via events.

    def op_ping(self, rid, req):
        return {"pong": time.time()}

    def op_info(self, rid, req):
        return self.engine.info()

    def op_list_models(self, rid, req):
        return self.engine.list_models(req.get("provider"))

    def op_list_sessions(self, rid, req):
        return self.engine.list_sessions()

    def op_new_session(self, rid, req):
        s = self.engine.new_session(req.get("title"), req.get("provider"), req.get("model"),
                                    req.get("system"), req.get("options"), req.get("agent"), req.get("engine"))
        return {"session": s}

    def op_list_dirs(self, rid, req):
        """Subfolders of `path` (default: home), for choosing a working folder."""
        path = req.get("path") or "~"
        if not isinstance(path, str):
            raise RelayError("path must be a string")
        path = os.path.abspath(os.path.expanduser(path))
        try:
            with os.scandir(path) as entries:
                dirs = sorted((e.name for e in entries if e.is_dir() and not e.name.startswith(".")), key=str.lower)
        except OSError as e:
            raise RelayError(f"cannot open {path}: {e.strerror}") from None
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent if parent != path else None, "dirs": dirs[:500]}

    def op_get_session(self, rid, req):
        return {"session": self.engine.get_session(req.get("session"))}

    def op_update_session(self, rid, req):
        changes = {k: v for k, v in req.items() if k not in ("id", "op", "session")}
        return {"session": self.engine.update_session(req.get("session"), **changes)}

    def op_delete_session(self, rid, req):
        self.engine.delete_session(req.get("session"))
        return {"deleted": req.get("session")}

    def op_chat(self, rid, req):
        token = CancelToken()
        with self.active_lock:
            self.active[rid] = (token, req.get("session"))
        try:
            self.engine.chat(req.get("session"), req.get("text"),
                             lambda ev: self.send({**ev, "id": rid}), token)
        finally:
            with self.active_lock:
                self.active.pop(rid, None)
        return None

    def op_cancel(self, rid, req):
        """Stop a reply: `target` (a chat request id on this connection) or `session` (from any connection)."""
        sid = req.get("session")
        if is_opencode_id(sid):  # the run ends through OpenCode's events, which the stream reports
            return {"cancelled": self.engine.request_cancel(sid)}
        with self.active_lock:
            tokens = [t for r, (t, s) in self.active.items() if r == req.get("target") or (sid and s == sid)]
        for t in tokens:
            t.cancel()
        flagged = self.engine.request_cancel(sid) if sid else False
        return {"cancelled": bool(tokens) or flagged}

    def op_approve(self, rid, req):
        self.engine.approve(req.get("session"), req.get("call_id"), req.get("decision"), req.get("note"))
        return {"ok": True}

    def op_form_reply(self, rid, req):
        self.engine.reply_form(req.get("session"), req.get("form_id"), req.get("answer"))
        return {"ok": True}

    def op_form_cancel(self, rid, req):
        self.engine.cancel_form(req.get("session"), req.get("form_id"))
        return {"ok": True}


def cmd_serve(args):
    # A dropped SSH connection must not kill a generation that is still being saved.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        engine = Engine(load_config())
    except (RelayError, ValueError, OSError) as e:
        sys.stdout.write(json.dumps({"type": "fatal", "error": str(e)}) + "\n")
        sys.stdout.flush()
        return 1
    Server(engine, sys.stdin.buffer, sys.stdout.buffer).run()
    return 0


# --------------------------------------------------------------------------- terminal commands

DIM, RESET = ("\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "")


def _print_history(session):
    for m in session["messages"]:
        if m["role"] == "tool":
            continue
        who = "you" if m["role"] == "user" else session.get("model", "assistant")
        note = f" [{m['status']}]" if m.get("status") else ""
        print(f"{DIM}{who}{note}:{RESET} {m.get('content', '')}\n")


def cmd_chat(args):
    engine = Engine(load_config())
    saved = engine.list_sessions()["sessions"] if args.last else []
    if args.session:
        session = engine.get_session(args.session)
    elif saved:
        session = engine.get_session(saved[0]["id"])
    else:
        session = engine.new_session(provider=args.provider, model=args.model)
    if args.model and session["model"] != args.model:
        session = engine.update_session(session["id"], model=args.model)
    _print_history(session)
    print(f"{DIM}session {session['id']} · {session['provider']}/{session['model']} · "
          f"/help for commands, Ctrl-C stops a reply, Ctrl-D quits{RESET}")

    def emit(ev):
        if ev["type"] == "delta":
            print(ev["text"], end="", flush=True)
        elif ev["type"] == "thinking":
            print(f"{DIM}{ev['text']}{RESET}", end="", flush=True)
        elif ev["type"] == "tool_call":
            print(f"\n● {ev['name']}({json.dumps(ev['arguments'], ensure_ascii=False)[:300]})")
            if ev["needs_approval"]:
                answer = input("  allow? [y]es / [a]lways this session / [n]o: ").strip().lower()
                decision = {"y": "allow", "a": "allow_session"}.get(answer[:1], "deny")
                engine.approve(session["id"], ev["call_id"], decision)
        elif ev["type"] == "tool_output":
            print(f"{DIM}{ev['text']}{RESET}", end="", flush=True)
        elif ev["type"] == "tool_result":
            mark = "" if ev["ok"] else " (failed)"
            print(f"{DIM}  ⎿ {ev['approval']}{mark}{RESET}")
        elif ev["type"] == "form":
            form, answer = ev["form"], {}
            for field in form.get("fields", []):
                print(f"\n? {field.get('description') or field.get('title') or field['key']}")
                for option in field.get("options") or []:
                    print(f"    {option['value']}")
                value = input("  answer: ").strip()
                answer[field["key"]] = value.split(",") if field.get("type") == "multiselect" else value
            engine.reply_form(form["sessionID"], form["id"], answer)
        elif ev["type"] == "end":
            stats = (ev.get("message") or {}).get("stats", {})
            tps = f" · {stats['tokens_per_sec']} tok/s" if stats.get("tokens_per_sec") else ""
            tail = f"[{ev['status']}] {ev.get('error') or ''}" if ev["status"] != "done" else ""
            print(f"\n{DIM}{tail}{stats.get('wall_sec', '?')}s{tps}{RESET}\n")

    while True:
        try:
            text = input("> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            continue
        if not text.strip():
            continue
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            arg = arg.strip()
            if cmd in ("quit", "exit"):
                return 0
            elif cmd == "models":
                print("\n".join(engine.list_models(session["provider"])["models"]))
            elif cmd == "model" and arg:
                session = engine.update_session(session["id"], model=arg)
                print(f"model: {arg}")
            elif cmd == "new":
                session = engine.new_session(provider=session["provider"], model=session["model"])
                print(f"new session {session['id']}")
            elif cmd == "sessions":
                for s in engine.list_sessions()["sessions"]:
                    print(f"{s['id']}  {s['title'] or '(untitled)'}")
            elif cmd == "load" and arg:
                session = engine.get_session(arg)
                _print_history(session)
            else:
                print("/models  /model NAME  /new  /sessions  /load ID  /quit")
            continue

        token = CancelToken()
        errors = []

        def run():
            try:
                engine.chat(session["id"], text, emit, token)
            except (RelayError, BackendError) as e:
                errors.append(e)

        worker = threading.Thread(target=run)
        worker.start()
        while worker.is_alive():
            try:
                worker.join(0.2)
            except KeyboardInterrupt:
                token.cancel()
        if errors:
            print(f"error: {errors[0]}")


def cmd_sessions(args):
    engine = Engine(load_config())
    result = engine.list_sessions()
    if result.get("warning"):
        print(f"warning: {result['warning']}", file=sys.stderr)
    for s in result["sessions"]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated"] or 0))
        flag = "  [archived]" if s["archived"] else ""
        count = f"{s['message_count']:>3} msgs" if s["message_count"] else "        "
        print(f"{s['id']}  {when}  {count}  {s['model']}  {s['title'] or '(untitled)'}{flag}")
    return 0


def cmd_doctor(args):
    print(f"config:   {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'not found, using defaults'})")
    try:
        engine = Engine(load_config())
    except (RelayError, ValueError) as e:
        print(f"error: {e}")
        return 1
    print(f"sessions: {engine.store.root} ({len(engine.store.list())} saved)")
    ok = True
    if engine.uses_opencode:
        try:
            oc = engine.opencode
            version = oc.request("GET", "/api/info").get("version")
            print(f"opencode: OK, v{version} at {oc.base_url()}, {len(oc.list_sessions())} sessions, "
                  f"{len(oc.list_models()['models'])} models, default {oc.default_model()}")
        except BackendError as e:
            ok = False
            print(f"opencode: FAILED: {e}")
    for name, p in engine.providers.items():
        try:
            models = p.list_models()
            print(f"provider {name} ({p.cfg['type']}, {p.base}): OK, {len(models)} models")
            for m in models:
                print(f"    {m}")
            default = p.cfg.get("default_model")
            if default and default not in models:
                print(f"    warning: default_model {default!r} is not installed")
        except BackendError as e:
            ok = False
            print(f"provider {name} ({p.cfg['type']}, {p.base}): FAILED: {e}")
    return 0 if ok else 1


def cmd_init_config(args):
    if CONFIG_PATH.exists() and not args.force:
        print(f"{CONFIG_PATH} already exists (use --force to overwrite)")
        return 1
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:  # pre-select an installed qwen model if Ollama is up
        models = OllamaProvider("ollama", cfg["providers"]["ollama"], 15).list_models()
        pick = next((m for m in models if "qwen" in m.lower()), models[0] if models else "")
        cfg["providers"]["ollama"]["default_model"] = pick
    except BackendError:
        pass
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {CONFIG_PATH}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lllm-relay", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"lllm-relay {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="speak the JSON-lines protocol on stdin/stdout (used by the app)")
    p = sub.add_parser("chat", help="chat in the terminal")
    p.add_argument("-s", "--session", help="resume this session id")
    p.add_argument("-l", "--last", action="store_true", help="resume the most recent session")
    p.add_argument("-m", "--model")
    p.add_argument("-p", "--provider")
    sub.add_parser("sessions", help="list saved sessions")
    sub.add_parser("doctor", help="check config and backend connectivity")
    p = sub.add_parser("init-config", help="write a default config file")
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    handler = {"serve": cmd_serve, "chat": cmd_chat, "sessions": cmd_sessions,
               "doctor": cmd_doctor, "init-config": cmd_init_config}[args.cmd]
    try:
        return handler(args) or 0
    except (RelayError, BackendError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
