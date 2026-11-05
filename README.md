# lllm

A Claude-Code-style Android client for your own LLM. The phone connects over SSH to a small
relay on your server, which talks to Ollama (or any OpenAI-compatible backend) and keeps
every conversation on disk, so sessions survive disconnects and can be resumed anywhere.

[<img src="https://raw.githubusercontent.com/ImranR98/Obtainium/main/assets/graphics/badge_obtainium.png" alt="Get it on Obtainium" height="54">](https://apps.obtainium.imranr.dev/redirect?r=obtainium://add/https://github.com/cryptid11/lllm)

Or grab the APK from [Releases](https://github.com/cryptid11/lllm/releases/latest).

```
Android app ──SSH (key auth, exec channel)──▶ server: `lllm-relay serve`
               JSON lines over stdin/stdout          │  sessions saved as JSON on the server
                                                     └─▶ HTTP ─▶ Ollama / OpenAI-compatible backend
```

- **relay/**: `lllm-relay`, one Python file (stdlib only) for the server.
- **android/**: the app (Kotlin, Jetpack Compose, sshj).
- **docs/PROTOCOL.md**: the wire protocol between them.

## 1. Server

```bash
git clone https://github.com/cryptid11/lllm && cd lllm
relay/install.sh        # -> ~/.local/bin/lllm-relay and ~/.config/lllm/config.json
lllm-relay doctor       # checks the backend, lists models
lllm-relay chat         # terminal chat; --last resumes, -s ID picks a session
```

The relay must run on a machine the phone can SSH into. The model itself can be elsewhere:
just point `base_url` at it.

`~/.config/lllm/config.json`:

```json
{
  "default_provider": "ollama",
  "providers": {
    "ollama":   {"type": "ollama", "base_url": "http://127.0.0.1:11434", "default_model": "qwen3.5:2b",
                 "options": {"num_ctx": 8192}, "keep_alive": "30m"},
    "llamacpp": {"type": "openai", "base_url": "http://127.0.0.1:8080/v1"},
    "openrouter": {"type": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "api_key_file": "~/openrouter.key",
                   "models": ["moonshotai/kimi-k3.1-pro", "qwen/qwen3.8-max-prime"],
                   "default_model": "moonshotai/kimi-k3.1-pro",
                   "params": {"max_tokens": 1024}}
  },
  "system_prompt": "You are a helpful assistant.",
  "sessions_dir": "~/.local/share/lllm/sessions"
}
```

An empty `default_model` means "the first model the backend reports".

- `models` limits the app's model picker to that list (otherwise it shows everything the backend lists).
- The key comes from `api_key` (inline), `api_key_file` (read on each request, so you can rotate it
  without restarting), or `api_key_env` (an environment variable).
- `params` go into every request (`max_tokens`, `temperature`, ...). With OpenRouter, set
  `max_tokens`: without it, OpenRouter reserves the model's whole output budget up front and refuses
  requests from keys with a low spending limit.
- `headers` adds extra HTTP headers.

## 2. Phone

1. Install the app (Obtainium button above).
2. Enter host, port and user; tap **Generate** to create a key on the phone.
3. Copy the shown line into `~/.ssh/authorized_keys` on the server. By default the line is
   locked with `command="…lllm-relay serve",restrict`, so that key can only run the relay.
4. **Save & connect.** The server's host key is pinned on first connect.

In a chat: type `/` for commands (`/new`, `/model`, `/sessions`, `/settings`, `/help`),
tap the model name to switch models, and tap ✕ to interrupt a reply. If the connection drops
mid-reply, the server finishes and saves it, and the app picks it up when it reconnects.

## Testing without a model

```bash
python3 relay/tests/test_relay.py              # relay end-to-end tests
python3 relay/tests/mock_ollama.py 11434 0.08  # fake Ollama for trying the app
```

The mock echoes your message back. Include "markdown" for a formatted reply, "think" for
reasoning output, "slow" for a slow reply, or "boom" for a backend error.

## Builds

Every push to `main` builds the APK in GitHub Actions and publishes it as a release.
The debug signing key is committed (`android/app/debug.keystore`) so every build installs
as an update; it is public, so don't reuse it for anything else.
