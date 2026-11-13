package dev.russell.studyreader

import android.webkit.JavascriptInterface
import dev.russell.studyreader.core.ReviewController

/**
 * What review.html calls, as window.ReviewApp: the same methods the desktop's web channel
 * offers. Calls arrive on WebView's bridge thread and run on the store's worker, in order.
 */
class ReviewBridge(private val store: Store, private val controller: () -> ReviewController) {
    private fun run(block: ReviewController.() -> Unit) = store.worker.execute { controller().block() }

    @JavascriptInterface fun start(limit: String) = run { start(limit) }
    @JavascriptInterface fun stop() = run { stop() }
    @JavascriptInterface fun check(cardId: Double, response: String) = run { check(cardId.toLong(), response) }
    @JavascriptInterface fun rate(cardId: Double, rating: Double) = run { rate(cardId.toLong(), rating.toInt()) }
    @JavascriptInterface fun act(cardId: Double, action: String) = run { act(cardId.toLong(), action) }
    @JavascriptInterface fun choose(json: String) = run { choose(json) }
    @JavascriptInterface fun refresh() = run { refresh() }
    @JavascriptInterface fun open(what: String) = run { open(what) }
}
