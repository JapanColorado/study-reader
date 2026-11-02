// Platform-free logic the app runs on: FSRS, numeric grading, the review queue, rewards and the
// sync format. It talks to SQLite through the small Sql interface, so tests run on the JVM over
// sqlite-jdbc.
plugins {
    alias(libs.plugins.kotlin.jvm)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

dependencies {
    api(libs.serialization.json)
    testImplementation(libs.junit)
    testImplementation(libs.sqlite.jdbc)
}

tasks.test {
    // The parity fixtures (scripts/queue_fixtures.py) are made in UTC.
    environment("TZ", "UTC")
    systemProperty("user.timezone", "UTC")
}
