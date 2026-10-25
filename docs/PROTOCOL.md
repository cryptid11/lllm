# lllm relay protocol (v1)

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
| `update_session` | `session`, any of `title`, `provider`, `model`, `system`, `options` | `{session: Session}`   |
| `delete_session` | `session`                                                 | `{deleted: id}`                  |
| `chat`           | `session`, `text`                                         | streamed events (below)          |
| `cancel`         | `target` (the `id` of a running `chat`)                   | `{cancelled: bool}`              |

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
| `end`      | chat: `{id, status, error, index, message}`. `status` is `done`, `cancelled` or `error` |

A `chat` produces either a single `error` (rejected before starting, e.g. session busy)
or `start`, any number of `thinking`/`delta`, then exactly one `end`.

## Data

**Summary**: `{id, title, created, updated, provider, model, message_count, preview, generating}`

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

## Disconnects

If the connection drops mid-reply, the relay keeps generating and saves the result.
A reconnecting client sees `generating: true` and a `partial` last message in `get_session`;
poll it until `generating` is false.
