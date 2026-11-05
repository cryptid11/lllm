package dev.lllm.app

import android.os.Bundle
import android.graphics.Color
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import dev.lllm.app.ui.AppViewModel
import dev.lllm.app.ui.ChatScreen
import dev.lllm.app.ui.LllmTheme
import dev.lllm.app.ui.Palette
import dev.lllm.app.ui.Screen
import dev.lllm.app.ui.SessionsScreen
import dev.lllm.app.ui.SetupScreen
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {
    private val vm: AppViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        setContent { LllmTheme { App(vm) } }
    }

    override fun onResume() {
        super.onResume()
        vm.onResume()
    }
}

@Composable
private fun App(vm: AppViewModel) {
    val state by vm.state.collectAsStateWithLifecycle()
    BackHandler(enabled = state.screen == Screen.Chat || (state.screen == Screen.Setup && state.profile?.isComplete == true)) {
        vm.back()
    }
    Box(Modifier.fillMaxSize().background(Palette.Bg)) {
        when (state.screen) {
            Screen.Setup -> SetupScreen(state, vm)
            Screen.Sessions -> SessionsScreen(state, vm)
            Screen.Chat -> ChatScreen(state, vm)
        }
        state.notice?.let { msg ->
            LaunchedEffect(msg) {
                delay(5000)
                vm.dismissNotice()
            }
            Text(
                msg,
                Modifier
                    .align(Alignment.TopCenter)
                    .statusBarsPadding()
                    .padding(12.dp)
                    .fillMaxWidth()
                    .background(Palette.Raised, RoundedCornerShape(8.dp))
                    .clickable { vm.dismissNotice() }
                    .padding(12.dp),
                color = Palette.Text,
                fontSize = 13.sp,
            )
        }
    }
}
