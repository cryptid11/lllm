package dev.lllm.app.ui

import android.app.Application
import dev.lllm.app.RelayService
import dev.lllm.app.data.Profile
import dev.lllm.app.data.ProfileStore
import dev.lllm.app.relay.ChatMessage
import dev.lllm.app.relay.Hello
import dev.lllm.app.relay.PendingApproval
import dev.lllm.app.relay.RelayClient
import dev.lllm.app.relay.RelayException
import dev.lllm.app.relay.SessionDetail
import dev.lllm.app.relay.SessionSummary
import dev.lllm.app.relay.ToolCall
import dev.lllm.app.relay.argumentsText
import dev.lllm.app.relay.str
import dev.lllm.app.relay.strings
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
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
    /** [retrying]: a reconnect is scheduled. */
    data class Down(val reason: String, val retrying: Boolean = false) : Conn
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
    /** The session list shows archived sessions instead of active ones. */
    val showArchived: Boolean = false,
    /** Sessions swiped away whose delete can still be undone. */
    val pendingDelete: Set<String> = emptySet(),
    /** A tool call in the open session waiting for Allow/Deny. */
    val pendingApproval: PendingApproval? = null,
    /** Bash output streaming in, by tool call id, until its result arrives. */
    val liveOutput: Map<String, String> = emptyMap(),
    /** The app is on screen (otherwise approvals also raise a notification). */
    val appVisible: Boolean = true,
) {
    val connected get() = conn is Conn.Up
    val generating get() = generatingSince != null
    val visibleSessions get() = sessions.filter { it.archived == showArchived && it.id !in pendingDelete }
    val archivedCount get() = sessions.count { it.archived && it.id !in pendingDelete }
}

/**
 * App state and the relay connection. It lives as long as the process (see [dev.lllm.app.LllmApp]),
 * not the screen, so the connection survives the app going to the background; [RelayService]
 * keeps the process alive meanwhile.
 */
class AppController(private val app: Application) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val store = ProfileStore(app)
    private val _state = MutableStateFlow(UiState())
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var client: RelayClient? = null
    private var connectJob: Job? = null
    private var pollJob: Job? = null
    private var chatId: Long? = null
    private val deleteJobs = mutableMapOf<String, Job>()

    /** The user wants to be connected: keep the service up and reconnect after drops. */
    private var wantConnection = false
    private var reconnectJob: Job? = null
    private var reconnectAttempts = 0

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
            Screen.Sessions -> if (s.showArchived) showArchived(false)
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

    /** Connects now, from the UI. Resets the reconnect backoff. */
    fun connect() {
        reconnectAttempts = 0
        open()
    }

    private fun open() {
        val profile = s.profile?.takeIf { it.isComplete } ?: return
        wantConnection = true
        RelayService.start(app)
        reconnectJob?.cancel()
        connectJob?.cancel()
        client?.close()
        client = null
        _state.update { it.copy(conn = Conn.Connecting) }
        connectJob = scope.launch {
            val c = RelayClient(profile)
            val hello = try {
                withContext(Dispatchers.IO) { c.connect() }
            } catch (e: CancellationException) {
                c.close()
                throw e
            } catch (e: Exception) {
                // RelayExceptions are our own diagnoses (key rejected, host key changed, relay
                // missing): retrying will not fix them. Network errors usually go away.
                val retry = e !is RelayException
                _state.update { it.copy(conn = Conn.Down(describe(e), retrying = retry)) }
                if (retry) scheduleReconnect()
                return@launch
            }
            reconnectAttempts = 0
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
                    _state.update { it.copy(conn = Conn.Down(reason, retrying = wantConnection), generatingSince = null) }
                    if (wantConnection) scheduleReconnect()
                }
            }
            refreshSessions()
            s.session?.let { reloadSession(it.id) }
        }
    }

    private fun scheduleReconnect() {
        if (!wantConnection) return
        val delayMs = minOf(60_000L, 2_000L shl minOf(reconnectAttempts, 5))
        reconnectAttempts++
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            delay(delayMs)
            open()
        }
    }

    /** Closes the connection and stops the background service until the next [connect]. */
    fun disconnect() {
        wantConnection = false
        reconnectJob?.cancel()
        connectJob?.cancel()
        client?.close()
        client = null
        _state.update { it.copy(conn = Conn.Down("Disconnected"), generatingSince = null) }
        RelayService.stop(app)
    }

    private fun describe(e: Exception): String = when {
        e is java.net.UnknownHostException -> "Unknown host: ${e.message}"
        e is java.net.ConnectException -> "Cannot connect: ${e.message}"
        e is java.net.SocketTimeoutException -> "Connection timed out"
        else -> e.message ?: e.javaClass.simpleName
    }

    /** Runs [block] against the live connection, turning failures into a notice. */
    private fun withClient(block: suspend (RelayClient) -> Unit) = scope.launch {
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

    fun setVisible(visible: Boolean) = _state.update { it.copy(appVisible = visible) }

    fun openSession(id: String) {
        _state.update {
            it.copy(screen = Screen.Chat, session = null, messages = emptyList(), generatingSince = null,
                    pendingApproval = null, liveOutput = emptyMap())
        }
        reloadSession(id)
    }

    fun newSession() = withClient { c ->
        val detail = SessionDetail.from(c.call("new_session"))
        _state.update {
            it.copy(screen = Screen.Chat, session = detail, messages = emptyList(), generatingSince = null)
        }
    }

    fun showArchived(show: Boolean) = _state.update { it.copy(showArchived = show) }

    fun renameSession(id: String, title: String) = withClient { c ->
        val saved = SessionDetail.from(c.call("update_session", mapOf("session" to id, "title" to title))).title
        _state.update { st ->
            st.copy(
                sessions = st.sessions.map { if (it.id == id) it.copy(title = saved) else it },
                session = st.session?.let { if (it.id == id) it.copy(title = saved) else it },
            )
        }
    }

    fun setArchived(id: String, archived: Boolean) {
        _state.update { st -> st.copy(sessions = st.sessions.map { if (it.id == id) it.copy(archived = archived) else it }) }
        withClient { c ->
            try {
                c.call("update_session", mapOf("session" to id, "archived" to archived))
            } catch (e: Exception) {
                refreshSessions() // undo the optimistic change
                throw e
            }
        }
    }

    /** Hides the session now and deletes it on the server unless [undoDelete] comes first. */
    fun deleteSoon(id: String) {
        _state.update { it.copy(pendingDelete = it.pendingDelete + id) }
        deleteJobs[id] = scope.launch {
            try {
                delay(UNDO_MS)
                val c = client ?: throw RelayException("Not connected, so the session was kept")
                c.call("delete_session", mapOf("session" to id))
                _state.update { st -> st.copy(sessions = st.sessions.filterNot { it.id == id }) }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                notice(describe(e))
            } finally {
                deleteJobs.remove(id)
                _state.update { it.copy(pendingDelete = it.pendingDelete - id) }
            }
        }
    }

    fun undoDelete(id: String) {
        deleteJobs.remove(id)?.cancel()
    }

    private fun reloadSession(id: String) = withClient { c ->
        val detail = SessionDetail.from(c.call("get_session", mapOf("session" to id)))
        if (chatId != null) return@withClient // a live stream owns the transcript
        _state.update {
            it.copy(
                session = detail,
                messages = detail.messages,
                generatingSince = if (detail.generating) (it.generatingSince ?: System.currentTimeMillis()) else null,
                pendingApproval = detail.pendingApproval,
                liveOutput = emptyMap(),
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
                if (s.session?.id != id || chatId != null) return@withClient
                _state.update {
                    it.copy(
                        session = detail,
                        messages = detail.messages,
                        generatingSince = if (detail.generating) it.generatingSince else null,
                        pendingApproval = detail.pendingApproval,
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
            "/agent" -> {
                val on = when (arg.lowercase()) {
                    "on" -> true
                    "off" -> false
                    else -> !(s.session?.agent ?: false)
                }
                updateSession(mapOf("agent" to on)) { if (it.agent) "Agent mode on: ${it.permissionMode}" else "Chat only" }
            }
            "/mode" -> {
                val mode = arg.lowercase().ifEmpty { nextMode(s.session?.permissionMode) }
                if (mode !in MODES) {
                    notice("Modes: ${MODES.joinToString(", ")}")
                    return false
                }
                updateSession(mapOf("permission_mode" to mode)) { "Permission mode: ${it.permissionMode}" }
            }
            "/cd" -> {
                if (arg.isEmpty()) return false.also { notice("cwd: ${s.session?.cwd}") }
                updateSession(mapOf("cwd" to arg)) { "cwd: ${it.cwd}" }
            }
            "/help" -> notice(COMMANDS.joinToString("\n") { "${it.first}  ${it.second}" })
            else -> {
                notice("Unknown command $name. Try /help")
                return false
            }
        }
        return true
    }

    fun cycleMode() = updateSession(mapOf("permission_mode" to nextMode(s.session?.permissionMode))) {
        "Permission mode: ${it.permissionMode}"
    }

    private fun nextMode(mode: String?) = MODES[(MODES.indexOf(mode) + 1).mod(MODES.size)]

    /** Changes session settings on the relay; [message] turns the updated session into a notice. */
    private fun updateSession(fields: Map<String, Any?>, message: (SessionDetail) -> String?) = withClient { c ->
        val id = s.session?.id ?: return@withClient
        val detail = SessionDetail.from(c.call("update_session", mapOf("session" to id) + fields))
        _state.update { it.copy(session = detail) }
        message(detail)?.let(::notice)
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
                    "tool_call" -> {
                        val call = ToolCall(ev.optString("call_id"), ev.optString("name"), ev.argumentsText("arguments"))
                        updateReply { it.copy(toolCalls = it.toolCalls + call) }
                        if (ev.optBoolean("needs_approval")) {
                            _state.update { it.copy(pendingApproval = PendingApproval(sessionId, call.id, call.name, call.arguments)) }
                        }
                    }
                    "tool_output" -> _state.update { st ->
                        val id = ev.optString("call_id")
                        st.copy(liveOutput = st.liveOutput + (id to st.liveOutput[id].orEmpty() + ev.optString("text")))
                    }
                    "tool_result" -> _state.update { st ->
                        val id = ev.optString("call_id")
                        val result = ChatMessage(
                            "tool", ev.optString("output"),
                            toolCallId = id, ok = ev.optBoolean("ok"), approval = ev.str("approval"),
                        )
                        st.copy(
                            messages = st.messages + result,
                            liveOutput = st.liveOutput - id,
                            pendingApproval = st.pendingApproval?.takeIf { it.callId != id },
                        )
                    }
                    "turn" -> _state.update { it.copy(messages = it.messages + ChatMessage("assistant", "", partial = true)) }
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
            _state.update { it.copy(generatingSince = null, pendingApproval = null, liveOutput = emptyMap()) }
        }
        refreshSessions()
        // The server's copy is canonical (full tool outputs, final statuses, title).
        val detail = SessionDetail.from(c.call("get_session", mapOf("session" to sessionId)))
        if (s.session?.id == sessionId && chatId == null) {
            _state.update { it.copy(session = detail, messages = detail.messages) }
        }
    }

    /** Applies [f] to the reply being written: the last assistant message. */
    private fun updateReply(f: (ChatMessage) -> ChatMessage) = _state.update {
        val i = it.messages.indexOfLast { m -> m.role == "assistant" }
        if (i < 0) it else it.copy(messages = it.messages.toMutableList().also { list -> list[i] = f(list[i]) })
    }

    /** Stops the reply in the open session, whichever connection started it. */
    fun stop() {
        val id = s.session?.id ?: return
        withClient { it.call("cancel", mapOf("session" to id)) }
    }

    /** decision: allow, allow_session or deny. [note] is passed to the model with a denial. */
    fun approve(decision: String, note: String? = null) {
        val pending = s.pendingApproval ?: return
        approve(pending, decision, note)
    }

    fun approve(pending: PendingApproval, decision: String, note: String? = null) {
        _state.update { it.copy(pendingApproval = it.pendingApproval?.takeIf { p -> p.callId != pending.callId }) }
        withClient { c ->
            try {
                c.call("approve", mapOf("session" to pending.session, "call_id" to pending.callId,
                                        "decision" to decision, "note" to note))
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                _state.update { it.copy(pendingApproval = pending) } // still pending: show it again
                throw e
            }
        }
    }

    companion object {
        /** How long a swiped-away session can be restored; a bit longer than the snackbar. */
        const val UNDO_MS = 5_000L

        val MODES = listOf("ask", "auto-read", "auto")

        val COMMANDS = listOf(
            "/new" to "start a new session",
            "/model" to "pick a model (or /model NAME)",
            "/sessions" to "browse and resume sessions",
            "/settings" to "connection and key",
            "/agent" to "tools on/off (or /agent on|off)",
            "/mode" to "approvals: ask · auto-read · auto",
            "/cd" to "change working directory",
            "/help" to "list commands",
        )
    }
}
