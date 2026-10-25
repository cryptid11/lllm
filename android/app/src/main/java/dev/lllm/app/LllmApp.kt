package dev.lllm.app

import android.app.Application
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

class LllmApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Android ships a stripped-down "BC" provider; sshj needs the full one.
        // Appended (not inserted first) so platform crypto such as the Keystore keeps its defaults.
        Security.removeProvider("BC")
        Security.addProvider(BouncyCastleProvider())
    }
}
