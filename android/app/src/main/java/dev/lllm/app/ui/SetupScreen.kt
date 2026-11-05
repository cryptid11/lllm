package dev.lllm.app.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.lllm.app.data.Profile
import dev.lllm.app.data.SshKeys
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SetupScreen(state: UiState, vm: AppViewModel) {
    val initial = state.profile ?: Profile()
    var host by rememberSaveable { mutableStateOf(initial.host) }
    var port by rememberSaveable { mutableStateOf(initial.port.toString()) }
    var user by rememberSaveable { mutableStateOf(initial.user) }
    var command by rememberSaveable { mutableStateOf(initial.relayCommand) }
    var privateKey by rememberSaveable { mutableStateOf(initial.privateKey) }
    var passphrase by rememberSaveable { mutableStateOf(initial.passphrase) }
    var publicKey by rememberSaveable { mutableStateOf(initial.publicKey) }
    var hostKey by rememberSaveable { mutableStateOf(initial.hostKeyFingerprint) }
    var restrict by rememberSaveable { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var pasting by remember { mutableStateOf(false) }

    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    fun useKey(pem: String, pass: String) {
        scope.launch {
            try {
                val pub = withContext(Dispatchers.Default) { SshKeys.publicKeyLine(pem, pass) }
                privateKey = pem
                publicKey = "$pub lllm-android"
                error = null
            } catch (e: Exception) {
                error = "Could not read that key: ${e.message ?: e.javaClass.simpleName}" +
                    if (pass.isEmpty()) " (encrypted? enter the passphrase first)" else ""
            }
        }
    }

    val importer = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri != null) {
            val text = runCatching {
                context.contentResolver.openInputStream(uri)!!.bufferedReader().use { it.readText() }
            }.getOrNull()
            if (text == null) error = "Could not read the file" else useKey(text, passphrase)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Connection") },
                navigationIcon = {
                    if (state.profile?.isComplete == true) {
                        IconButton(onClick = { vm.back() }) {
                            Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                        }
                    }
                },
            )
        },
    ) { pad ->
        Column(
            Modifier
                .padding(pad)
                .fillMaxSize()
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Section("Server")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    host, { host = it.trim() }, Modifier.weight(1f),
                    label = { Text("Host / IP") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                )
                OutlinedTextField(
                    port, { port = it.filter(Char::isDigit).take(5) }, Modifier.width(90.dp),
                    label = { Text("Port") }, singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                )
            }
            OutlinedTextField(
                user, { user = it.trim() }, Modifier.fillMaxWidth(),
                label = { Text("User") }, singleLine = true,
            )
            OutlinedTextField(
                command, { command = it }, Modifier.fillMaxWidth(),
                label = { Text("Relay command") }, singleLine = true,
            )

            Section("Key")
            if (privateKey.isNotBlank() && publicKey.isNotBlank()) {
                Text("✓ ${publicKey.substringBefore(' ')} key loaded", color = Palette.Ok, fontSize = 13.sp)
            } else {
                Text(
                    "Generate a key on this phone (recommended: it never leaves the device), or import one.",
                    color = Palette.Dim, fontSize = 13.sp,
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = {
                    val k = SshKeys.generateEd25519("lllm-android")
                    privateKey = k.privateKey
                    publicKey = k.publicKey
                    passphrase = ""
                    error = null
                }) { Text("Generate") }
                OutlinedButton(onClick = { importer.launch(arrayOf("*/*")) }) { Text("File") }
                OutlinedButton(onClick = { pasting = true }) { Text("Paste") }
            }
            OutlinedTextField(
                passphrase, { passphrase = it }, Modifier.fillMaxWidth(),
                label = { Text("Key passphrase (if encrypted)") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
            )

            if (publicKey.isNotBlank()) {
                val line = if (restrict) "command=\"${serveCommand(command)}\",restrict $publicKey" else publicKey
                Text("Add this line to ~/.ssh/authorized_keys on the server:", color = Palette.Dim, fontSize = 13.sp)
                // Tap to copy, or long-press to select part of it.
                SelectionContainer {
                    Text(
                        line,
                        Modifier
                            .fillMaxWidth()
                            .background(Palette.Surface, RoundedCornerShape(6.dp))
                            .clickable { copyText(context, "authorized_keys line", line) }
                            .padding(10.dp),
                        color = Palette.Code, fontSize = 12.sp,
                    )
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Switch(restrict, { restrict = it })
                    Spacer(Modifier.width(8.dp))
                    Text("Only allow this key to run the relay", Modifier.weight(1f), fontSize = 13.sp)
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = { copyText(context, "authorized_keys line", line) }) { Text("Copy line") }
                    OutlinedButton(onClick = { shareText(context, line) }) { Text("Share") }
                }
            }

            if (hostKey.isNotBlank()) {
                Section("Trusted host key")
                Text(hostKey, color = Palette.Dim, fontSize = 12.sp)
                TextButton(onClick = { hostKey = "" }) { Text("Forget (re-pin on next connect)") }
            }

            error?.let { Text(it, color = Palette.Error, fontSize = 13.sp) }

            Button(
                onClick = {
                    val p = port.toIntOrNull()
                    error = when {
                        host.isBlank() || user.isBlank() -> "Host and user are required"
                        p == null || p !in 1..65535 -> "Invalid port"
                        privateKey.isBlank() -> "Generate or import a key first"
                        else -> null
                    }
                    if (error == null) {
                        vm.saveProfile(
                            Profile(host, p!!, user, command.trim(), privateKey, passphrase, publicKey, hostKey)
                        )
                    }
                },
                Modifier.fillMaxWidth(),
            ) { Text("Save & connect") }
            Spacer(Modifier.height(24.dp))
        }
    }

    if (pasting) {
        var text by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { pasting = false },
            title = { Text("Paste private key") },
            text = {
                OutlinedTextField(
                    text, { text = it }, Modifier.fillMaxWidth().height(220.dp),
                    placeholder = { Text("-----BEGIN OPENSSH PRIVATE KEY-----", fontSize = 11.sp) },
                    textStyle = androidx.compose.ui.text.TextStyle(fontSize = 11.sp),
                )
            },
            confirmButton = {
                TextButton(onClick = { pasting = false; useKey(text, passphrase) }) { Text("Use key") }
            },
            dismissButton = { TextButton(onClick = { pasting = false }) { Text("Cancel") } },
        )
    }
}

/** The relay command, escaped for use inside command="..." in authorized_keys. */
private fun serveCommand(command: String): String = command.trim().replace("\"", "\\\"")

@Composable
private fun Section(title: String) {
    Text(title.uppercase(), color = Palette.Accent, fontSize = 12.sp, modifier = Modifier.padding(top = 8.dp))
}
