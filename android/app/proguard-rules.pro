# The review page calls these through addJavascriptInterface.
-keepclassmembers class dev.russell.studyreader.ReviewBridge {
    @android.webkit.JavascriptInterface <methods>;
}
-keepattributes JavascriptInterface
