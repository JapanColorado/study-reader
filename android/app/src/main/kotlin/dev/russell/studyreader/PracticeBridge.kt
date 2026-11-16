package dev.russell.studyreader

import android.webkit.JavascriptInterface
import dev.russell.studyreader.core.PracticeController

/**
 * What practice.html calls, as window.PracticeApp: the same methods the desktop's web channel
 * offers. Calls arrive on WebView's bridge thread and run on the store's worker, in order.
 */
class PracticeBridge(private val store: Store, private val controller: () -> PracticeController) {
    private fun run(block: PracticeController.() -> Unit) = store.worker.execute { controller().block() }

    @JavascriptInterface fun start(limit: String) = run { start(limit) }
    @JavascriptInterface fun stop() = run { stop() }
    @JavascriptInterface fun check(questionId: Double, response: String) = run { check(questionId.toLong(), response) }
    @JavascriptInterface fun rate(questionId: Double, rating: Double) = run { rate(questionId.toLong(), rating.toInt()) }
    @JavascriptInterface fun act(questionId: Double, action: String) = run { act(questionId.toLong(), action) }
    @JavascriptInterface fun choose(json: String) = run { choose(json) }
    @JavascriptInterface fun refresh() = run { refresh() }
    @JavascriptInterface fun open(what: String) = run { open(what) }
}
