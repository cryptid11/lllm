package dev.lllm.app.ui

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.lllm.app.relay.SessionSummary

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionsScreen(state: UiState, vm: AppViewModel) {
    var confirmDelete by remember { mutableStateOf<SessionSummary?>(null) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("lllm", color = Palette.Accent)
                        ConnLine(state)
                    }
                },
                actions = {
                    IconButton(onClick = { vm.refreshSessions() }, enabled = state.connected) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh")
                    }
                    IconButton(onClick = { vm.goTo(Screen.Setup) }) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings")
                    }
                },
            )
        },
        floatingActionButton = {
            if (state.connected) {
                ExtendedFloatingActionButton(
                    onClick = { vm.newSession() },
                    icon = { Icon(Icons.Default.Add, contentDescription = null) },
                    text = { Text("New session") },
                    containerColor = Palette.Accent,
                    contentColor = Palette.Bg,
                )
            }
        },
    ) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            val conn = state.conn
            if (conn is Conn.Down) {
                Column(
                    Modifier
                        .fillMaxWidth()
                        .padding(16.dp)
                        .background(Palette.Surface, RoundedCornerShape(8.dp))
                        .padding(12.dp)
                ) {
                    Text("Not connected", color = Palette.Error)
                    SelectionContainer { Text(conn.reason, color = Palette.Dim, fontSize = 13.sp) }
                    TextButton(onClick = { vm.connect() }) { Text("Retry") }
                }
            }
            if (state.connected && state.sessions.isEmpty()) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("No sessions yet", color = Palette.Dim)
                }
            }
            LazyColumn(contentPadding = PaddingValues(bottom = 96.dp)) {
                items(state.sessions, key = { it.id }) { s ->
                    SessionRow(s, onOpen = { vm.openSession(s.id) }, onLongPress = { confirmDelete = s })
                }
            }
        }
    }

    confirmDelete?.let { s ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text("Delete session?") },
            text = { Text(s.title.ifEmpty { "(untitled)" }) },
            confirmButton = {
                TextButton(onClick = { vm.deleteSession(s.id); confirmDelete = null }) {
                    Text("Delete", color = Palette.Error)
                }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = null }) { Text("Cancel") } },
        )
    }
}

@Composable
fun ConnLine(state: UiState) {
    val (dot, text) = when (val c = state.conn) {
        is Conn.Up -> Palette.Ok to "${state.profile?.label} · ${c.hello.host}"
        Conn.Connecting -> Palette.Accent to "connecting to ${state.profile?.label}…"
        is Conn.Down -> Palette.Error to "disconnected"
        Conn.Idle -> Palette.Dim to "not configured"
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(7.dp).background(dot, CircleShape))
        Spacer(Modifier.width(6.dp))
        Text(text, color = Palette.Dim, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SessionRow(s: SessionSummary, onOpen: () -> Unit, onLongPress: () -> Unit) {
    Column(
        Modifier
            .fillMaxWidth()
            .combinedClickable(onClick = onOpen, onLongClick = onLongPress)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (s.generating) {
                Box(Modifier.size(7.dp).background(Palette.Accent, CircleShape))
                Spacer(Modifier.width(6.dp))
            }
            Text(
                s.title.ifEmpty { "(untitled)" }, Modifier.weight(1f),
                maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 15.sp,
            )
        }
        if (s.preview.isNotBlank()) {
            Text(s.preview, color = Palette.Dim, fontSize = 13.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        Text(
            "${s.model} · ${s.messageCount} msgs · ${ago(s.updated)}",
            color = Color(0xFF6E6C68), fontSize = 11.sp,
        )
    }
}

fun ago(epochSec: Double): String {
    val sec = (System.currentTimeMillis() / 1000.0 - epochSec).toLong().coerceAtLeast(0)
    return when {
        sec < 60 -> "just now"
        sec < 3600 -> "${sec / 60}m ago"
        sec < 86400 -> "${sec / 3600}h ago"
        else -> "${sec / 86400}d ago"
    }
}
