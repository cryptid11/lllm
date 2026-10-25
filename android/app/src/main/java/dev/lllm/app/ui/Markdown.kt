package dev.lllm.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// A deliberately small Markdown subset: fenced code, headings, bullets, **bold**, `code`.
// It tolerates half-written input, since it re-renders while a reply streams in.

private sealed interface Block {
    data class Prose(val text: String) : Block
    data class Code(val code: String) : Block
}

private fun parseBlocks(text: String): List<Block> {
    val blocks = mutableListOf<Block>()
    val prose = StringBuilder()
    val code = StringBuilder()
    var inCode = false
    fun flushProse() {
        if (prose.isNotBlank()) blocks += Block.Prose(prose.toString().trim('\n'))
        prose.clear()
    }
    for (line in text.lines()) {
        if (line.trimStart().startsWith("```")) {
            if (inCode) {
                blocks += Block.Code(code.toString().trimEnd('\n'))
                code.clear()
            } else {
                flushProse()
            }
            inCode = !inCode
        } else if (inCode) {
            code.append(line).append('\n')
        } else {
            prose.append(line).append('\n')
        }
    }
    if (inCode) blocks += Block.Code(code.toString().trimEnd('\n')) else flushProse()
    return blocks
}

private val inlineToken = Regex("""\*\*([^*]+)\*\*|`([^`]+)`""")

private fun inline(text: String): AnnotatedString = buildAnnotatedString {
    text.lines().forEachIndexed { i, raw ->
        if (i > 0) append('\n')
        var line = raw
        val heading = line.startsWith("#")
        if (heading) line = line.trimStart('#').trimStart()
        val bullet = Regex("""^(\s*)[-*] (.*)""").find(line)
        if (bullet != null) line = bullet.groupValues[1] + "• " + bullet.groupValues[2]
        if (heading) pushStyle(SpanStyle(fontWeight = FontWeight.Bold, color = Palette.Text))
        var pos = 0
        for (m in inlineToken.findAll(line)) {
            append(line.substring(pos, m.range.first))
            val (bold, code) = m.destructured
            if (bold.isNotEmpty()) {
                withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(bold) }
            } else {
                withStyle(SpanStyle(color = Palette.Code, background = Palette.Surface)) { append(code) }
            }
            pos = m.range.last + 1
        }
        append(line.substring(pos))
        if (heading) pop()
    }
}

@Composable
fun Markdown(text: String, color: Color, modifier: Modifier = Modifier) {
    val blocks = remember(text) { parseBlocks(text) }
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        for (b in blocks) when (b) {
            is Block.Prose -> Text(inline(b.text), color = color, fontSize = 14.sp, lineHeight = 20.sp)
            is Block.Code -> Box(
                Modifier
                    .fillMaxWidth()
                    .background(Palette.Surface, RoundedCornerShape(6.dp))
                    .horizontalScroll(rememberScrollState())
                    .padding(10.dp)
            ) {
                Text(b.code, color = Palette.Code, fontSize = 13.sp, lineHeight = 18.sp, softWrap = false)
            }
        }
    }
}
