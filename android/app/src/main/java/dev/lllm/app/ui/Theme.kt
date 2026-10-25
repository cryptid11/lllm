package dev.lllm.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily

/** Terminal palette, Claude Code style. */
object Palette {
    val Bg = Color(0xFF141413)
    val Surface = Color(0xFF1F1E1D)
    val Raised = Color(0xFF2A2927)
    val Border = Color(0xFF3D3A36)
    val Text = Color(0xFFE8E6E3)
    val Dim = Color(0xFF8C8A85)
    val Accent = Color(0xFFD97757)
    val Error = Color(0xFFE5534B)
    val Ok = Color(0xFF6BBF59)
    val Code = Color(0xFF9CC2E8)
}

private val colors = darkColorScheme(
    primary = Palette.Accent,
    onPrimary = Palette.Bg,
    secondary = Palette.Accent,
    onSecondary = Palette.Bg,
    background = Palette.Bg,
    onBackground = Palette.Text,
    surface = Palette.Bg,
    onSurface = Palette.Text,
    surfaceVariant = Palette.Surface,
    onSurfaceVariant = Palette.Dim,
    surfaceContainer = Palette.Surface,
    surfaceContainerHigh = Palette.Raised,
    surfaceContainerHighest = Palette.Raised,
    primaryContainer = Palette.Raised,
    onPrimaryContainer = Palette.Text,
    outline = Palette.Border,
    outlineVariant = Palette.Border,
    error = Palette.Error,
)

private fun TextStyle.mono() = copy(fontFamily = FontFamily.Monospace)

private val typography = Typography().run {
    Typography(
        displayLarge = displayLarge.mono(), displayMedium = displayMedium.mono(), displaySmall = displaySmall.mono(),
        headlineLarge = headlineLarge.mono(), headlineMedium = headlineMedium.mono(), headlineSmall = headlineSmall.mono(),
        titleLarge = titleLarge.mono(), titleMedium = titleMedium.mono(), titleSmall = titleSmall.mono(),
        bodyLarge = bodyLarge.mono(), bodyMedium = bodyMedium.mono(), bodySmall = bodySmall.mono(),
        labelLarge = labelLarge.mono(), labelMedium = labelMedium.mono(), labelSmall = labelSmall.mono(),
    )
}

@Composable
fun LllmTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = colors, typography = typography, content = content)
}
