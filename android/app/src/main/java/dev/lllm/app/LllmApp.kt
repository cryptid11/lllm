package dev.lllm.app

import android.app.Application
import dev.lllm.app.ui.AppController
import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.security.Security

class LllmApp : Application() {
    /** Process-wide app state and connection, shared by the activity and [RelayService]. */
    val controller: AppController by lazy { AppController(this) }

    override fun onCreate() {
        super.onCreate()
        // Android ships a stripped-down "BC" provider; sshj needs the full one.
        // Appended (not inserted first) so platform crypto such as the Keystore keeps its defaults.
        Security.removeProvider("BC")
        Security.addProvider(BouncyCastleProvider())
    }
}
