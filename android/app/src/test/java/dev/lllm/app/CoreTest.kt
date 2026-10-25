package dev.lllm.app

import dev.lllm.app.data.SshKeys
import dev.lllm.app.relay.ChatMessage
import dev.lllm.app.relay.SessionDetail
import dev.lllm.app.relay.SessionSummary
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CoreTest {

    @Test
    fun generatedKeyIsReadableOpenSsh() {
        val key = SshKeys.generateEd25519("test@lllm")
        assertTrue(key.privateKey.startsWith("-----BEGIN OPENSSH PRIVATE KEY-----"))
        // sshj must parse it back to the same public key we advertise.
        val parsed = SshKeys.publicKeyLine(key.privateKey, "")
        assertEquals(key.publicKey.substringBeforeLast(' '), parsed)
        assertTrue(parsed.startsWith("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"))
    }

    @Test
    fun parsesSessionFromRelay() {
        // Shape produced by relay/lllm_relay.py (get_session).
        val data = JSONObject(
            """{"session": {"id": "20261025-1-abc", "title": "hi", "provider": "ollama", "model": "qwen",
               "system": null, "generating": false, "messages": [
                 {"role": "user", "content": "hi", "ts": 1.0},
                 {"role": "assistant", "content": "yo", "thinking": "hmm", "status": "cancelled",
                  "stats": {"eval_count": 7, "tokens_per_sec": 10.0, "wall_sec": 0.5}}]}}"""
        )
        val s = SessionDetail.from(data)
        assertEquals("qwen", s.model)
        assertEquals(2, s.messages.size)
        val reply = s.messages[1]
        assertEquals(ChatMessage("assistant", "yo", "hmm", "cancelled", null, false, 7, 10.0, 0.5), reply)
        assertNull(s.messages[0].status)
    }

    @Test
    fun parsesSessionList() {
        val data = JSONObject(
            """{"sessions": [{"id": "a", "title": null, "updated": 5.0, "provider": "ollama", "model": "m",
               "message_count": 2, "preview": "p", "generating": true}]}"""
        )
        val list = SessionSummary.listFrom(data)
        assertEquals("", list[0].title)
        assertTrue(list[0].generating)
    }
}
