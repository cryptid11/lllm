package dev.lllm.app.relay

import dev.lllm.app.data.Profile
import dev.lllm.app.data.SshKeys
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.ReceiveChannel
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import net.schmizz.keepalive.KeepAliveProvider
import net.schmizz.sshj.DefaultConfig
import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.connection.channel.direct.Session
import net.schmizz.sshj.transport.TransportException
import net.schmizz.sshj.transport.verification.HostKeyVerifier
import net.schmizz.sshj.userauth.UserAuthException
import net.schmizz.sshj.userauth.password.PasswordUtils
import org.json.JSONObject
import java.io.BufferedReader
import java.io.Closeable
import java.io.IOException
import java.io.InputStreamReader
import java.io.OutputStream
import java.security.PublicKey
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/** A running `chat` request: its id (for cancel) and its events up to and including "end". */
class ChatStream(val id: Long, val events: ReceiveChannel<JSONObject>)

/**
 * One SSH connection running `lllm-relay serve`, speaking the JSON-lines protocol.
 * [connect] blocks; everything else is safe to call from any coroutine.
 */
class RelayClient(private val profile: Profile) : Closeable {

    /** Set by [connect] when the host key was not pinned yet and has now been trusted. */
    var newHostKey: String? = null
        private set

    private var ssh: SSHClient? = null
    private var session: Session? = null
    private var output: OutputStream? = null
    private val nextId = AtomicLong(0)
    private val calls = ConcurrentHashMap<Long, CompletableDeferred<JSONObject>>()
    private val streams = ConcurrentHashMap<Long, Channel<JSONObject>>()
    private val stderr = StringBuffer()
    private val _closed = CompletableDeferred<String>()

    /** Completes with a human-readable reason once the connection is gone. */
    val closed: Deferred<String> get() = _closed

    fun connect(): Hello {
        var seenKey: String? = null
        val client = SSHClient(DefaultConfig().apply { keepAliveProvider = KeepAliveProvider.KEEP_ALIVE })
        client.connectTimeout = 15_000
        client.addHostKeyVerifier(object : HostKeyVerifier {
            override fun verify(hostname: String?, port: Int, key: PublicKey): Boolean {
                val fp = SshKeys.fingerprint(key)
                seenKey = fp
                return profile.hostKeyFingerprint.isEmpty() || profile.hostKeyFingerprint == fp
            }

            override fun findExistingAlgorithms(hostname: String?, port: Int): List<String> = emptyList()
        })
        try {
            try {
                client.connect(profile.host, profile.port)
            } catch (e: TransportException) {
                val seen = seenKey
                if (seen != null && profile.hostKeyFingerprint.isNotEmpty() && seen != profile.hostKeyFingerprint) {
                    throw RelayException(
                        "Host key changed! Expected ${profile.hostKeyFingerprint}, server sent $seen. " +
                            "If you reinstalled the server, clear the pinned key in Settings."
                    )
                }
                throw e
            }
            if (profile.hostKeyFingerprint.isEmpty()) newHostKey = seenKey
            client.connection.keepAlive.keepAliveInterval = 15

            val keys = client.loadKeys(
                profile.privateKey.trim() + "\n",
                null as String?,
                PasswordUtils.createOneOff(profile.passphrase.toCharArray()),
            )
            try {
                client.authPublickey(profile.user, keys)
            } catch (e: UserAuthException) {
                throw RelayException(
                    "The server rejected the key for ${profile.user}. " +
                        "Add the app's public key to ~/.ssh/authorized_keys (see Settings)."
                )
            }

            val sess = client.startSession()
            val cmd = sess.exec(profile.relayCommand)
            thread(name = "relay-stderr", isDaemon = true) {
                runCatching {
                    cmd.errorStream.bufferedReader().forEachLine { line ->
                        if (stderr.length < 4000) stderr.append(line).append('\n')
                    }
                }
            }
            val reader = BufferedReader(InputStreamReader(cmd.inputStream, Charsets.UTF_8))
            val first = reader.readLine()
            if (first == null) {
                Thread.sleep(300) // let stderr arrive
                val why = stderr.toString().trim().ifEmpty { "no output" }
                throw RelayException("The relay command exited immediately: $why")
            }
            val hello = JSONObject(first)
            when (hello.optString("type")) {
                "hello" -> {}
                "fatal" -> throw RelayException("Relay failed to start: ${hello.optString("error")}")
                else -> throw RelayException("Unexpected first message from relay: $first")
            }
            if (hello.optInt("protocol") != PROTOCOL_VERSION) {
                throw RelayException("Relay speaks protocol ${hello.optInt("protocol")}, app expects $PROTOCOL_VERSION")
            }

            ssh = client
            session = sess
            output = cmd.outputStream
            thread(name = "relay-reader", isDaemon = true) { readLoop(reader) }
            return Hello.from(hello)
        } catch (e: Exception) {
            runCatching { client.disconnect() }
            throw e
        }
    }

    private fun readLoop(reader: BufferedReader) {
        val reason = try {
            while (true) {
                val line = reader.readLine() ?: break
                val msg = runCatching { JSONObject(line) }.getOrNull() ?: continue
                dispatch(msg)
            }
            stderr.toString().trim().lines().lastOrNull()?.takeIf { it.isNotBlank() }
                ?: "The relay closed the connection"
        } catch (e: IOException) {
            "Connection lost: ${e.message ?: e.javaClass.simpleName}"
        }
        shutdown(reason)
    }

    private fun dispatch(msg: JSONObject) {
        if (msg.isNull("id")) return
        val id = msg.optLong("id", -1)
        val type = msg.optString("type")
        streams[id]?.let { ch ->
            ch.trySend(msg)
            if (type == "end" || type == "error") {
                streams.remove(id)
                ch.close()
            }
            return
        }
        val call = calls.remove(id) ?: return
        when (type) {
            "result" -> call.complete(msg.optJSONObject("data") ?: JSONObject())
            "error" -> call.completeExceptionally(RelayException(msg.optString("error")))
        }
    }

    private fun write(msg: JSONObject) {
        val out = output ?: throw RelayException("Not connected")
        val bytes = (msg.toString() + "\n").toByteArray(Charsets.UTF_8)
        synchronized(out) {
            out.write(bytes)
            out.flush()
        }
    }

    private fun request(id: Long, op: String, fields: Map<String, Any?>): JSONObject =
        JSONObject().apply {
            put("id", id)
            put("op", op)
            fields.forEach { (k, v) -> put(k, JSONObject.wrap(v)) }
        }

    /** Sends a non-streaming request and returns its `data`. */
    suspend fun call(op: String, fields: Map<String, Any?> = emptyMap(), timeoutMs: Long = 60_000): JSONObject {
        val id = nextId.incrementAndGet()
        val result = CompletableDeferred<JSONObject>()
        calls[id] = result
        try {
            withContext(Dispatchers.IO) { write(request(id, op, fields)) }
            return withTimeout(timeoutMs) { result.await() }
        } finally {
            calls.remove(id)
        }
    }

    suspend fun chat(session: String, text: String): ChatStream {
        val id = nextId.incrementAndGet()
        val ch = Channel<JSONObject>(Channel.UNLIMITED)
        streams[id] = ch
        try {
            withContext(Dispatchers.IO) { write(request(id, "chat", mapOf("session" to session, "text" to text))) }
        } catch (e: Exception) {
            streams.remove(id)
            throw e
        }
        return ChatStream(id, ch)
    }

    suspend fun cancel(chatId: Long) {
        call("cancel", mapOf("target" to chatId))
    }

    private fun shutdown(reason: String) {
        if (!_closed.complete(reason)) return
        val err = RelayException(reason)
        calls.values.forEach { it.completeExceptionally(err) }
        calls.clear()
        streams.values.forEach { it.close(err) }
        streams.clear()
        runCatching { output?.close() }
        runCatching { session?.close() }
        runCatching { ssh?.disconnect() }
    }

    override fun close() {
        thread(isDaemon = true) { shutdown("Disconnected") } // network I/O: keep it off the main thread
    }

    companion object {
        const val PROTOCOL_VERSION = 1
    }
}
