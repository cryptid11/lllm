package dev.lllm.app.data

import net.schmizz.sshj.SSHClient
import net.schmizz.sshj.common.Buffer
import net.schmizz.sshj.common.KeyType
import net.schmizz.sshj.userauth.password.PasswordUtils
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.security.MessageDigest
import java.security.PublicKey
import java.security.SecureRandom
import java.util.Base64

object SshKeys {

    class Generated(val privateKey: String, val publicKey: String)

    /** Creates an Ed25519 key pair in the unencrypted OpenSSH private key format. */
    fun generateEd25519(comment: String): Generated {
        val random = SecureRandom()
        val priv = Ed25519PrivateKeyParameters(random)
        val seed = priv.encoded
        val pub = priv.generatePublicKey().encoded
        val type = "ssh-ed25519".toByteArray()

        val pubBlob = bytes { string(type); string(pub) }
        val check = random.nextInt()
        val section = bytes {
            writeInt(check)
            writeInt(check)
            string(type)
            string(pub)
            string(seed + pub)
            string(comment.toByteArray())
            var pad = 1
            while (size() % 8 != 0) write(pad++)
        }
        val body = bytes {
            write("openssh-key-v1".toByteArray())
            write(0)
            string("none".toByteArray()) // cipher
            string("none".toByteArray()) // kdf
            string(ByteArray(0)) // kdf options
            writeInt(1)
            string(pubBlob)
            string(section)
        }
        val pem = buildString {
            append("-----BEGIN OPENSSH PRIVATE KEY-----\n")
            Base64.getEncoder().encodeToString(body).chunked(70).forEach { append(it).append('\n') }
            append("-----END OPENSSH PRIVATE KEY-----\n")
        }
        val publicLine = "ssh-ed25519 ${Base64.getEncoder().encodeToString(pubBlob)} $comment"
        return Generated(pem, publicLine)
    }

    /**
     * Parses a private key (any format sshj understands) and returns its public key line.
     * Throws if the key is malformed or the passphrase is wrong.
     */
    fun publicKeyLine(privateKey: String, passphrase: String): String {
        val provider = SSHClient().loadKeys(
            privateKey.trim() + "\n",
            null as String?,
            PasswordUtils.createOneOff(passphrase.toCharArray()),
        )
        val pub = provider.public
        return "${KeyType.fromKey(pub)} ${Base64.getEncoder().encodeToString(blob(pub))}"
    }

    /** OpenSSH-style "SHA256:..." fingerprint. */
    fun fingerprint(key: PublicKey): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(blob(key))
        return "SHA256:" + Base64.getEncoder().withoutPadding().encodeToString(digest)
    }

    private fun blob(key: PublicKey): ByteArray = Buffer.PlainBuffer().putPublicKey(key).compactData

    private fun bytes(block: DataOutputStream.() -> Unit): ByteArray {
        val bos = ByteArrayOutputStream()
        DataOutputStream(bos).apply(block).flush()
        return bos.toByteArray()
    }

    private fun DataOutputStream.string(b: ByteArray) {
        writeInt(b.size)
        write(b)
    }
}
