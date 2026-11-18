#!/usr/bin/env python3
"""A fake OpenCode service (the parts of its HTTP API the relay uses), for tests.

Prompts: "tool: NAME {json}" calls a tool, "sub: NAME {json}" does so from a sub-agent session,
"question" opens a form, "slow" streams until interrupted, "boom" fails, "drop" cuts the event
streams while it works, anything else is echoed. GET /test/state shows recorded replies.

    python3 mock_opencode.py PORT PASSWORD
"""
import base64
import fnmatch
import itertools
import json
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASSWORD = sys.argv[2] if len(sys.argv) > 2 else "test-pass"
lock = threading.RLock()
ids = itertools.count(1)
sessions, messages, permissions, forms, active, interrupts, subscribers, shells = {}, {}, {}, {}, set(), set(), [], {}
state = {"replies": [], "form_answers": [], "prompts": []}
ACTIONS = {"write": "edit", "edit": "edit"}
DEFAULT_MODEL = {"providerID": "opencode", "id": "default-model"}


def now():
    return int(time.time() * 1000)


def publish(kind, **data):
    with lock:
        for q in subscribers:
            q.put({"id": f"evt_{next(ids)}", "type": kind, "data": data})


def effect(session, action, resource):
    result = "allow"
    for rule in session.get("permissions") or []:
        if fnmatch.fnmatch(action, rule["action"]) and fnmatch.fnmatch(resource, rule["resource"]):
            result = rule["effect"]
    return result


class Interrupted(Exception):
    pass


def check(sid):
    if sid in interrupts:
        raise Interrupted()


def assistant(sid):
    m = {"id": f"msg_{next(ids)}", "type": "assistant", "time": {"created": now()}, "agent": "build",
         "model": sessions[sid].get("model") or DEFAULT_MODEL, "content": []}
    messages[sid].append(m)
    publish("session.step.started", sessionID=sid, assistantMessageID=m["id"])
    return m


def finish(m, reason):
    t = now()
    m["time"].update(firstToken=t - 100, streamed=t, completed=t)
    m.update(finish=reason, cost=0.01, tokens={"input": 10, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}})


def say(sid, m, text, delay=0.0):
    part = {"type": "text", "text": ""}
    m["content"].append(part)
    for word in text.split(" "):
        check(sid)
        part["text"] += word + " "
        publish("session.text.delta", sessionID=sid, assistantMessageID=m["id"], delta=word + " ")
        time.sleep(delay)


def wait_for(sid, event):
    while not event.wait(0.05):
        check(sid)


def run_tool(root, sid, m, name, args):
    cid = f"call_{next(ids)}"
    part = {"type": "tool", "id": cid, "name": name, "state": {"status": "running", "input": args, "metadata": {}},
            "time": {"created": now()}}
    m["content"].append(part)
    publish("session.tool.input.started", sessionID=sid, assistantMessageID=m["id"], id=cid, name=name)
    publish("session.tool.called", sessionID=sid, assistantMessageID=m["id"], id=cid, input=args)
    action = ACTIONS.get(name, name)
    resource = args.get("command") or args.get("path") or "*"
    rejected = None
    if effect(sessions[root], action, resource) == "ask":
        pid, done = f"per_{next(ids)}", threading.Event()
        request = {"id": pid, "sessionID": sid, "action": action, "resources": [resource], "save": ["*"],
                   "source": {"type": "tool", "messageID": m["id"], "id": cid}}
        permissions[pid] = {"request": request, "done": done, "reply": None}
        publish("permission.asked", **request)
        wait_for(root, done)
        body = permissions.pop(pid)["reply"]
        publish("permission.replied", sessionID=sid, requestID=pid, reply=body["decision"])
        if body["decision"] == "reject":
            rejected = body.get("message") or "rejected"
    if rejected:
        error = {"type": "permission", "message": rejected}
        part["state"] = {"status": "error", "input": args, "error": error}
        publish("session.tool.failed", sessionID=sid, assistantMessageID=m["id"], id=cid, error=error)
        return rejected
    output = f"ran {name}"
    if name == "shell":
        shell = f"sh_{next(ids)}"
        shells[shell] = "tick 1\n"
        publish("session.tool.progress", sessionID=sid, assistantMessageID=m["id"], id=cid, metadata={"shellID": shell})
        time.sleep(0.7)
        output = shells[shell]
    content = [{"type": "text", "text": output}]
    part["state"] = {"status": "completed", "input": args, "content": content}
    publish("session.tool.success", sessionID=sid, assistantMessageID=m["id"], id=cid, content=content)
    return output


def run(sid, text):
    outcome, error = "succeeded", None
    publish("session.execution.started", sessionID=sid)
    try:
        m = assistant(sid)
        if text.startswith(("tool: ", "sub: ")):
            kind, rest = text.split(": ", 1)
            name, _, raw = rest.partition(" ")
            actor = sid
            if kind == "sub":
                actor = new_session({"parentID": sid, "title": "child"})["id"]
            result = run_tool(sid, actor, m, name, json.loads(raw))
            finish(m, "tool-calls")
            m = assistant(sid)
            say(sid, m, f"result: {result}")
        elif text == "question":
            fid, done = f"frm_{next(ids)}", threading.Event()
            form = {"id": fid, "sessionID": sid, "title": "Questions", "metadata": {"kind": "question"}, "fields": [
                {"key": "q0", "type": "string", "description": "Red or blue?", "custom": True,
                 "options": [{"value": "Red", "label": "Red"}, {"value": "Blue", "label": "Blue"}]}]}
            forms[fid] = {"form": form, "done": done, "answer": None}
            publish("form.created", form=form)
            wait_for(sid, done)
            answer = forms.pop(fid)["answer"]
            if answer is None:
                publish("form.cancelled", id=fid, sessionID=sid)
            else:
                publish("form.replied", id=fid, sessionID=sid, answer=answer)
            say(sid, m, f"answer: {answer}")
        elif text == "slow":
            say(sid, m, " ".join(["word"] * 200), delay=0.05)
        elif text == "boom":
            raise RuntimeError("simulated failure")
        elif text == "drop":
            with lock:
                for q in subscribers:
                    q.put(None)
                subscribers.clear()
            time.sleep(0.5)
            say(sid, m, "finished unseen", delay=0.02)
        else:
            say(sid, m, f"oc-echo: {text}")
        finish(m, "stop")
    except Interrupted:
        outcome = "interrupted"
    except RuntimeError as e:
        outcome, error = "failed", {"type": "test", "message": str(e)}
    with lock:
        active.discard(sid)
        interrupts.discard(sid)
    idle = {"id": f"msg_{next(ids)}", "type": "idle", "time": {"created": now()}, "outcome": outcome}
    if error:
        idle["error"] = error
    messages[sid].append(idle)
    sessions[sid]["time"]["updated"] = now()
    if outcome == "succeeded":
        publish("session.execution.succeeded", sessionID=sid)
    elif outcome == "failed":
        publish("session.execution.failed", sessionID=sid, error=error)
    else:
        publish("session.execution.interrupted", sessionID=sid, reason="user")


def new_session(body):
    sid = f"ses_{next(ids)}"
    info = {"id": sid, "projectID": "p", "cost": 0, "time": {"created": now(), "updated": now()},
            "location": body.get("location") or {"directory": "/tmp"}, "permissions": body.get("permissions") or []}
    for key in ("title", "model", "parentID"):
        if body.get(key):
            info[key] = body[key]
    sessions[sid], messages[sid] = info, []
    return info


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status=200, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def events(self):
        q = queue.Queue()
        with lock:
            subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        try:
            while True:
                ev = q.get()
                if ev is None:
                    self.wfile.write(b"0\r\n\r\n")
                    self.close_connection = True
                    return
                data = f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode()
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
        except OSError:
            self.close_connection = True
        finally:
            with lock:
                if q in subscribers:
                    subscribers.remove(q)

    def handle_api(self, method):
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        parts = url.path.strip("/").split("/")
        if url.path == "/test/state":
            return self.reply(200, state)
        expected = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            return self.reply(401, {"_tag": "UnauthorizedError", "message": "Authentication required"})
        route = (method, url.path)
        if route == ("GET", "/api/event"):
            return self.events()
        if route == ("GET", "/api/info"):
            return self.reply(200, {"version": "9.9.9"})
        if route == ("GET", "/api/session/active"):
            return self.reply(200, {"data": {s: {"type": "running"} for s in active}})
        if route == ("GET", "/api/model"):
            return self.reply(200, {"data": [{**DEFAULT_MODEL, "type": "chat"}, {"providerID": "x", "id": "chat-2", "type": "chat"},
                                             {"providerID": "x", "id": "picture", "type": "image"}]})
        if route == ("GET", "/api/model/default"):
            return self.reply(200, {"data": DEFAULT_MODEL})
        if route == ("POST", "/api/session"):
            return self.reply(200, {"data": new_session(body)})
        if route == ("GET", "/api/session"):
            found = list(sessions.values())
            if query.get("parentID") == "null":
                found = [s for s in found if not s.get("parentID")]
            elif query.get("parentID"):
                found = [s for s in found if s.get("parentID") == query["parentID"]]
            wanted = query.get("archived") == "true"
            found = [s for s in found if bool(s["time"].get("archived")) == wanted]
            return self.reply(200, {"data": found, "cursor": {"next": None}})
        if parts[:2] == ["api", "shell"]:
            text, cursor = shells.get(parts[2], ""), int(query.get("cursor", 0))
            return self.reply(200, {"data": {"output": text[cursor:], "cursor": len(text), "size": len(text), "truncated": False}})
        if parts[:2] != ["api", "session"] or len(parts) < 3 or parts[2] not in sessions:
            return self.reply(404, {"_tag": "SessionNotFoundError", "message": "Session not found"})
        sid, rest = parts[2], parts[3:]
        info = sessions[sid]
        if not rest:
            if method == "GET":
                return self.reply(200, {"data": info})
            if method == "DELETE":
                del sessions[sid]
                return self.reply(204)
            for key in ("title", "permissions"):
                if key in body:
                    info[key] = body[key]
            if "archived" in body:
                if body["archived"]:
                    info["time"]["archived"] = now()
                else:
                    info["time"].pop("archived", None)
            return self.reply(200, {"data": info})
        if rest == ["model"]:
            info["model"] = body["model"]
            return self.reply(204)
        if rest == ["move"]:
            info["location"] = {"directory": body["directory"]}
            return self.reply(204)
        if rest == ["message"]:
            return self.reply(200, {"data": messages[sid], "cursor": {"next": None}})
        if rest == ["prompt"]:
            state["prompts"].append(body["text"])
            messages[sid].append({"id": f"msg_{next(ids)}", "type": "user", "time": {"created": now()}, "text": body["text"]})
            active.add(sid)
            threading.Thread(target=run, args=(sid, body["text"]), daemon=True).start()
            return self.reply(200, {"data": {"id": "msg"}})
        if rest == ["interrupt"]:
            running = sid in active
            if running:
                interrupts.add(sid)
            return self.reply(200, {"interrupted": running})
        if rest == ["permission"]:
            return self.reply(200, {"data": [p["request"] for p in permissions.values() if p["request"]["sessionID"] == sid]})
        if rest[0] == "permission" and rest[2:] == ["reply"]:
            p = permissions[rest[1]]
            p["reply"] = body
            state["replies"].append(body)
            p["done"].set()
            return self.reply(204)
        if rest == ["form"]:
            return self.reply(200, {"data": [f["form"] for f in forms.values() if f["form"]["sessionID"] == sid]})
        if rest[0] == "form":
            f = forms[rest[1]]
            if method == "POST":
                f["answer"] = body["answer"]
                state["form_answers"].append(body["answer"])
            f["done"].set()
            return self.reply(204)
        return self.reply(404, {"message": "no route"})

    def do_GET(self):
        self.handle_api("GET")

    def do_POST(self):
        self.handle_api("POST")

    def do_PATCH(self):
        self.handle_api("PATCH")

    def do_DELETE(self):
        self.handle_api("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


if __name__ == "__main__":
    Server(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
