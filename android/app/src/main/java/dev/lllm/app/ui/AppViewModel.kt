package dev.lllm.app.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import dev.lllm.app.data.Profile
import dev.lllm.app.data.ProfileStore
import dev.lllm.app.relay.ChatMessage
import dev.lllm.app.relay.Hello
import dev.lllm.app.relay.RelayClient
import dev.lllm.app.relay.SessionDetail
import dev.lllm.app.relay.SessionSummary
import dev.lllm.app.relay.strings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class Screen { Setup, Sessions, Chat }

sealed interface Conn {
    data object Idle : Conn
    data object Connecting : Conn
    data class Up(val hello: Hello) : Conn
    data class Down(val reason: String) : Conn
}

data class UiState(
    val screen: Screen = Screen.Sessions,
    val profile: Profile? = null,
    val conn: Conn = Conn.Idle,
    val sessions: List<SessionSummary> = emptyList(),
    val session: SessionDetail? = null,
    val messages: List<ChatMessage> = emptyList(),
    /** Epoch millis when the reply in progress started, or null when idle. */
    val generatingSince: Long? = null,
    val models: List<String>? = null,
    val notice: String? = null,
) {
    val connected get() = conn is Conn.Up
    val generating get() = generatingSince != null
}

class AppViewModel(app: Application) : AndroidViewModel(app) {

    private val store = ProfileStore(app)
    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var client: RelayClient? = null
    private var connectJob: Job? = null
    private var pollJob: Job? = null
    private var chatId: Long? = null

    init {
        val profile = store.load()
        _state.update {
            it.copy(profile = profile, screen = if (profile?.isComplete == true) Screen.Sessions else Screen.Setup)
        }
        if (profile?.isComplete == true) connect()
    }

    private val s get() = _state.value

    private fun notice(msg: String) = _state.update { it.copy(notice = msg) }

    fun dismissNotice() = _state.update { it.copy(notice = null) }

    // ------------------------------------------------------------------ navigation

    fun goTo(screen: Screen) {
        if (screen != Screen.Chat) pollJob?.cancel()
        if (screen == Screen.Sessions) refreshSessions()
        _state.update { it.copy(screen = screen) }
    }

    fun back() {
        when (s.screen) {
            Screen.Chat -> goTo(Screen.Sessions)
            Screen.Setup -> if (s.profile?.isComplete == true) goTo(Screen.Sessions)
            Screen.Sessions -> {}
        }
    }

    fun onResume() {
        if (s.profile?.isComplete == true && (s.conn is Conn.Idle || s.conn is Conn.Down)) connect()
    }

    // ------------------------------------------------------------------ connection

    fun saveProfile(profile: Profile) {
        val hostChanged = profile.host != s.profile?.host || profile.port != s.profile?.port
        val p = if (hostChanged && profile.hostKeyFingerprint == s.profile?.hostKeyFingerprint) {
            profile.copy(hostKeyFingerprint = "") // new server: pin its key on next connect
        } else profile
        store.save(p)
        _state.update { it.copy(profile = p, screen = Screen.Sessions, sessions = emptyList(), session = null) }
        connect()
    }

    fun connect() {
        val profile = s.profile?.takeIf { it.isComplete } ?: return
        connectJob?.cancel()
        client?.close()
        client = null
        _state.update { it.copy(conn = Conn.Connecting) }
        connectJob = viewModelScope.launch {
            val c = RelayClient(profile)
            val hello = try {
                withContext(Dispatchers.IO) { c.connect() }
            } catch (e: CancellationException) {
                c.close()
                throw e
            } catch (e: Exception) {
                _state.update { it.copy(conn = Conn.Down(describe(e))) }
                return@launch
            }
            c.newHostKey?.let { fp ->
                val pinned = profile.copy(hostKeyFingerprint = fp)
                store.save(pinned)
                _state.update { it.copy(profile = pinned) }
                notice("Trusted host key $fp")
            }
            client = c
            _state.update { it.copy(conn = Conn.Up(hello)) }
            launch {
                val reason = c.closed.await()
                if (client === c) {
                    client = null
                    _state.update { it.copy(conn = Conn.Down(reason), generatingSince = null) }
                }
            }
            refreshSessions()
            s.session?.let { reloadSession(it.id) }
        }
    }

    private fun describe(e: Exception): String = when {
        e is java.net.UnknownHostException -> "Unknown host: ${e.message}"
        e is java.net.ConnectException -> "Cannot connect: ${e.message}"
        e is java.net.SocketTimeoutException -> "Connection timed out"
        else -> e.message ?: e.javaClass.simpleName
    }

    /** Runs [block] against the live connection, turning failures into a notice. */
    private fun withClient(block: suspend (RelayClient) -> Unit) = viewModelScope.launch {
        val c = client ?: return@launch notice("Not connected")
        try {
            block(c)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            notice(describe(e))
        }
    }

    // ------------------------------------------------------------------ sessions

    fun refreshSessions() = withClient { c ->
        val list = SessionSummary.listFrom(c.call("list_sessions"))
        _state.update { it.copy(sessions = list) }
    }

    fun openSession(id: String) {
        _state.update { it.copy(screen = Screen.Chat, session = null, messages = emptyList(), generatingSince = null) }
        reloadSession(id)
    }

    fun newSession() = withClient { c ->
        val detail = SessionDetail.from(c.call("new_session"))
        _state.update {
            it.copy(screen = Screen.Chat, session = detail, messages = emptyList(), generatingSince = null)
        }
    }

    fun deleteSession(id: String) = withClient { c ->
        c.call("delete_session", mapOf("session" to id))
        _state.update { st -> st.copy(sessions = st.sessions.filterNot { it.id == id }) }
    }

    private fun reloadSession(id: String) = withClient { c ->
        val detail = SessionDetail.from(c.call("get_session", mapOf("session" to id)))
        if (chatId != null) return@withClient // a live stream owns the transcript
        _state.update {
            it.copy(
                session = detail,
                messages = detail.messages,
                generatingSince = if (detail.generating) (it.generatingSince ?: System.currentTimeMillis()) else null,
            )
        }
        if (detail.generating) pollWhileGenerating(id)
    }

    /** A reply started by an earlier connection is still being written on the server: watch it. */
    private fun pollWhileGenerating(id: String) {
        pollJob?.cancel()
        pollJob = withClient { c ->
            while (true) {
                delay(1500)
                val detail = SessionDetail.from(c.call("get_session", mapOf("session" to id)))
                if (s.session?.id != id) return@withClient
                _state.update {
                    it.copy(
                        session = detail,
                        messages = detail.messages,
                        generatingSince = if (detail.generating) it.generatingSince else null,
                    )
                }
                if (!detail.generating) {
                    refreshSessions()
                    return@withClient
                }
            }
        }
    }

    fun loadModels() = withClient { c ->
        _state.update { it.copy(models = null) }
        val provider = s.session?.provider
        val data = c.call("list_models", if (provider != null) mapOf("provider" to provider) else emptyMap())
        _state.update { it.copy(models = data.getJSONArray("models").strings()) }
    }

    fun closeModels() = _state.update { it.copy(models = null) }

    fun setModel(model: String) = withClient { c ->
        val id = s.session?.id ?: return@withClient
        val detail = SessionDetail.from(c.call("update_session", mapOf("session" to id, "model" to model)))
        _state.update { it.copy(session = detail) }
        notice("Model set to $model")
    }

    // ------------------------------------------------------------------ chat

    /** Handles text from the prompt: a slash command, or a message to the model. */
    fun submit(text: String): Boolean {
        val t = text.trim()
        if (t.isEmpty()) return false
        if (t.startsWith("/")) return command(t)
        val session = s.session ?: return false
        if (s.generating || !s.connected) return false
        send(session.id, t)
        return true
    }

    private fun command(t: String): Boolean {
        val name = t.substringBefore(' ').lowercase()
        val arg = t.substringAfter(' ', "").trim()
        when (name) {
            "/new", "/clear" -> newSession()
            "/sessions", "/resume" -> goTo(Screen.Sessions)
            "/model" -> if (arg.isNotEmpty()) setModel(arg) else loadModels()
            "/settings" -> goTo(Screen.Setup)
            "/help" -> notice(COMMANDS.joinToString("\n") { "${it.first}  ${it.second}" })
            else -> {
                notice("Unknown command $name. Try /help")
                return false
            }
        }
        return true
    }

    private fun send(sessionId: String, text: String) = withClient { c ->
        pollJob?.cancel()
        val stream = c.chat(sessionId, text)
        chatId = stream.id
        _state.update {
            it.copy(
                messages = it.messages + ChatMessage("user", text) + ChatMessage("assistant", "", partial = true),
                generatingSince = System.currentTimeMillis(),
            )
        }
        try {
            for (ev in stream.events) {
                when (ev.optString("type")) {
                    "delta" -> updateReply { it.copy(content = it.content + ev.optString("text")) }
                    "thinking" -> updateReply { it.copy(thinking = it.thinking + ev.optString("text")) }
                    "end" -> {
                        val final = ev.optJSONObject("message")?.let(ChatMessage::from)
                        updateReply { final ?: it.copy(partial = false, status = ev.optString("status")) }
                    }
                    "error" -> {
                        // Rejected before starting (e.g. busy): nothing was saved, so undo.
                        _state.update { it.copy(messages = it.messages.dropLast(2)) }
                        notice(ev.optString("error"))
                    }
                }
            }
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            // The connection dropped; the relay keeps generating and we resync on reconnect.
            updateReply { it.copy(status = "disconnected") }
            throw e
        } finally {
            chatId = null
            _state.update { it.copy(generatingSince = null) }
        }
        refreshSessions()
        if (s.session?.title.isNullOrEmpty()) {
            val detail = SessionDetail.from(c.call("get_session", mapOf("session" to sessionId)))
            _state.update { it.copy(session = detail) }
        }
    }

    private fun updateReply(f: (ChatMessage) -> ChatMessage) = _state.update {
        val last = it.messages.lastOrNull() ?: return@update it
        if (last.isUser) it else it.copy(messages = it.messages.dropLast(1) + f(last))
    }

    fun stop() {
        val id = chatId ?: return
        withClient { it.cancel(id) }
    }

    override fun onCleared() {
        client?.close()
    }

    companion object {
        val COMMANDS = listOf(
            "/new" to "start a new session",
            "/model" to "pick a model (or /model NAME)",
            "/sessions" to "browse and resume sessions",
            "/settings" to "connection and key",
            "/help" to "list commands",
        )
    }
}
