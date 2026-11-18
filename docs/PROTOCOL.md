# lllm relay protocol (v2)

The client opens an SSH **exec** channel running `~/.local/bin/lllm-relay serve` (no PTY)
and exchanges newline-delimited JSON (UTF-8, one object per line) over stdin/stdout.
The connection is the session transport only: all conversation state lives on the server,
so a client can disconnect at any time and resume later from any device.

## Requests (client → relay)

```json
{"id": 7, "op": "chat", "session": "20261025-204121-ae4450", "text": "hello"}
```

`id` is chosen by the client (any JSON value; use increasing integers) and is echoed on every
message that belongs to that request. Requests may be pipelined; replies can arrive out of order.

| op               | fields                                                    | result `data`                    |
|------------------|-----------------------------------------------------------|----------------------------------|
| `ping`           |                                                           | `{pong}`                         |
| `info`           |                                                           | same as `hello` (below)          |
| `list_models`    | `provider?`                                               | `{provider, models: [str]}`      |
| `list_sessions`  |                                                           | `{sessions: [Summary]}`          |
| `new_session`    | `title?`, `provider?`, `model?`, `system?`, `options?`    | `{session: Session}`             |
| `get_session`    | `session`                                                 | `{session: Session}`             |
| `update_session` | `session`, any of `title`, `provider`, `model`, `system`, `options`, `archived` | `{session: Session}` |
| `delete_session` | `session`                                                 | `{deleted: id}`                  |
| `chat`           | `session`, `text`                                         | streamed events (below)          |
| `cancel`         | `target` (a `chat` request id on this connection) or `session` (works from any connection) | `{cancelled: bool}` |
| `approve`        | `session`, `call_id`, `decision` (`allow`, `allow_session`, `deny`), `note?` | `{ok}`        |
| `form_reply`     | `session`, `form_id`, `answer` (object of field key → value); OpenCode only | `{ok}`         |
| `form_cancel`    | `session`, `form_id`; OpenCode only                       | `{ok}`                           |

## Messages (relay → client)

| type       | when                                                                          |
|------------|-------------------------------------------------------------------------------|
| `hello`    | first line after connecting: `{version, protocol, host, default_provider, providers}` |
| `fatal`    | instead of `hello` if the relay cannot start (bad config); `{error}`          |
| `result`   | `{id, data}`, the successful reply to a non-streaming op                       |
| `error`    | `{id, error}`, the request failed (unknown session, backend down, ...)         |
| `start`    | chat: `{id, session, index, model, provider}`. `index` is the reply's position in `messages` |
| `thinking` | chat: `{id, text}`, a reasoning-token chunk (qwen3 etc. when `think` is on)    |
| `delta`    | chat: `{id, text}`, a reply-text chunk                                         |
| `tool_call`   | agent: `{id, call_id, name, arguments, needs_approval}`. If `needs_approval`, the call waits for `approve` |
| `tool_output` | agent: `{id, call_id, text}`, live bash output                                   |
| `tool_result` | agent: `{id, call_id, ok, approval, output}` (output truncated to 4000 chars; the session has it in full) |
| `approval_resolved` | OpenCode: `{id, call_id}`, the approval was answered (possibly elsewhere) |
| `form`        | OpenCode: `{id, form}`, the agent asks a question and waits for `form_reply`     |
| `form_resolved` | OpenCode: `{id, form_id}`, the question was answered or cancelled              |
| `turn`        | agent: `{id, index}`, the model starts another response after tool results     |
| `end`      | chat: `{id, status, error, index, message}`. `status` is `done`, `cancelled` or `error` |

A `chat` produces either a single `error` (rejected before starting, e.g. session busy)
or `start`, any number of `thinking`/`delta`, then exactly one `end`.

## Data

**Summary**: `{id, title, created, updated, provider, model, message_count, preview, generating, archived}`

`updated` is the time of the last message; renaming or archiving does not change it.

**Session**:
```json
{
  "id": "20261025-204121-ae4450",
  "title": "hi from the terminal",
  "created": 1793004081.2, "updated": 1793004085.9,
  "provider": "ollama", "model": "qwen2.5:3b",
  "system": null,
  "options": {"think": false, "options": {"num_ctx": 8192}},
  "generating": false,
  "messages": [
    {"role": "user", "content": "hi", "ts": 1793004081.3},
    {"role": "assistant", "content": "Hello!", "ts": 1793004081.3, "model": "qwen2.5:3b",
     "thinking": "...", "stats": {"eval_count": 7, "tokens_per_sec": 4.1, "wall_sec": 3.2}}
  ]
}
```

Assistant message flags: `partial: true` while still generating (saved every ~2 s),
`status: "cancelled" | "error" | "interrupted"` when it did not finish normally, plus `error`.
`system: null` means use the server's default system prompt.

`options` is passed to the backend: for Ollama `think` (bool) and `options` (num_ctx,
temperature, ...); for OpenAI-compatible backends `params` (temperature, max_tokens, ...).

## Agent mode

Sessions with `agent: true` offer the model six tools that run on the relay's machine as the relay's
user: `bash`, `read_file`, `write_file`, `edit_file`, `list_dir`, `grep`. One `chat` then loops:
model response → tool calls → results → next model response (a `turn` event), until the model
answers without calling tools or `agent.max_steps` is reached.

Session field `engine`: `builtin` (the relay's own tools, described here) or `pi` (the relay runs
[Pi](https://pi.dev) in RPC mode per message and translates its events into the same messages and
events; Pi's tool names are `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`, and the `cwd` is
fixed after the first message).

Session fields: `agent`, `permission_mode` (`ask` approves everything; `auto-read`, the default,
auto-runs `read_file`/`list_dir`/`grep`; `auto` never asks), `cwd` (updated when bash `cd`s),
`allowed_tools` (tools allowed for the rest of the session via `allow_session`). All are settable
with `update_session`; `cwd` is resolved against the current one and must exist.

Stored messages follow the OpenAI shape: assistant messages may carry `tool_calls`
(`[{id, type: "function", function: {name, arguments}}]`, arguments as JSON text), followed by one
`{role: "tool", tool_call_id, name, content, ok, approval}` per call. `approval` is `auto`, `allow`,
`allow_session`, `deny`, `timeout` or `cancelled`.

While a call waits, the session has `pending_approval: {call_id, name, arguments, since}`. Since every
connection is its own relay process, approvals and `cancel` by `session` go through the session file,
so a client that reconnects can answer an approval (or stop the reply) started by an earlier connection.

## OpenCode sessions

Session ids starting with `ses` live in the OpenCode service, which the relay proxies: `engine` is
`opencode`, `provider` is `opencode`, `model` is `provider/name`, and `list_sessions` merges them
with local sessions (adding `warning` if OpenCode cannot be reached). `message_count` is 0 and
`preview` is the session's folder. A `tool_call` may be sent twice for one call: first when the tool
is called and again with `needs_approval` and `save` (the rules **Always** would remember) when it
starts waiting. `get_session` reports waits as `pending_approval` and `pending_form` (OpenCode's form
object). `permission_mode` can also read `custom` when the rules match no preset.

## Disconnects

If the connection drops mid-reply, the relay keeps generating and saves the result.
A reconnecting client sees `generating: true` and a `partial` last message in `get_session`;
poll it until `generating` is false.
