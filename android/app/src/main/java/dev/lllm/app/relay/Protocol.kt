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
) {
    val isUser get() = role == "user"

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
            )
        }
    }
}

/** Like optString, but null for missing/null values instead of "" / "null". */
fun JSONObject.str(name: String): String? = if (isNull(name)) null else optString(name)

fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }

fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
