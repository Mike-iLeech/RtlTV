package io.github.mikeileech.sdr4noob.ui

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import io.github.mikeileech.sdr4noob.i18n.dec
import io.github.mikeileech.sdr4noob.i18n.tr

/** Мелкие помощники разметки: экраны собираются кодом, без XML. */
object Ui {
    // тема «Тёмная» настольной версии (rtltv/themes.py)
    const val BG = 0xFF15181D.toInt()
    const val CARD = 0xFF1D2128.toInt()
    const val ACCENT = 0xFF4FB3FF.toInt()
    const val DIM = 0xFF8A93A3.toInt()

    fun dp(ctx: Context, v: Number) = (v.toFloat() * ctx.resources.displayMetrics.density).toInt()

    fun vbox(ctx: Context, pad: Int = 16) = LinearLayout(ctx).apply {
        orientation = LinearLayout.VERTICAL
        val p = dp(ctx, pad); setPadding(p, p, p, p)
    }

    fun hbox(ctx: Context) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
    }

    fun text(ctx: Context, s: CharSequence, size: Float = 15f, color: Int = Color.WHITE, bold: Boolean = false) =
        TextView(ctx).apply {
            text = s; textSize = size; setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    fun button(ctx: Context, s: String, onClick: (View) -> Unit) = Button(ctx).apply {
        text = s; isAllCaps = false; setOnClickListener(onClick)
    }

    fun card(ctx: Context): LinearLayout = vbox(ctx, 14).apply {
        background = GradientDrawable().apply { setColor(CARD); cornerRadius = dp(ctx, 12).toFloat() }
    }

    fun lp(w: Int = ViewGroup.LayoutParams.MATCH_PARENT, h: Int = ViewGroup.LayoutParams.WRAP_CONTENT,
           weight: Float = 0f, top: Int = 0) =
        LinearLayout.LayoutParams(w, h, weight).apply { topMargin = top }

    /** «101,000 МГц», «446,00625 МГц» (по-английски «101.000 MHz»): не меньше трёх знаков после запятой, но без потери герц. */
    fun mhz(hz: Long): String {
        var s = String.format(java.util.Locale.ROOT, "%.6f", hz / 1e6)
        while (s.endsWith("0") && s.length - s.indexOf('.') > 4) s = s.dropLast(1)
        return dec(s) + " " + tr("МГц")
    }
}
