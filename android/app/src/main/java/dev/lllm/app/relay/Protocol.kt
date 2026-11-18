package dev.lllm.app.relay

import org.json.JSONArray
import org.json.JSONObject

// Models for docs/PROTOCOL.md. Parsed by hand with org.json (bundled with Android).

class RelayException(message: String) : Exception(message)

data class Hello(
    val version: String,
    val protocol: Int,
    val host: String,
    val defaultProvider: String,
) {
    companion object {
        fun from(o: JSONObject) = Hello(
            version = o.optString("version"),
            protocol = o.optInt("protocol"),
            host = o.optString("host"),
            defaultProvider = o.optString("default_provider"),
        )
    }
}

data class SessionSummary(
    val id: String,
    val title: String,
    val updated: Double,
    val provider: String,
    val model: String,
    val messageCount: Int,
    val preview: String,
    val generating: Boolean,
    val archived: Boolean = false,
) {
    companion object {
        fun from(o: JSONObject) = SessionSummary(
            id = o.getString("id"),
            title = o.str("title") ?: "",
            updated = o.optDouble("updated", 0.0),
            provider = o.str("provider") ?: "",
            model = o.str("model") ?: "",
            messageCount = o.optInt("message_count"),
            preview = o.str("preview") ?: "",
            generating = o.optBoolean("generating"),
            archived = o.optBoolean("archived"),
        )

        fun listFrom(data: JSONObject): List<SessionSummary> =
            data.getJSONArray("sessions").objects().map(::from)
    }
}

/** A tool the model asked to run; [arguments] is the raw JSON object text. */
data class ToolCall(val id: String, val name: String, val arguments: String) {
    val label: String get() = toolLabel(name, arguments)

    companion object {
        fun listFrom(arr: JSONArray?): List<ToolCall> = arr?.objects()?.map { c ->
            val fn = c.optJSONObject("function") ?: JSONObject()
            ToolCall(c.optString("id"), fn.optString("name"), fn.optString("arguments", "{}"))
        } ?: emptyList()
    }
}

/** A tool call waiting for the user to allow or deny it. */
data class PendingApproval(
    val session: String,
    val callId: String,
    val name: String,
    val arguments: String,
    /** OpenCode: the rules "Always" would save for the project, e.g. "git *". */
    val save: List<String> = emptyList(),
) {
    val label: String get() = toolLabel(name, arguments)
}

data class FormOption(val value: String, val label: String, val description: String)

data class FormField(
    val key: String,
    /** string, number, integer, boolean, multiselect or external. */
    val type: String,
    val title: String,
    val description: String,
    val required: Boolean,
    val options: List<FormOption>,
    /** Free text is accepted besides the options. */
    val custom: Boolean,
    val placeholder: String,
    val url: String,
    val default: Any?,
    /** Shown only when these (key, op, value) conditions hold. */
    val conditions: List<Triple<String, String, Any?>>,
)

/** A question the agent is waiting on (OpenCode's forms). */
data class PendingForm(
    val session: String,
    val id: String,
    val title: String,
    val isQuestion: Boolean,
    val fields: List<FormField>,
) {
    companion object {
        fun from(o: JSONObject) = PendingForm(
            session = o.optString("sessionID"),
            id = o.optString("id"),
            title = o.optString("title"),
            isQuestion = o.optJSONObject("metadata")?.optString("kind") == "question",
            fields = o.optJSONArray("fields")?.objects().orEmpty().filterNot { it.optBoolean("hidden") }.map { f ->
                FormField(
                    key = f.optString("key"),
                    type = f.optString("type", "string"),
                    title = f.str("title") ?: "",
                    description = f.str("description") ?: "",
                    required = f.optBoolean("required"),
                    options = f.optJSONArray("options")?.objects().orEmpty().map {
                        FormOption(it.optString("value"), it.optString("label"), it.str("description") ?: "")
                    },
                    custom = f.optBoolean("custom"),
                    placeholder = f.str("placeholder") ?: "",
                    url = f.str("url") ?: "",
                    default = if (f.isNull("default")) null else f.opt("default"),
                    conditions = f.optJSONArray("when")?.objects().orEmpty().map {
                        Triple(it.optString("key"), it.optString("op"), it.opt("value"))
                    },
                )
            },
        )
    }
}

fun JSONObject.argumentsText(name: String): String =
    optJSONObject(name)?.toString() ?: optString(name, "{}")

/** Claude Code style one-liner, e.g. Bash(ls -la) or Edit(src/main.py). */
fun toolLabel(name: String, arguments: String): String {
    val args = runCatching { JSONObject(arguments) }.getOrDefault(JSONObject())
    fun arg(key: String): String {
        val line = args.optString(key).lineSequence().firstOrNull().orEmpty()
        return if (line.length > 80) line.take(77) + "…" else line
    }
    // Tool names of both engines: the builtin one (read_file...) and Pi (read, ls, find...).
    return when (name) {
        "bash" -> "Bash(${arg("command")})"
        "shell" -> "Shell(${arg("command")})"
        "read_file", "read" -> "Read(${arg("path")})"
        "write_file", "write" -> "Write(${arg("path")})"
        "edit_file", "edit" -> "Edit(${arg("path")})"
        "list_dir", "ls" -> "List(${arg("path").ifEmpty { "." }})"
        "grep" -> "Grep(${arg("pattern")})"
        "find" -> "Find(${arg("pattern")})"
        "glob" -> "Glob(${arg("pattern")})"
        "question" -> "Question"
        else -> {
            // Other tools (MCP, extensions): show the first text argument.
            val first = args.keys().asSequence().map { args.opt(it) }.filterIsInstance<String>().firstOrNull()
            "$name(${first?.lineSequence()?.firstOrNull()?.take(60) ?: "…"})"
        }
    }
}

data class ChatMessage(
    val role: String,
    val content: String,
    val thinking: String = "",
    /** null when finished normally, else "cancelled", "error" or "interrupted". */
    val status: String? = null,
    val error: String? = null,
    val partial: Boolean = false,
    val evalCount: Int? = null,
    val tokensPerSec: Double? = null,
    val wallSec: Double? = null,
    /** Assistant messages: tools the model called. */
    val toolCalls: List<ToolCall> = emptyList(),
    /** Tool messages (role "tool"): which call this is the result of; [content] is the output. */
    val toolCallId: String? = null,
    val ok: Boolean? = null,
    /** How the call was cleared: auto, allow, allow_session, deny, timeout or cancelled. */
    val approval: String? = null,
) {
    val isUser get() = role == "user"
    val isTool get() = role == "tool"

    companion object {
        fun from(o: JSONObject): ChatMessage {
            val stats = o.optJSONObject("stats")
            return ChatMessage(
                role = o.optString("role"),
                content = o.str("content") ?: "",
                thinking = o.str("thinking") ?: "",
                status = o.str("status"),
                error = o.str("error"),
                partial = o.optBoolean("partial"),
                evalCount = stats?.takeIf { it.has("eval_count") && !it.isNull("eval_count") }?.optInt("eval_count"),
                tokensPerSec = stats?.takeIf { it.has("tokens_per_sec") }?.optDouble("tokens_per_sec"),
                wallSec = stats?.takeIf { it.has("wall_sec") }?.optDouble("wall_sec"),
                toolCalls = ToolCall.listFrom(o.optJSONArray("tool_calls")),
                toolCallId = o.str("tool_call_id"),
                ok = if (o.has("ok")) o.optBoolean("ok") else null,
                approval = o.str("approval"),
            )
        }
    }
}

data class SessionDetail(
    val id: String,
    val title: String,
    val provider: String,
    val model: String,
    val messages: List<ChatMessage>,
    val generating: Boolean,
    /** Tools enabled for this session. */
    val agent: Boolean = false,
    val permissionMode: String = "auto-read",
    val cwd: String = "",
    val pendingApproval: PendingApproval? = null,
    /** "opencode", "pi" or "builtin": which agent runs this session on the server. */
    val engine: String = "builtin",
    val pendingForm: PendingForm? = null,
) {
    companion object {
        fun from(data: JSONObject): SessionDetail {
            val s = data.getJSONObject("session")
            return SessionDetail(
                id = s.getString("id"),
                title = s.str("title") ?: "",
                provider = s.str("provider") ?: "",
                model = s.str("model") ?: "",
                messages = s.optJSONArray("messages")?.objects()?.map(ChatMessage::from) ?: emptyList(),
                generating = s.optBoolean("generating"),
                agent = s.optBoolean("agent"),
                permissionMode = s.str("permission_mode") ?: "auto-read",
                cwd = s.str("cwd") ?: "",
                engine = s.str("engine") ?: "builtin",
                pendingApproval = s.optJSONObject("pending_approval")?.let {
                    PendingApproval(
                        s.getString("id"), it.optString("call_id"), it.optString("name"),
                        it.argumentsText("arguments"), it.optJSONArray("save")?.strings().orEmpty(),
                    )
                },
                pendingForm = s.optJSONObject("pending_form")?.let(PendingForm::from),
            )
        }
    }
}

/** Like optString, but null for missing/null values instead of "" / "null". */
fun JSONObject.str(name: String): String? = if (isNull(name)) null else optString(name)

fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }

fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
