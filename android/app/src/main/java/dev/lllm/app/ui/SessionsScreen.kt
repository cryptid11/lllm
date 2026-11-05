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
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Snackbar
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.material3.SwipeToDismissBox
import androidx.compose.material3.SwipeToDismissBoxValue
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberSwipeToDismissBoxState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.lllm.app.relay.SessionSummary
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SessionsScreen(state: UiState, vm: AppViewModel) {
    var renaming by remember { mutableStateOf<SessionSummary?>(null) }
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val archivedView = state.showArchived

    /** Shows "<message> · Undo", replacing any snackbar already up so each undo window starts now. */
    fun offerUndo(message: String, undo: () -> Unit) {
        snackbar.currentSnackbarData?.dismiss()
        scope.launch {
            val result = snackbar.showSnackbar(message, actionLabel = "Undo", duration = SnackbarDuration.Short)
            if (result == SnackbarResult.ActionPerformed) undo()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(if (archivedView) "Archived" else "lllm", color = Palette.Accent)
                        ConnLine(state)
                    }
                },
                navigationIcon = {
                    if (archivedView) {
                        IconButton(onClick = { vm.showArchived(false) }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Active sessions")
                        }
                    }
                },
                actions = {
                    if (!archivedView && state.archivedCount > 0) {
                        TextButton(onClick = { vm.showArchived(true) }) { Text("Archived ${state.archivedCount}") }
                    }
                    IconButton(onClick = { vm.refreshSessions() }, enabled = state.connected) {
                        Icon(Icons.Default.Refresh, contentDescription = "Refresh")
                    }
                    IconButton(onClick = { vm.goTo(Screen.Setup) }) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings")
                    }
                },
            )
        },
        snackbarHost = {
            SnackbarHost(snackbar) { data ->
                Snackbar(data, containerColor = Palette.Raised, contentColor = Palette.Text, actionColor = Palette.Accent)
            }
        },
        floatingActionButton = {
            if (state.connected && !archivedView) {
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
            val sessions = state.visibleSessions
            if (state.connected && sessions.isEmpty()) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(if (archivedView) "Nothing archived" else "No sessions yet", color = Palette.Dim)
                }
            }
            LazyColumn(contentPadding = PaddingValues(bottom = 96.dp)) {
                items(sessions, key = { it.id }) { s ->
                    val name = s.title.ifEmpty { "(untitled)" }
                    SwipeableSessionRow(
                        s,
                        archivedView = archivedView,
                        onOpen = { vm.openSession(s.id) },
                        onLongPress = { renaming = s },
                        onDelete = {
                            vm.deleteSoon(s.id)
                            offerUndo("Deleted “$name”") { vm.undoDelete(s.id) }
                        },
                        onToggleArchive = {
                            val archive = !s.archived
                            vm.setArchived(s.id, archive)
                            offerUndo(if (archive) "Archived “$name”" else "Restored “$name”") {
                                vm.setArchived(s.id, !archive)
                            }
                        },
                    )
                }
                if (sessions.isNotEmpty()) {
                    item(key = "hint") {
                        Text(
                            "hold to rename · swipe ← delete · swipe → ${if (archivedView) "unarchive" else "archive"}",
                            Modifier.fillMaxWidth().padding(16.dp),
                            color = Color(0xFF5E5C58), fontSize = 11.sp,
                        )
                    }
                }
            }
        }
    }

    renaming?.let { s ->
        var title by remember(s.id) { mutableStateOf(s.title) }
        AlertDialog(
            onDismissRequest = { renaming = null },
            title = { Text("Rename session") },
            text = {
                OutlinedTextField(title, { title = it }, Modifier.fillMaxWidth(), label = { Text("Title") }, singleLine = true)
            },
            confirmButton = {
                TextButton(
                    onClick = { vm.renameSession(s.id, title); renaming = null },
                    enabled = title.isNotBlank(),
                ) { Text("Save") }
            },
            dismissButton = { TextButton(onClick = { renaming = null }) { Text("Cancel") } },
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

/** Swipe left (end to start) deletes; swipe right archives, or unarchives in the archived view. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SwipeableSessionRow(
    s: SessionSummary,
    archivedView: Boolean,
    onOpen: () -> Unit,
    onLongPress: () -> Unit,
    onDelete: () -> Unit,
    onToggleArchive: () -> Unit,
) {
    val swipe = rememberSwipeToDismissBoxState(positionalThreshold = { distance -> distance * 0.4f })
    val delete by rememberUpdatedState(onDelete)
    val toggleArchive by rememberUpdatedState(onToggleArchive)
    LaunchedEffect(swipe) {
        // The swipe position is saved per row key, so a row restored by Undo would come back
        // already "dismissed". Reset it, and only act on swipes that happen from here on.
        if (swipe.currentValue != SwipeToDismissBoxValue.Settled) swipe.snapTo(SwipeToDismissBoxValue.Settled)
        snapshotFlow { swipe.currentValue }.drop(1).collect { value ->
            when (value) {
                SwipeToDismissBoxValue.EndToStart -> delete()
                SwipeToDismissBoxValue.StartToEnd -> toggleArchive()
                SwipeToDismissBoxValue.Settled -> {}
            }
        }
    }

    SwipeToDismissBox(
        state = swipe,
        backgroundContent = {
            val (color, label, align) = when (swipe.dismissDirection) {
                SwipeToDismissBoxValue.EndToStart -> Triple(Palette.Error, "Delete", Alignment.CenterEnd)
                SwipeToDismissBoxValue.StartToEnd ->
                    Triple(Palette.Accent, if (archivedView) "Unarchive" else "Archive", Alignment.CenterStart)
                SwipeToDismissBoxValue.Settled -> Triple(Color.Transparent, "", Alignment.Center)
            }
            Box(Modifier.fillMaxSize().background(color).padding(horizontal = 24.dp), contentAlignment = align) {
                Text(label, color = Palette.Bg, fontSize = 14.sp)
            }
        },
    ) {
        SessionRow(s, onOpen, onLongPress)
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun SessionRow(s: SessionSummary, onOpen: () -> Unit, onLongPress: () -> Unit) {
    Column(
        Modifier
            .fillMaxWidth()
            .background(Palette.Bg) // opaque, so the swipe background only shows while swiping
            .combinedClickable(onClick = onOpen, onLongClick = onLongPress, onLongClickLabel = "Rename")
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
