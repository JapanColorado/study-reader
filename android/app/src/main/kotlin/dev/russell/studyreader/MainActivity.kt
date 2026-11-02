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
    "surface" to "#fbfaf8", "raised" to "#ffffff", "fg" to "#1f1e1c", "muted" to "#6b6862", "faint" to "#a19d95",
    "border" to "#dedad2", "hover" to "#ebe8e1", "accent" to "#b8562f", "accent_fg" to "#ffffff", "accent_soft" to "#f4e2d8",
    "user" to "#efece6", "code" to "#f1eee8", "error" to "#c0392b", "scroll" to "#cfcac1", "success" to "#3a8f5c", "warning" to "#b8860b",
)
private val DARK = mapOf(
    "surface" to "#1f1e1d", "raised" to "#292725", "fg" to "#ece9e2", "muted" to "#a09c94", "faint" to "#6c6862",
    "border" to "#363330", "hover" to "#2e2c2a", "accent" to "#e08a63", "accent_fg" to "#1c1410", "accent_soft" to "#3d2b22",
    "user" to "#2d2b29", "code" to "#2a2826", "error" to "#ec7063", "scroll" to "#45413d", "success" to "#6cc28e", "warning" to "#e2bd52",
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

    private fun dark() = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        store = Store.get(this)
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
                    val colors = buildJsonObject { (if (dark()) DARK else LIGHT).forEach { (k, v) -> put(k, v) } }
                    view.evaluateJavascript("setTheme($colors, ${dark()})", null)
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
