package dev.lllm.app

import dev.lllm.app.data.SshKeys
import dev.lllm.app.relay.ChatMessage
import dev.lllm.app.relay.SessionDetail
import dev.lllm.app.relay.SessionSummary
import dev.lllm.app.relay.toolLabel
import dev.lllm.app.ui.UiState
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
        assertEquals(false, list[0].archived)
    }

    @Test
    fun parsesAgentSession() {
        // Shape produced by the relay for an agent turn waiting on approval.
        val data = JSONObject(
            """{"session": {"id": "s1", "title": "t", "provider": "p", "model": "m", "generating": true,
               "agent": true, "permission_mode": "ask", "cwd": "/home/rod",
               "pending_approval": {"call_id": "c2", "name": "bash", "arguments": {"command": "rm -rf build"}},
               "messages": [
                 {"role": "user", "content": "clean up"},
                 {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}},
                   {"id": "c2", "type": "function", "function": {"name": "bash", "arguments": "{\"command\": \"rm -rf build\"}"}}]},
                 {"role": "tool", "tool_call_id": "c1", "name": "list_dir", "content": "build/", "ok": true, "approval": "auto"}]}}"""
        )
        val s = SessionDetail.from(data)
        assertTrue(s.agent)
        assertEquals("ask", s.permissionMode)
        assertEquals("Bash(rm -rf build)", s.pendingApproval?.label)
        assertEquals("s1", s.pendingApproval?.session)
        val calls = s.messages[1].toolCalls
        assertEquals(listOf("List(.)", "Bash(rm -rf build)"), calls.map { it.label })
        val result = s.messages[2]
        assertTrue(result.isTool)
        assertEquals("c1", result.toolCallId)
        assertEquals(true, result.ok)
        assertEquals("auto", result.approval)
    }

    @Test
    fun toolLabelsAreShortOneLiners() {
        assertEquals("Edit(src/a.py)", toolLabel("edit_file", """{"path": "src/a.py", "old_string": "x"}"""))
        assertEquals("Bash(echo one)", toolLabel("bash", """{"command": "echo one\necho two"}"""))
        assertEquals(78, toolLabel("bash", """{"command": "${"x".repeat(200)}"}""").length - "Bash()".length)
        assertEquals("mystery(…)", toolLabel("mystery", "not json"))
        // Pi's tool names
        assertEquals("Edit(a.kt)", toolLabel("edit", """{"path": "a.kt", "edits": [{"oldText": "x", "newText": "y"}]}"""))
        assertEquals("List(.)", toolLabel("ls", "{}"))
        assertEquals("Find(*.py)", toolLabel("find", """{"pattern": "*.py"}"""))
        assertEquals("mcp__web__fetch(https://x.y)", toolLabel("mcp__web__fetch", """{"url": "https://x.y"}"""))
    }

    @Test
    fun sessionListHidesArchivedAndPendingDeletes() {
        fun s(id: String, archived: Boolean = false) = SessionSummary(id, id, 0.0, "p", "m", 0, "", false, archived)
        val state = UiState(sessions = listOf(s("a"), s("b"), s("c", archived = true), s("d", archived = true)),
                            pendingDelete = setOf("b", "d"))
        assertEquals(listOf("a"), state.visibleSessions.map { it.id })
        assertEquals(1, state.archivedCount)
        assertEquals(listOf("c"), state.copy(showArchived = true).visibleSessions.map { it.id })
    }
}
