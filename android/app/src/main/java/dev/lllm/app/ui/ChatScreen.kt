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
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.runtime.mutableStateMapOf
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
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.lllm.app.relay.ChatMessage
import dev.lllm.app.relay.FolderListing
import dev.lllm.app.relay.FormField
import dev.lllm.app.relay.PendingApproval
import dev.lllm.app.relay.PendingForm
import dev.lllm.app.relay.strings
import dev.lllm.app.relay.ToolCall
import org.json.JSONArray
import org.json.JSONObject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(state: UiState, vm: AppController) {
    val session = state.session
    var input by rememberSaveable { mutableStateOf("") }
    var pickingFolder by remember { mutableStateOf(false) }
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
            val form = state.pendingForm
            if (form != null) {
                FormCard(form, onSubmit = { vm.replyForm(form, it) }, onSkip = { vm.cancelForm(form) })
            } else if (pending != null) {
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
                onFolder = { pickingFolder = true },
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
                Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
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

    if (pickingFolder && session != null) {
        FolderDialog(session.cwd, vm, onDismiss = { pickingFolder = false })
    }
}

/** Browse the server's folders or type a path, then use it as the session's working folder. */
@Composable
private fun FolderDialog(start: String, vm: AppController, onDismiss: () -> Unit) {
    var path by remember { mutableStateOf(start) }
    var opened by remember { mutableStateOf(start) }
    var listing by remember { mutableStateOf<FolderListing?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(opened) {
        listing = null
        error = null
        try {
            val found = vm.listFolders(opened)
            listing = found
            path = found.path
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            error = e.message ?: "Cannot open that folder"
        }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Working folder") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    path, { path = it }, Modifier.fillMaxWidth(),
                    singleLine = true,
                    textStyle = TextStyle(fontSize = 13.sp, fontFamily = FontFamily.Monospace),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Go),
                    keyboardActions = KeyboardActions(onGo = { opened = path.trim() }),
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { listing?.parent?.let { opened = it } }, enabled = listing?.parent != null) {
                        Text("↑ Up", fontSize = 13.sp)
                    }
                    OutlinedButton(onClick = { opened = path.trim() }) { Text("Open", fontSize = 13.sp) }
                }
                error?.let { Text(it, color = Palette.Error, fontSize = 12.sp) }
                val current = listing
                Column(Modifier.heightIn(max = 280.dp).verticalScroll(rememberScrollState())) {
                    when {
                        current == null && error == null -> Text("Loading…", color = Palette.Dim, fontSize = 13.sp)
                        current?.dirs?.isEmpty() == true -> Text("No subfolders", color = Palette.Dim, fontSize = 13.sp)
                    }
                    current?.let { folder ->
                        folder.dirs.forEach { name ->
                            Text(
                                "$name/",
                                Modifier
                                    .fillMaxWidth()
                                    .clickable { opened = folder.path.trimEnd('/') + "/" + name }
                                    .padding(vertical = 9.dp),
                                color = Palette.Text, fontSize = 14.sp,
                            )
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { vm.setFolder(path.trim()); onDismiss() }, enabled = path.isNotBlank()) {
                Text("Use this folder")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
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
            val engine = when (session.engine) {
                "opencode" -> "opencode"
                "pi" -> "pi agent"
                else -> "agent"
            }
            Text("  $engine · ${session.permissionMode} · ${session.cwd}", color = Palette.Dim, fontSize = 12.sp)
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
    fun diff(old: String, new: String) = "- " + old.replace("\n", "\n- ") + "\n+ " + new.replace("\n", "\n+ ")
    val detail = when (pending.name) {
        "bash", "shell" -> args.optString("command")
        "write_file", "write" -> args.optString("content").let { if (it.length > 1500) it.take(1500) + "\n…" else it }
        "edit_file" -> diff(args.optString("old_string"), args.optString("new_string"))
        // OpenCode's edit has one replacement, Pi's a list of {oldText, newText}.
        "edit" -> if (args.has("oldString")) diff(args.optString("oldString"), args.optString("newString")) else args.optJSONArray("edits")?.let { edits ->
            (0 until edits.length()).mapNotNull { edits.optJSONObject(it) }
                .joinToString("\n⋯\n") { diff(it.optString("oldText"), it.optString("newText")) }
        } ?: args.toString(2)
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
        Text(
            if (pending.save.isEmpty()) "Always = allow ${pending.name} for the rest of this session"
            else "Always = OpenCode remembers this for the whole project: ${pending.name} ${pending.save.joinToString(", ")}",
            color = Palette.Dim, fontSize = 11.sp,
        )
    }
}

/** A question from the agent, which waits until it is answered or skipped. */
@Composable
private fun FormCard(form: PendingForm, onSubmit: (Map<String, Any?>) -> Unit, onSkip: () -> Unit) {
    val answers = remember(form.id) {
        mutableStateMapOf<String, Any?>().apply {
            form.fields.forEach { f ->
                val default = f.default
                when {
                    default is JSONArray -> put(f.key, default.strings().toSet())
                    default != null -> put(f.key, default)
                }
            }
        }
    }
    fun visible(f: FormField) = f.type != "external" && f.conditions.all { (key, op, value) ->
        val same = answers[key]?.toString() == value?.toString()
        if (op == "neq") !same else same
    }
    fun valueOf(f: FormField): Any? = when (f.type) {
        "integer" -> (answers[f.key] as? String)?.trim()?.toLongOrNull() ?: (answers[f.key] as? Number)
        "number" -> (answers[f.key] as? String)?.trim()?.toDoubleOrNull() ?: (answers[f.key] as? Number)
        "multiselect" -> (answers[f.key] as? Set<*>)?.takeIf { it.isNotEmpty() }?.toList()
        "boolean" -> answers[f.key] as? Boolean
        else -> (answers[f.key] as? String)?.takeIf { it.isNotBlank() }
    }
    val fields = form.fields.filter(::visible)
    val complete = fields.all { f ->
        val needed = f.required || (form.isQuestion && f.options.isNotEmpty())
        val typed = answers[f.key] as? String
        valueOf(f) != null || (!needed && typed.isNullOrBlank())
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
        Text(form.title.ifEmpty { "Question" }, color = Palette.Accent, fontSize = 14.sp, fontWeight = FontWeight.Bold)
        Column(
            Modifier.heightIn(max = 340.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            form.fields.filter { it.type == "external" }.forEach { f ->
                SelectionContainer { Text("${f.title.ifEmpty { f.key }}: ${f.url}", color = Palette.Code, fontSize = 12.sp) }
            }
            fields.forEach { f -> FormFieldView(f, answers) }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = { onSubmit(fields.mapNotNull { f -> valueOf(f)?.let { f.key to it } }.toMap()) },
                enabled = complete,
            ) { Text("Send") }
            OutlinedButton(onClick = onSkip) { Text("Skip", fontSize = 13.sp) }
        }
    }
}

@Composable
private fun FormFieldView(f: FormField, answers: MutableMap<String, Any?>) {
    @Composable
    fun choice(label: String, description: String, selected: Boolean, onClick: () -> Unit) {
        Row(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 5.dp)) {
            Text(if (selected) "● " else "○ ", color = if (selected) Palette.Accent else Palette.Dim, fontSize = 13.sp)
            Column {
                Text(label, color = Palette.Text, fontSize = 13.sp)
                if (description.isNotBlank()) Text(description, color = Palette.Dim, fontSize = 11.sp)
            }
        }
    }

    @Composable
    fun textInput(value: String, hint: String, onChange: (String) -> Unit) {
        BasicTextField(
            value = value,
            onValueChange = onChange,
            modifier = Modifier
                .fillMaxWidth()
                .border(1.dp, Palette.Border, RoundedCornerShape(6.dp))
                .padding(8.dp),
            textStyle = TextStyle(color = Palette.Text, fontSize = 13.sp, fontFamily = FontFamily.Monospace),
            cursorBrush = SolidColor(Palette.Accent),
            decorationBox = { field ->
                if (value.isEmpty() && hint.isNotEmpty()) Text(hint, color = Palette.Dim, fontSize = 13.sp)
                field()
            },
        )
    }

    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        if (f.title.isNotBlank()) Text(f.title, color = Palette.Text, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        if (f.description.isNotBlank()) Text(f.description, color = Palette.Dim, fontSize = 12.sp)
        when {
            f.type == "boolean" -> listOf(true to "Yes", false to "No").forEach { (value, label) ->
                choice(label, "", answers[f.key] == value) { answers[f.key] = value }
            }
            f.type == "multiselect" -> {
                @Suppress("UNCHECKED_CAST")
                val chosen = answers[f.key] as? Set<String> ?: emptySet()
                f.options.forEach { o ->
                    choice(o.label, o.description, o.value in chosen) {
                        answers[f.key] = if (o.value in chosen) chosen - o.value else chosen + o.value
                    }
                }
            }
            f.options.isNotEmpty() -> {
                val current = answers[f.key] as? String
                f.options.forEach { o -> choice(o.label, o.description, current == o.value) { answers[f.key] = o.value } }
                if (f.custom) {
                    val typed = current?.takeIf { value -> f.options.none { it.value == value } }.orEmpty()
                    textInput(typed, "or type your own answer…") { answers[f.key] = it }
                }
            }
            else -> textInput(answers[f.key]?.toString().orEmpty(), f.placeholder) { answers[f.key] = it }
        }
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
    onFolder: () -> Unit,
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
        val session = state.session
        if (session?.agent == true && session.cwd.isNotEmpty()) {
            // A Pi session cannot leave the folder its conversation started in.
            val locked = session.engine == "pi" && state.messages.isNotEmpty()
            val shown = if (session.cwd.length > 44) "…" + session.cwd.takeLast(43) else session.cwd
            Text(
                if (locked) "folder  $shown" else "folder  $shown  ✎",
                Modifier
                    .fillMaxWidth()
                    .clickable(enabled = state.connected && !state.generating && !locked) { onFolder() }
                    .padding(start = 4.dp, top = 5.dp, bottom = 2.dp),
                color = if (locked) Palette.Dim else Palette.Accent, fontSize = 11.sp, maxLines = 1,
            )
        }
    }
}
