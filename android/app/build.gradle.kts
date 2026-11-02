import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// The desktop app's review page and what it loads (card rendering, KaTeX, MathLive, confetti),
// copied in at build time so the phone draws cards exactly as the desktop does.
val desktopAssets = rootProject.file("../src/study_reader/assets")
val webAssets = layout.buildDirectory.dir("generated/webassets")
val copyWebAssets by tasks.registering(Sync::class) {
    from(desktopAssets) {
        include("review.html", "app.css", "progress.js", "quiz.js", "quiz.css", "markdown.js", "markdown-it.min.js",
            "mathfield.js", "confetti.js")
        include("katex/**", "mathlive/**")
        exclude("mathlive/mathlive-static.css")
    }
    into(webAssets.map { it.dir("web") })
}

android {
    namespace = "dev.russell.studyreader"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.russell.studyreader"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    // Release signing: gitignored keystore.properties (storeFile, storePassword, keyAlias, keyPassword).
    // Absent => the release APK is debug-signed.
    val keystoreProps = rootProject.file("keystore.properties")
        .takeIf { it.exists() }
        ?.let { f -> Properties().apply { f.inputStream().use { load(it) } } }
    signingConfigs {
        if (keystoreProps != null) {
            create("release") {
                storeFile = file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    sourceSets["main"].assets.srcDir(webAssets)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
}

tasks.named("preBuild") { dependsOn(copyWebAssets) }

dependencies {
    implementation(project(":core"))
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons.extended)
    implementation(libs.core.ktx)
    implementation(libs.lifecycle.runtime.ktx)
    implementation(libs.activity.compose)
    implementation(libs.webkit)
    implementation(libs.documentfile)
}
