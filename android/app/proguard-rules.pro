# The practice page calls these through addJavascriptInterface.
-keepclassmembers class dev.russell.studyreader.PracticeBridge {
    @android.webkit.JavascriptInterface <methods>;
}
-keepattributes JavascriptInterface
