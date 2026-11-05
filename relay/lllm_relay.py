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
import http.client
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

VERSION = "0.1.0"
PROTOCOL_VERSION = 1

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
}


class RelayError(Exception):
    """An error caused by the request (bad id, unknown session, ...)."""


class BackendError(Exception):
    """The LLM backend failed or could not be reached."""


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
        raise BackendError(f"cannot reach backend at {u.scheme}://{u.netloc}: {e}") from None
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
# A provider turns a list of {"role", "content"} messages into a stream of
# ("content", text), ("thinking", text) and ("stats", dict) tuples.


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

    def stream_chat(self, model, messages, opts, token):
        payload = {"model": model, "messages": messages, "stream": True}
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

    def stream_chat(self, model, messages, opts, token):
        payload = {"model": model, "messages": messages, "stream": True,
                   **self.cfg.get("params", {}), **opts.get("params", {})}
        if self.cfg.get("include_usage", True):
            payload["stream_options"] = {"include_usage": True}

        conn, resp = http_request("POST", f"{self.base}/chat/completions", payload,
                                  headers=self._headers(), timeout=self.timeout, token=token)
        stats = {}
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
                    if reasoning or delta.get("content"):
                        last = time.monotonic()
                        first = first or last
                    if reasoning:
                        yield "thinking", reasoning
                    if delta.get("content"):
                        yield "content", delta["content"]
                    if choice.get("finish_reason"):
                        stats["done_reason"] = choice["finish_reason"]
        finally:
            conn.close()
        if stats.get("eval_count") and first and last and last > first:
            stats["tokens_per_sec"] = round(stats["eval_count"] / (last - first), 2)
        yield "stats", stats


PROVIDER_TYPES = {"ollama": OllamaProvider, "openai": OpenAIProvider}


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

    def save(self, session, durable=True):
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
            "providers": {n: {"type": p.cfg["type"], "default_model": p.cfg.get("default_model") or None}
                          for n, p in self.providers.items()},
        }

    def list_models(self, provider=None):
        p = self.provider(provider)
        return {"provider": p.name, "models": p.list_models()}

    def new_session(self, title=None, provider=None, model=None, system=None, options=None):
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
            "messages": [],
        }
        with self.store.lock:
            self.store.save(session)
        return session

    def update_session(self, sid, **changes):
        allowed = {"title", "provider", "model", "system", "options"}
        with self.store.lock:
            session = self.store.load(sid)
            for key, value in changes.items():
                if key not in allowed:
                    raise RelayError(f"cannot change {key!r}")
                if key == "provider":
                    self.provider(value)
                session[key] = value
            self.store.save(session)
        return session

    def _history(self, session, upto):
        system = session.get("system")
        if system is None:
            system = self.cfg.get("system_prompt")
        msgs = [{"role": "system", "content": system}] if system else []
        for m in session["messages"][:upto]:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def chat(self, sid, text, emit, token):
        """Append a user message, stream the reply through emit(), and persist it.

        The reply is saved as it is produced, and the generation runs to completion
        even if the client disconnects, so a reconnecting client finds the full answer.
        """
        if not isinstance(text, str) or not text.strip():
            raise RelayError("empty message")
        with self.store.lock:
            session = self.store.load(sid)
            if is_generating(session):
                raise RelayError("this session is already generating a reply")
            provider = self.provider(session.get("provider"))
            model = session.get("model") or self.default_model(provider)
            now = time.time()
            session["messages"].append({"role": "user", "content": text, "ts": now})
            session["messages"].append({"role": "assistant", "content": "", "ts": now,
                                        "model": model, "partial": True})
            if not session.get("title"):
                session["title"] = make_title(text)
            session["generating"] = {"pid": os.getpid(), "since": now}
            self.store.save(session)
            index = len(session["messages"]) - 1
        history = self._history(session, index)

        emit({"type": "start", "session": sid, "index": index, "model": model,
              "provider": provider.name})
        content, thinking, stats = [], [], {}
        status, error = "done", None
        started = last_save = time.monotonic()
        first_token = None
        try:
            for kind, value in provider.stream_chat(model, history, session.get("options") or {}, token):
                if first_token is None and kind in ("content", "thinking"):
                    first_token = time.monotonic()
                if kind == "content":
                    content.append(value)
                    emit({"type": "delta", "text": value})
                elif kind == "thinking":
                    thinking.append(value)
                    emit({"type": "thinking", "text": value})
                elif kind == "stats":
                    stats.update(value)
                if token.cancelled:
                    raise Cancelled()
                interval = float(self.cfg.get("partial_save_interval", 2.0))
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
        message = self._store_reply(sid, index, "".join(content), "".join(thinking),
                                    final=True, status=status, error=error, stats=stats)
        emit({"type": "end", "status": status, "error": error, "index": index, "message": message})
        return message

    def _store_reply(self, sid, index, content, thinking, final=False, status=None, error=None, stats=None):
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
                session.pop("generating", None)
                if status != "done":
                    msg["status"] = status
                if error:
                    msg["error"] = error
                if stats:
                    msg["stats"] = stats
            self.store.save(session, durable=final)
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
        self.active = {}  # request id -> CancelToken
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

    def dispatch(self, req):
        rid, op = req.get("id"), req.get("op")
        if op in ("chat", "list_models", "new_session"):  # may block on the backend
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
        return {"sessions": self.engine.store.list()}

    def op_new_session(self, rid, req):
        s = self.engine.new_session(req.get("title"), req.get("provider"), req.get("model"),
                                    req.get("system"), req.get("options"))
        return {"session": s}

    def op_get_session(self, rid, req):
        s = self.engine.store.load(req.get("session"))
        s["generating"] = is_generating(s)
        return {"session": s}

    def op_update_session(self, rid, req):
        changes = {k: v for k, v in req.items() if k not in ("id", "op", "session")}
        return {"session": self.engine.update_session(req.get("session"), **changes)}

    def op_delete_session(self, rid, req):
        with self.engine.store.lock:
            self.engine.store.delete(req.get("session"))
        return {"deleted": req.get("session")}

    def op_chat(self, rid, req):
        token = CancelToken()
        with self.active_lock:
            self.active[rid] = token
        try:
            self.engine.chat(req.get("session"), req.get("text"),
                             lambda ev: self.send({**ev, "id": rid}), token)
        finally:
            with self.active_lock:
                self.active.pop(rid, None)
        return None

    def op_cancel(self, rid, req):
        with self.active_lock:
            token = self.active.get(req.get("target"))
        if token:
            token.cancel()
        return {"cancelled": token is not None}


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
        who = "you" if m["role"] == "user" else session.get("model", "assistant")
        note = f" [{m['status']}]" if m.get("status") else ""
        print(f"{DIM}{who}{note}:{RESET} {m.get('content', '')}\n")


def cmd_chat(args):
    engine = Engine(load_config())
    if args.session:
        session = engine.store.load(args.session)
    elif args.last and engine.store.list():
        session = engine.store.load(engine.store.list()[0]["id"])
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
                for s in engine.store.list():
                    print(f"{s['id']}  {s['title'] or '(untitled)'}")
            elif cmd == "load" and arg:
                session = engine.store.load(arg)
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
    for s in engine.store.list():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated"] or 0))
        print(f"{s['id']}  {when}  {s['message_count']:>3} msgs  {s['model']}  {s['title'] or '(untitled)'}")
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
