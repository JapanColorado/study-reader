package dev.russell.studyreader

import android.Manifest
import android.annotation.SuppressLint
import android.app.TimePickerDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewAssetLoader
import androidx.webkit.WebViewClientCompat
import dev.russell.studyreader.core.ReviewController
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/** The desktop's light and dark colors (theme.py), for the review page and the app bar. */
private val LIGHT = mapOf(
    "window" to "#f4f1ea", "surface" to "#faf8f4", "raised" to "#ffffff", "fg" to "#1d1b18", "muted" to "#69645c",
    "faint" to "#a39e95", "border" to "#e2ddd3", "hover" to "#ece8df", "accent" to "#b9532b", "accent_hover" to "#a34723",
    "accent_fg" to "#ffffff", "accent_soft" to "#f6e4da", "user" to "#efebe4", "code" to "#f2efe9", "error" to "#c0392b",
    "scroll" to "#d2ccc2", "success" to "#2f8a57", "warning" to "#a87a0a", "new" to "#2f6db5",
    "success_soft" to "#e1f0e6", "new_soft" to "#e3ecf8", "shadow" to "rgba(60,45,30,0.10)",
)
private val DARK = mapOf(
    "window" to "#171615", "surface" to "#1c1b19", "raised" to "#22201e", "fg" to "#efebe4", "muted" to "#a8a298",
    "faint" to "#6f6a63", "border" to "#312e2a", "hover" to "#2b2926", "accent" to "#e5895f", "accent_hover" to "#efa07c",
    "accent_fg" to "#1d130e", "accent_soft" to "#3a2a21", "user" to "#2a2826", "code" to "#272522", "error" to "#ef7a6c",
    "scroll" to "#433f3a", "success" to "#74c69d", "warning" to "#e3be5c", "new" to "#7fb0e8",
    "success_soft" to "#1f3128", "new_soft" to "#1f2a38", "shadow" to "rgba(0,0,0,0.35)",
)
private fun color(hex: String) = Color(android.graphics.Color.parseColor(hex))

class MainActivity : ComponentActivity() {
    private lateinit var store: Store
    private var webView: WebView? = null
    private var pageReady = false
    private val queued = mutableListOf<String>()
    @Volatile private var controller: ReviewController? = null
    private var onCard = mutableStateOf(false)
    private var syncing = mutableStateOf(false)
    private var screen = mutableStateOf("review")

    private fun systemDark() = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
    private val darkState = mutableStateOf(false)
    private fun dark() = darkState.value

    /** The page's colors follow the system's light or dark mode (the activity isn't recreated for it). */
    private fun applyTheme() {
        val view = webView ?: return
        view.setBackgroundColor(android.graphics.Color.parseColor((if (dark()) DARK else LIGHT)["surface"]))
        if (!pageReady) return
        val colors = buildJsonObject { (if (dark()) DARK else LIGHT).forEach { (k, v) -> put(k, v) } }
        view.evaluateJavascript("setTheme($colors, ${dark()})", null)
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        if (systemDark() != darkState.value) {
            darkState.value = systemDark()
            enableEdgeToEdge()  // status bar icons for the new mode
            applyTheme()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        store = Store.get(this)
        darkState.value = systemDark()
        store.worker.execute {
            controller = ReviewController(store.sql, js = ::runJs, onChanged = { updateStatus() })
        }
        setContent {
            val palette = if (dark()) DARK else LIGHT
            val scheme = if (dark()) darkColorScheme(primary = color(palette["accent"]!!), background = color(palette["surface"]!!),
                surface = color(palette["surface"]!!), onSurface = color(palette["fg"]!!))
            else lightColorScheme(primary = color(palette["accent"]!!), background = color(palette["surface"]!!),
                surface = color(palette["surface"]!!), onSurface = color(palette["fg"]!!))
            MaterialTheme(colorScheme = scheme) {
                if (screen.value == "settings") SettingsScreen(store, onBack = { screen.value = "review"; syncNow() }, onSync = { syncNow() })
                else ReviewScreen()
            }
        }
    }

    override fun onResume() {
        super.onResume()
        syncNow()
    }

    override fun onPause() {
        super.onPause()
        store.worker.execute { try { store.flush() } catch (_: Exception) {} }
    }

    private fun runJs(js: String) {
        android.util.Log.d("StudyReview", "js: ${js.take(120)}")
        if (js.startsWith("showCard(")) onCard.value = true
        else if (js.startsWith("showOverview(")) onCard.value = false
        runOnUiThread {
            val view = webView
            if (view != null && pageReady) view.evaluateJavascript(js, null) else queued += js
        }
    }

    private fun updateStatus() {
        controller?.status = store.statusLine()
    }

    fun syncNow() {
        if (syncing.value) return
        syncing.value = true
        store.worker.execute {
            val changed = store.sync()
            val c = controller
            if (c != null) {
                c.status = store.statusLine()
                c.emptyMessage = if (store.folder == null) "No cards yet. Choose the folder the computer shares in Settings."
                                 else "No cards yet. Turn on Phone sync in Study Reader on the computer (Settings › Phone sync…) and pick the same folder."
                if (changed || c.cardId == null) c.refresh()
            }
            syncing.value = false
        }
    }

    @OptIn(ExperimentalMaterial3Api::class)
    @Composable
    private fun ReviewScreen() {
        BackHandler(enabled = onCard.value) { store.worker.execute { controller?.stop() } }
        Scaffold(
            topBar = {
                TopAppBar(
                    title = { Text("Review") },
                    colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
                    actions = {
                        if (syncing.value) CircularProgressIndicator(Modifier.size(22.dp).padding(end = 4.dp), strokeWidth = 2.dp)
                        else IconButton(onClick = { syncNow() }) { Icon(Icons.Filled.Sync, "Sync") }
                        IconButton(onClick = { screen.value = "settings" }) { Icon(Icons.Filled.Settings, "Settings") }
                    },
                )
            },
        ) { padding ->
            AndroidView(
                modifier = Modifier.fillMaxSize().padding(padding).imePadding(),
                factory = { ctx -> webView ?: createWebView(ctx).also { webView = it } },
            )
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun createWebView(ctx: android.content.Context): WebView {
        val loader = WebViewAssetLoader.Builder()
            .addPathHandler("/assets/", WebViewAssetLoader.AssetsPathHandler(ctx))
            .addPathHandler("/media/", WebViewAssetLoader.InternalStoragePathHandler(ctx, store.mediaDir))
            .build()
        WebView.setWebContentsDebuggingEnabled(0 != (ctx.applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE))
        return WebView(ctx).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            setBackgroundColor(android.graphics.Color.parseColor((if (dark()) DARK else LIGHT)["surface"]))
            addJavascriptInterface(ReviewBridge(store) { controller!! }, "ReviewApp")
            webChromeClient = object : android.webkit.WebChromeClient() {
                override fun onConsoleMessage(m: android.webkit.ConsoleMessage): Boolean {
                    android.util.Log.i("StudyReview", "page: ${m.message()} (${m.sourceId()}:${m.lineNumber()})")
                    return true
                }
            }
            webViewClient = object : WebViewClientCompat() {
                override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
                    loader.shouldInterceptRequest(request.url)

                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    // Links in card text open in the browser.
                    if (request.url.host != "appassets.androidplatform.net") {
                        startActivity(Intent(Intent.ACTION_VIEW, request.url))
                    }
                    return true
                }

                override fun onPageFinished(view: WebView, url: String?) {
                    android.util.Log.d("StudyReview", "page loaded: $url")
                    pageReady = true
                    applyTheme()
                    view.evaluateJavascript("setMedia(${JsonPrimitive("https://appassets.androidplatform.net/media/")})", null)
                    queued.forEach { view.evaluateJavascript(it, null) }
                    queued.clear()
                    store.worker.execute { updateStatus(); controller?.refresh() }
                }
            }
            loadUrl("https://appassets.androidplatform.net/assets/web/review.html")
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SettingsScreen(store: Store, onBack: () -> Unit, onSync: () -> Unit) {
    val context = LocalContext.current
    var folder by remember { mutableStateOf(store.folder) }
    var status by remember { mutableStateOf("") }
    var reminder by remember { mutableStateOf(Reminders.enabled(context)) }
    var minute by remember { mutableStateOf(Reminders.minuteOfDay(context)) }
    BackHandler(onBack = onBack)
    LaunchedEffect(folder) { store.worker.execute { status = store.statusLine() } }

    val pickFolder = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri: Uri? ->
        if (uri != null) {
            context.contentResolver.takePersistableUriPermission(
                uri, Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
            )
            store.folder = uri
            folder = uri
            onSync()
        }
    }
    val askNotifications = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        reminder = granted
        Reminders.configure(context, granted, minute)
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("Settings") },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } },
            colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
        )
    }) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(horizontal = 20.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text("Sync", style = MaterialTheme.typography.titleMedium)
            Text(
                "Cards come from Study Reader on your computer through a folder that Syncthing keeps in step on both " +
                    "devices. On the computer, choose Settings › Phone sync… and pick the folder Syncthing shares with this " +
                    "phone. Here, choose the same folder as Syncthing keeps it on the phone. Reviews you do here go back " +
                    "the same way, and the computer applies them.",
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(folder?.let { Uri.decode(it.lastPathSegment ?: it.toString()) } ?: "No folder chosen",
                style = MaterialTheme.typography.bodyLarge)
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(onClick = { pickFolder.launch(folder) }) { Text(if (folder == null) "Choose folder" else "Change folder") }
                OutlinedButton(onClick = onSync, enabled = folder != null) { Text("Sync now") }
            }
            if (status.isNotEmpty()) Text(status, style = MaterialTheme.typography.bodySmall)
            HorizontalDivider()
            Text("Reminder", style = MaterialTheme.typography.titleMedium)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Daily reminder", style = MaterialTheme.typography.bodyLarge)
                    Text("A notification when cards are waiting", style = MaterialTheme.typography.bodySmall)
                }
                Switch(checked = reminder, onCheckedChange = { on ->
                    if (on && Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
                        askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
                    } else {
                        reminder = on
                        Reminders.configure(context, on, minute)
                    }
                })
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Time", Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
                OutlinedButton(onClick = {
                    TimePickerDialog(context, { _, h, m ->
                        minute = h * 60 + m
                        Reminders.configure(context, reminder, minute)
                    }, minute / 60, minute % 60, android.text.format.DateFormat.is24HourFormat(context)).show()
                }) { Text("%d:%02d".format(minute / 60, minute % 60)) }
            }
            HorizontalDivider()
            Text("Review settings (new cards a day, retention, daily goal, rewards) follow the computer's.",
                style = MaterialTheme.typography.bodySmall)
            Spacer(Modifier.width(1.dp))
        }
    }
}
