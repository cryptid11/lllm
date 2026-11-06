package dev.lllm.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import dev.lllm.app.relay.PendingApproval
import dev.lllm.app.ui.Conn
import dev.lllm.app.ui.UiState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.launch

/**
 * Foreground service that keeps the process (and so the SSH connection held by
 * [dev.lllm.app.ui.AppController]) alive while the app is in the background.
 * Its ongoing notification shows the connection state and offers Disconnect.
 */
class RelayService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var wakeLock: PowerManager.WakeLock? = null

    /** [alert]: a tool call waits for approval while the app is not on screen. */
    private data class Status(
        val title: String,
        val text: String,
        val generating: Boolean,
        val alert: PendingApproval? = null,
    )

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        running = true
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Connection", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while lllm stays connected to your relay"
                setShowBadge(false)
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(APPROVAL_CHANNEL, "Approvals", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "A tool call is waiting for you to allow or deny it"
            }
        )
        val controller = (application as LllmApp).controller
        goForeground(build(status(controller.state.value)))
        scope.launch {
            controller.state.map(::status).distinctUntilChanged().collect { st ->
                nm.notify(NOTIFICATION_ID, build(st))
                val alert = st.alert
                if (alert != null) nm.notify(APPROVAL_ID, buildApproval(alert)) else nm.cancel(APPROVAL_ID)
                holdAwake(st.generating)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val controller = (application as LllmApp).controller
        if (intent?.action == ACTION_DISCONNECT) {
            controller.disconnect() // stops this service
            return START_NOT_STICKY
        }
        if (intent?.action == ACTION_APPROVE) {
            val pending = PendingApproval(
                intent.getStringExtra(EXTRA_SESSION).orEmpty(), intent.getStringExtra(EXTRA_CALL).orEmpty(),
                intent.getStringExtra(EXTRA_NAME).orEmpty(), "{}",
            )
            controller.approve(pending, intent.getStringExtra(EXTRA_DECISION) ?: "deny")
        }
        // If the system kills the process, recreate the service: the controller reconnects on start.
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        scope.cancel()
        holdAwake(false)
        super.onDestroy()
    }

    private fun goForeground(n: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    private fun status(st: UiState): Status {
        val who = st.profile?.label ?: "relay"
        return when (val c = st.conn) {
            is Conn.Up -> Status(
                "Connected to $who",
                when {
                    st.pendingApproval != null -> "Waiting for approval: ${st.pendingApproval.label}"
                    st.generating -> "Generating a reply…"
                    else -> "Tap to open"
                },
                st.generating,
                alert = st.pendingApproval?.takeIf { !st.appVisible },
            )
            Conn.Connecting -> Status("Connecting to $who…", "", false)
            is Conn.Down -> Status(if (c.retrying) "Reconnecting to $who…" else "Disconnected", c.reason, false)
            Conn.Idle -> Status("lllm", "Not connected", false)
        }
    }

    private fun build(st: Status): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val disconnect = PendingIntent.getService(
            this, 1,
            Intent(this, RelayService::class.java).setAction(ACTION_DISCONNECT),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_relay)
            .setContentTitle(st.title)
            .setContentText(st.text)
            .setContentIntent(open)
            .addAction(0, "Disconnect", disconnect)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun buildApproval(p: PendingApproval): Notification {
        fun action(code: Int, decision: String) = PendingIntent.getService(
            this, code,
            Intent(this, RelayService::class.java).setAction(ACTION_APPROVE)
                .putExtra(EXTRA_SESSION, p.session).putExtra(EXTRA_CALL, p.callId)
                .putExtra(EXTRA_NAME, p.name).putExtra(EXTRA_DECISION, decision),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val open = PendingIntent.getActivity(
            this, 2,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, APPROVAL_CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_relay)
            .setContentTitle("Allow ${p.label}?")
            .setContentText("The agent is waiting for you")
            .setContentIntent(open)
            .addAction(0, "Allow", action(10, "allow"))
            .addAction(0, "Deny", action(11, "deny"))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .build()
    }

    /**
     * Keeps the CPU awake only while a reply streams, so it keeps arriving with the screen off.
     * An idle connection does not need it: the server keeps the session, and data wakes the device.
     */
    private fun holdAwake(on: Boolean) {
        val held = wakeLock?.isHeld == true
        if (on && !held) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "lllm:generating")
                .apply { acquire(30 * 60 * 1000L) } // safety cap
        } else if (!on && held) {
            wakeLock?.release()
            wakeLock = null
        }
    }

    companion object {
        private const val CHANNEL = "connection"
        private const val NOTIFICATION_ID = 1
        private const val APPROVAL_CHANNEL = "approvals"
        private const val APPROVAL_ID = 2
        private const val ACTION_DISCONNECT = "dev.lllm.app.DISCONNECT"
        private const val ACTION_APPROVE = "dev.lllm.app.APPROVE"
        private const val EXTRA_SESSION = "session"
        private const val EXTRA_CALL = "call_id"
        private const val EXTRA_NAME = "name"
        private const val EXTRA_DECISION = "decision"

        @Volatile
        private var running = false

        fun start(context: Context) {
            if (running) return
            // Only allowed while the app is in the foreground (Android 12+); that is when users connect.
            runCatching { context.startForegroundService(Intent(context, RelayService::class.java)) }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, RelayService::class.java))
        }
    }
}
