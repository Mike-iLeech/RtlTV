package io.github.mikeileech.sdr4noob.ui

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import io.github.mikeileech.sdr4noob.i18n.I18n
import io.github.mikeileech.sdr4noob.i18n.tr

/**
 * Справка — то же руководство для начинающих, что в версии для Windows (rtltv/guide, копируется в APK
 * при сборке), включая интерактивную схему «Путь радиосигнала». Страницы руководства — простой HTML без
 * стилей (в настольной версии их задаёт окно справки), поэтому стиль тёмной темы добавляется здесь.
 */
class GuideActivity : Activity() {
    companion object {
        const val EXTRA_PAGE = "page"
        private const val ROOT = "file:///android_asset/"

        private val CSS = """
            body { background:#15181d; color:#d8dee9; font-family:sans-serif; line-height:1.45;
                   margin:12px; font-size:16px; word-wrap:break-word; }
            a { color:#4fb3ff; text-decoration:none; }
            h1 { color:#4fb3ff; font-size:1.5em; } h2 { color:#7fd1ff; font-size:1.25em; } h3 { font-size:1.1em; }
            img, svg { max-width:100%; height:auto; }
            table { border-collapse:collapse; max-width:100%; display:block; overflow-x:auto; }
            table.t th, table.t td { border:1px solid #2f3542; padding:4px; }
            table.t th { background:#252a33; }
            table.note, table.warn { background:#252a33; border:1px solid #4fb3ff; margin:8px 0; display:table; }
            table.warn { border-color:#d9822b; }
            table.btn { background:#4fb3ff; } table.btn a { color:#15181d; }
            code, .mono { font-family:monospace; color:#7fffd4; }
            .dim, .crumbs { color:#8a93a3; }
            .k { font-family:monospace; background:#252a33; }
        """.trimIndent().replace("\n", " ")
    }

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = tr("Справка")
        web = WebView(this)
        web.setBackgroundColor(Ui.BG)
        web.settings.javaScriptEnabled = true   // интерактивная схема (pipeline/) — JavaScript и Canvas
        web.settings.allowFileAccess = true
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                if (u.scheme == "file" && u.toString().startsWith(ROOT)) {
                    // схема в тёмной теме, как окно справки настольной версии
                    if (u.path.orEmpty().endsWith("/pipeline/index.html") && u.getQueryParameter("theme") == null) {
                        view.loadUrl(u.buildUpon().appendQueryParameter("theme", "dark").build().toString())
                        return true
                    }
                    return false
                }
                if (u.scheme == "http" || u.scheme == "https" || u.scheme == "mailto") {
                    try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: ActivityNotFoundException) {}
                }
                return true
            }

            override fun onPageFinished(view: WebView, url: String) {
                // у страниц справки нет своих стилей; у интерактивной схемы — свои, её не трогаем
                if (!url.contains("/pipeline/")) {
                    view.evaluateJavascript("(function(){var s=document.createElement('style');" +
                        "s.textContent=${org.json.JSONObject.quote(CSS)};document.head.appendChild(s);" +
                        "var m=document.createElement('meta');m.name='viewport';" +
                        "m.content='width=device-width, initial-scale=1';document.head.appendChild(m);})()", null)
                }
            }
        }
        setContentView(web)
        // при английском интерфейсе — английская страница (guide/en/…, android/en/…), если она есть в APK
        val page = I18n.localizedAsset(this, intent.getStringExtra(EXTRA_PAGE) ?: "guide/index.html")
        if (savedInstanceState != null) web.restoreState(savedInstanceState) else web.loadUrl(ROOT + page)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    @Deprecated("Activity.onBackPressed")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }
}
