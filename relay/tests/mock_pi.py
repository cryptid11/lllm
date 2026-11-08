#!/usr/bin/env python3
"""A fake `pi --mode rpc` for testing the relay's Pi engine (no Node, no model).

Prompts:
  tool: NAME {json}  call that tool (asks for approval through the extension dialog first)
  slow ...           stream slowly, honoring abort
  history            report previous prompts in this session, the model, and the API key env
  die                exit with an error on stderr
  anything else      echo it back
"""
import json
import os
import queue
import sys
import threading
import time

args = sys.argv[1:]


def flag(name):
    return args[args.index(name) + 1] if name in args else None


session_file = os.path.join(flag("--session-dir"), f"{flag('--session-id')}.jsonl")
commands = queue.Queue()


def read_stdin():
    for line in sys.stdin:
        commands.put(json.loads(line))
    commands.put(None)


threading.Thread(target=read_stdin, daemon=True).start()


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def assistant(text, calls=(), stop="stop"):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "toolCall", "id": c[0], "name": c[1], "arguments": c[2]} for c in calls]
    return {"role": "assistant", "content": content, "stopReason": stop,
            "usage": {"input": 10, "output": 5, "cost": {"total": 0.0001}}}


def stream_text(text, delay=0.0):
    """Streams words; returns False if aborted."""
    for word in text.split(" "):
        try:
            cmd = commands.get(timeout=delay) if delay else commands.get_nowait()
            if cmd and cmd.get("type") == "abort":
                return False
        except queue.Empty:
            pass
        out({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": word + " "}})
    return True


def wait_ui(rid):
    while True:
        cmd = commands.get()
        if cmd is None:
            sys.exit(0)
        if cmd.get("type") == "extension_ui_response" and cmd.get("id") == rid:
            return cmd


def run(prompt):
    previous = open(session_file).read().splitlines() if os.path.exists(session_file) else []
    with open(session_file, "a") as f:
        f.write(prompt + "\n")
    out({"type": "agent_start"})
    out({"type": "turn_start"})
    if prompt == "die":
        sys.stderr.write("mock pi: simulated crash\n")
        sys.stderr.flush()
        sys.exit(3)
    if prompt.startswith("tool: "):
        name, _, raw = prompt[6:].partition(" ")
        tool_args = json.loads(raw)
        out({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "need a tool. "}})
        out({"type": "message_end", "message": assistant("", [("call_1", name, tool_args)], "toolUse")})
        out({"type": "tool_execution_start", "toolCallId": "call_1", "toolName": name, "args": tool_args})
        title = json.dumps({"lllm": "approve", "id": "call_1", "tool": name, "input": tool_args})
        out({"type": "extension_ui_request", "id": "ui-1", "method": "input", "title": title, "placeholder": ""})
        answer = wait_ui("ui-1").get("value", "")
        if answer == "allow":
            out({"type": "tool_execution_update", "toolCallId": "call_1", "toolName": name,
                 "partialResult": {"content": [{"type": "text", "text": "line1\n"}]}})
            out({"type": "tool_execution_update", "toolCallId": "call_1", "toolName": name,
                 "partialResult": {"content": [{"type": "text", "text": "line1\nline2\n"}]}})
            result, error = f"ran {name}: {json.dumps(tool_args)}", False
        else:
            result, error = answer[5:] if answer.startswith("deny:") else "blocked", True
        out({"type": "tool_execution_end", "toolCallId": "call_1", "toolName": name,
             "result": {"content": [{"type": "text", "text": result}]}, "isError": error})
        out({"type": "turn_end"})
        out({"type": "turn_start"})
        reply = f"tool said: {result}"
    elif prompt == "history":
        reply = (f"previous={len(previous)} model={flag('--model')} provider={flag('--provider')} "
                 f"key={os.environ.get('OPENROUTER_API_KEY', '')} tools={'--no-tools' not in args} "
                 f"extension={os.path.exists(flag('-e') or '')}")
    elif prompt.startswith("slow"):
        if not stream_text("one two three four five six seven eight nine ten", delay=0.3):
            out({"type": "message_end", "message": assistant("one", stop="aborted")})
            out({"type": "agent_end"})
            out({"type": "agent_settled", "aborted": True})
            return
        reply = ""
    else:
        reply = f"pi-echo: {prompt}"
    if reply:
        stream_text(reply)
    out({"type": "message_end", "message": assistant(reply)})
    out({"type": "turn_end"})
    out({"type": "agent_end"})
    out({"type": "agent_settled", "aborted": False})


while True:
    cmd = commands.get()
    if cmd is None:
        break
    if cmd.get("type") == "prompt":
        out({"type": "response", "id": cmd.get("id"), "command": "prompt", "success": True,
             "data": {"disposition": "started"}})
        run(cmd["message"])
