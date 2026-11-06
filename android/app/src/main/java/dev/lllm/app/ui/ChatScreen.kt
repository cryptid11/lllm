package dev.lllm.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.lllm.app.relay.ChatMessage
import dev.lllm.app.relay.PendingApproval
import dev.lllm.app.relay.ToolCall
import org.json.JSONObject
import kotlinx.coroutines.delay

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(state: UiState, vm: AppController) {
    val session = state.session
    var input by rememberSaveable { mutableStateOf("") }
    val listState = rememberLazyListState()
    var follow by remember { mutableStateOf(true) }

    // Stick to the bottom while text streams in, unless the user scrolled up to read.
    LaunchedEffect(listState.isScrollInProgress) {
        if (!listState.isScrollInProgress) follow = !listState.canScrollForward
    }
    val last = state.messages.lastOrNull()
    val liveChars = state.liveOutput.values.sumOf { it.length }
    LaunchedEffect(state.messages.size, last?.content?.length, last?.thinking?.length, state.generating,
                   liveChars, state.pendingApproval) {
        if (follow) {
            val count = listState.layoutInfo.totalItemsCount
            if (count > 0) listState.scrollToItem(count - 1)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(
                            session?.title?.ifEmpty { null } ?: "New session",
                            maxLines = 1, overflow = TextOverflow.Ellipsis, fontSize = 16.sp,
                        )
                        ConnLine(state)
                    }
                },
                navigationIcon = {
                    IconButton(onClick = { vm.back() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Sessions")
                    }
                },
            )
        },
        bottomBar = {
            val pending = state.pendingApproval
            if (pending != null) {
                ApprovalCard(
                    pending,
                    onAllow = { vm.approve("allow") },
                    onAlways = { vm.approve("allow_session") },
                    onDeny = { note ->
                        vm.approve("deny", note)
                        input = ""
                    },
                    note = input,
                    onNote = { input = it },
                )
            } else PromptBar(
                input = input,
                onInput = { input = it },
                state = state,
                onSubmit = {
                    if (vm.submit(input)) {
                        input = ""
                        follow = true
                    }
                },
                onStop = { vm.stop() },
                onModel = { vm.loadModels() },
                onMode = { vm.cycleMode() },
            )
        },
    ) { pad ->
        if (session == null) {
            Box(Modifier.padding(pad).fillMaxSize(), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = Palette.Accent)
            }
            return@Scaffold
        }
        LazyColumn(
            Modifier.padding(pad).fillMaxSize(),
            state = listState,
            contentPadding = PaddingValues(horizontal = 14.dp, vertical = 10.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            item(key = "welcome") { Welcome(state) }
            // Tool results are shown under the call that produced them, not as messages of their own.
            val results = state.messages.filter { it.isTool }.associateBy { it.toolCallId }
            val replyIndex = state.messages.indexOfLast { it.role == "assistant" }
            itemsIndexed(state.messages) { i, m ->
                val streaming = state.generating && i == replyIndex
                when {
                    m.isUser -> UserMessage(m)
                    m.isTool -> {}
                    else -> AssistantMessage(m, streaming, results, state.liveOutput)
                }
            }
            if (state.generating) item(key = "spinner") { Working(state.generatingSince ?: 0L) }
            item(key = "end") { Spacer(Modifier.size(4.dp)) }
        }
    }

    // Shown while a model list is loaded (by /model or tapping the model name).
    val models = state.models
    if (models != null) {
        AlertDialog(
            onDismissRequest = { vm.closeModels() },
            title = { Text("Model") },
            text = {
                Column {
                    if (models.isEmpty()) Text("No models on the backend", color = Palette.Dim)
                    models.forEach { m ->
                        val current = m == session?.model
                        Text(
                            (if (current) "● " else "  ") + m,
                            Modifier
                                .fillMaxWidth()
                                .clickable { vm.closeModels(); vm.setModel(m) }
                                .padding(vertical = 10.dp),
                            color = if (current) Palette.Accent else Palette.Text,
                        )
                    }
                }
            },
            confirmButton = { TextButton(onClick = { vm.closeModels() }) { Text("Close") } },
        )
    }
}

@Composable
private fun Welcome(state: UiState) {
    val session = state.session ?: return
    Column(
        Modifier
            .fillMaxWidth()
            .border(1.dp, Palette.Accent, RoundedCornerShape(8.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        Text("✻ Welcome to lllm", color = Palette.Text)
        Text("  ${session.provider} · ${session.model}", color = Palette.Dim, fontSize = 12.sp)
        Text("  ${state.profile?.label}", color = Palette.Dim, fontSize = 12.sp)
        if (session.agent) {
            Text("  agent · ${session.permissionMode} · ${session.cwd}", color = Palette.Dim, fontSize = 12.sp)
        } else {
            Text("  chat only (/agent to give it tools)", color = Palette.Dim, fontSize = 12.sp)
        }
        Text("  /help for commands", color = Palette.Dim, fontSize = 12.sp)
    }
}

@Composable
private fun UserMessage(m: ChatMessage) {
    SelectionContainer {
        Row(
            Modifier
                .fillMaxWidth()
                .background(Palette.Surface, RoundedCornerShape(6.dp))
                .padding(horizontal = 10.dp, vertical = 8.dp)
        ) {
            Text("> ", color = Palette.Dim, fontSize = 14.sp)
            Text(m.content, color = Palette.Text, fontSize = 14.sp, lineHeight = 20.sp)
        }
    }
}

@Composable
private fun AssistantMessage(
    m: ChatMessage,
    streaming: Boolean,
    results: Map<String?, ChatMessage>,
    live: Map<String, String>,
) {
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (m.thinking.isNotBlank()) Thinking(m.thinking, startExpanded = streaming && m.content.isEmpty())
        if (m.content.isNotEmpty() || (!streaming && m.toolCalls.isEmpty())) {
            Row {
                Text("● ", color = if (streaming) Palette.Dim else Palette.Accent, fontSize = 14.sp)
                SelectionContainer(Modifier.weight(1f)) {
                    Markdown(m.content.ifEmpty { " " }, Palette.Text)
                }
            }
        }
        m.toolCalls.forEach { call -> ToolCallView(call, results[call.id], live[call.id]) }
        val note = when (m.status) {
            "cancelled" -> "Interrupted by user" to Palette.Error
            "interrupted" -> "Interrupted (relay stopped)" to Palette.Error
            "disconnected" -> "Connection lost; the reply continues on the server" to Palette.Accent
            "error" -> "Error: ${m.error ?: "unknown"}" to Palette.Error
            else -> null
        }
        val stats = listOfNotNull(
            m.evalCount?.let { "$it tokens" },
            m.tokensPerSec?.let { "%.1f tok/s".format(it) },
            m.wallSec?.let { "%.1fs".format(it) },
        ).joinToString(" · ")
        when {
            note != null -> Tail(note.first, note.second)
            stats.isNotEmpty() && !streaming -> Tail(stats, Palette.Dim)
        }
    }
}

/** "● Bash(ls -la)" then "⎿ output", collapsed to a few lines until tapped. */
@Composable
private fun ToolCallView(call: ToolCall, result: ChatMessage?, live: String?) {
    var expanded by remember { mutableStateOf(false) }
    val (dot, dotColor) = when {
        result == null -> "○" to Palette.Dim
        result.ok == true -> "●" to Palette.Ok
        else -> "●" to Palette.Error
    }
    val output = (result?.content ?: live).orEmpty().trimEnd()
    val lines = output.lines()
    val shown = if (expanded || lines.size <= 4) output else lines.take(4).joinToString("\n")
    Column(Modifier.fillMaxWidth().clickable(enabled = output.isNotEmpty()) { expanded = !expanded }) {
        Row {
            Text("$dot ", color = dotColor, fontSize = 14.sp)
            Text(call.label, color = Palette.Text, fontSize = 14.sp, fontWeight = FontWeight.Bold)
        }
        val how = when (result?.approval) {
            "allow", "allow_session" -> "approved"
            "deny" -> "denied"
            "timeout" -> "not approved in time"
            "cancelled" -> "not run"
            else -> null
        }
        if (output.isNotEmpty() || how != null) {
            Row(Modifier.padding(start = 4.dp, top = 2.dp)) {
                Text("⎿  ", color = Palette.Dim, fontSize = 12.sp)
                Column {
                    if (output.isNotEmpty()) {
                        Text(shown, color = Palette.Dim, fontSize = 12.sp, lineHeight = 16.sp)
                        if (!expanded && lines.size > 4) {
                            Text("… +${lines.size - 4} lines (tap to expand)", color = Palette.Dim, fontSize = 11.sp)
                        }
                    }
                    if (how != null && how != "approved") Text(how, color = Palette.Error, fontSize = 11.sp)
                }
            }
        }
    }
}

/** Replaces the prompt while a tool call waits for a decision. The prompt text becomes the deny note. */
@Composable
private fun ApprovalCard(
    pending: PendingApproval,
    onAllow: () -> Unit,
    onAlways: () -> Unit,
    onDeny: (String?) -> Unit,
    note: String,
    onNote: (String) -> Unit,
) {
    val args = remember(pending.arguments) { runCatching { JSONObject(pending.arguments) }.getOrDefault(JSONObject()) }
    val detail = when (pending.name) {
        "bash" -> args.optString("command")
        "write_file" -> args.optString("content").let { if (it.length > 1500) it.take(1500) + "\n…" else it }
        "edit_file" -> "- " + args.optString("old_string").replace("\n", "\n- ") +
            "\n+ " + args.optString("new_string").replace("\n", "\n+ ")
        else -> args.toString(2)
    }
    Column(
        Modifier
            .background(Palette.Bg)
            .navigationBarsPadding()
            .imePadding()
            .padding(horizontal = 10.dp, vertical = 6.dp)
            .border(1.dp, Palette.Accent, RoundedCornerShape(8.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text("Allow ${pending.label}?", color = Palette.Accent, fontSize = 14.sp, fontWeight = FontWeight.Bold)
        if (detail.isNotBlank()) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .heightIn(max = 220.dp)
                    .background(Palette.Surface, RoundedCornerShape(6.dp))
                    .verticalScroll(rememberScrollState())
                    .padding(8.dp)
            ) {
                Text(detail, color = Palette.Code, fontSize = 12.sp, lineHeight = 16.sp)
            }
        }
        BasicTextField(
            value = note,
            onValueChange = onNote,
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, Palette.Border, RoundedCornerShape(6.dp))
                .padding(8.dp),
            textStyle = TextStyle(color = Palette.Text, fontSize = 13.sp, fontFamily = FontFamily.Monospace),
            cursorBrush = SolidColor(Palette.Accent),
            decorationBox = { field ->
                if (note.isEmpty()) Text("optional note if you deny…", color = Palette.Dim, fontSize = 13.sp)
                field()
            },
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = onAllow, contentPadding = PaddingValues(horizontal = 14.dp)) { Text("Allow") }
            OutlinedButton(onClick = onAlways, contentPadding = PaddingValues(horizontal = 12.dp)) {
                Text("Always", fontSize = 13.sp)
            }
            OutlinedButton(onClick = { onDeny(note.trim().ifEmpty { null }) }, contentPadding = PaddingValues(horizontal = 12.dp)) {
                Text("Deny", color = Palette.Error, fontSize = 13.sp)
            }
        }
        Text("Always = allow ${pending.name} for the rest of this session", color = Palette.Dim, fontSize = 11.sp)
    }
}

@Composable
private fun Thinking(text: String, startExpanded: Boolean) {
    var expanded by remember { mutableStateOf(startExpanded) }
    Column(Modifier.clickable { expanded = !expanded }) {
        Text(
            (if (expanded) "▾ " else "▸ ") + "✻ Thinking",
            color = Palette.Dim, fontSize = 13.sp, fontStyle = FontStyle.Italic,
        )
        if (expanded) {
            Text(
                text.trim(), Modifier.padding(start = 16.dp, top = 4.dp),
                color = Palette.Dim, fontSize = 12.sp, fontStyle = FontStyle.Italic, lineHeight = 17.sp,
            )
        }
    }
}

@Composable
private fun Tail(text: String, color: androidx.compose.ui.graphics.Color) {
    Text("  ⎿  $text", color = color, fontSize = 12.sp)
}

private val spinnerFrames = listOf("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")

@Composable
private fun Working(since: Long) {
    var frame by remember { mutableIntStateOf(0) }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(120)
            frame = (frame + 1) % spinnerFrames.size
            now = System.currentTimeMillis()
        }
    }
    val secs = ((now - since) / 1000).coerceAtLeast(0)
    Row {
        Text("${spinnerFrames[frame]} ", color = Palette.Accent, fontSize = 14.sp)
        Text("Generating… ", color = Palette.Accent, fontSize = 14.sp)
        Text("(${secs}s · ■ to interrupt)", color = Palette.Dim, fontSize = 13.sp)
    }
}

@Composable
private fun PromptBar(
    input: String,
    onInput: (String) -> Unit,
    state: UiState,
    onSubmit: () -> Unit,
    onStop: () -> Unit,
    onModel: () -> Unit,
    onMode: () -> Unit,
) {
    Column(
        Modifier
            .background(Palette.Bg)
            .navigationBarsPadding()
            .imePadding()
            .padding(horizontal = 10.dp, vertical = 6.dp)
    ) {
        // Slash-command suggestions, like Claude Code's command palette.
        if (input.startsWith("/") && !input.contains(' ')) {
            val matches = AppController.COMMANDS.filter { it.first.startsWith(input.lowercase()) }
            Column(Modifier.padding(bottom = 6.dp)) {
                matches.forEach { (cmd, help) ->
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .clickable { onInput("$cmd ") }
                            .padding(vertical = 6.dp, horizontal = 4.dp)
                    ) {
                        Text(cmd, color = Palette.Accent, fontSize = 13.sp, modifier = Modifier.width(110.dp))
                        Text(help, color = Palette.Dim, fontSize = 13.sp)
                    }
                }
            }
        }
        Row(
            Modifier
                .fillMaxWidth()
                .border(1.dp, Palette.Border, RoundedCornerShape(8.dp))
                .padding(start = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text("> ", color = Palette.Dim, fontSize = 15.sp)
            Box(Modifier.weight(1f).padding(vertical = 12.dp)) {
                if (input.isEmpty()) {
                    Text(
                        if (state.connected) "Message ${state.session?.model ?: ""}…" else "Not connected",
                        color = Palette.Dim, fontSize = 15.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                }
                BasicTextField(
                    value = input,
                    onValueChange = onInput,
                    modifier = Modifier.fillMaxWidth().heightIn(max = 160.dp),
                    textStyle = TextStyle(color = Palette.Text, fontSize = 15.sp, fontFamily = FontFamily.Monospace),
                    cursorBrush = SolidColor(Palette.Accent),
                )
            }
            if (state.generating) {
                IconButton(onClick = onStop) {
                    Icon(Icons.Default.Close, contentDescription = "Interrupt", tint = Palette.Error)
                }
            } else {
                IconButton(onClick = onSubmit, enabled = input.isNotBlank() && (state.connected || input.startsWith("/"))) {
                    Icon(
                        Icons.AutoMirrored.Filled.Send, contentDescription = "Send",
                        tint = if (input.isNotBlank()) Palette.Accent else Palette.Dim,
                    )
                }
            }
        }
        Row(Modifier.fillMaxWidth().padding(top = 4.dp, start = 4.dp)) {
            Text(
                state.session?.model ?: "",
                Modifier.clickable(enabled = state.connected) { onModel() },
                color = Palette.Dim, fontSize = 11.sp,
            )
            val session = state.session
            if (session?.agent == true) {
                // Tap to cycle ask → auto-read → auto, like shift+tab in Claude Code.
                Text(
                    "  ·  ${session.permissionMode}",
                    Modifier.clickable(enabled = state.connected) { onMode() },
                    color = if (session.permissionMode == "auto") Palette.Error else Palette.Accent,
                    fontSize = 11.sp,
                )
            }
            Spacer(Modifier.weight(1f))
            Text("/ for commands", color = Palette.Dim, fontSize = 11.sp)
        }
    }
}
