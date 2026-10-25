package dev.lllm.app.data

import android.content.Context

/** Where and how to reach the relay. */
data class Profile(
    val host: String = "",
    val port: Int = 22,
    val user: String = "",
    val relayCommand: String = DEFAULT_RELAY_COMMAND,
    /** OpenSSH or PEM private key text. */
    val privateKey: String = "",
    val passphrase: String = "",
    /** "ssh-ed25519 AAAA..." for the private key, shown so it can be authorized on the server. */
    val publicKey: String = "",
    /** SHA256 fingerprint of the server's host key, pinned on first connect. */
    val hostKeyFingerprint: String = "",
) {
    val isComplete: Boolean
        get() = host.isNotBlank() && user.isNotBlank() && privateKey.isNotBlank()

    val label: String
        get() = if (port == 22) "$user@$host" else "$user@$host:$port"

    companion object {
        const val DEFAULT_RELAY_COMMAND = "~/.local/bin/lllm-relay serve"
    }
}

class ProfileStore(context: Context) {
    private val prefs = context.getSharedPreferences("profile", Context.MODE_PRIVATE)
    private val box = SecretBox("lllm-profile")

    fun load(): Profile? {
        val host = prefs.getString("host", null) ?: return null
        return Profile(
            host = host,
            port = prefs.getInt("port", 22),
            user = prefs.getString("user", "") ?: "",
            relayCommand = prefs.getString("relayCommand", null) ?: Profile.DEFAULT_RELAY_COMMAND,
            privateKey = box.open(prefs.getString("privateKey", "") ?: ""),
            passphrase = box.open(prefs.getString("passphrase", "") ?: ""),
            publicKey = prefs.getString("publicKey", "") ?: "",
            hostKeyFingerprint = prefs.getString("hostKeyFingerprint", "") ?: "",
        )
    }

    fun save(p: Profile) {
        prefs.edit()
            .putString("host", p.host)
            .putInt("port", p.port)
            .putString("user", p.user)
            .putString("relayCommand", p.relayCommand)
            .putString("privateKey", box.seal(p.privateKey))
            .putString("passphrase", box.seal(p.passphrase))
            .putString("publicKey", p.publicKey)
            .putString("hostKeyFingerprint", p.hostKeyFingerprint)
            .apply()
    }
}
