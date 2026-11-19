plugins {
    id("com.android.application")
}

android {
    namespace = "io.github.eagle10021.realroutes"
    compileSdk = 37

    defaultConfig {
        applicationId = "io.github.eagle10021.realroutes"
        minSdk = 31
        targetSdk = 37
        versionCode = 7
        versionName = "0.6.0"
        // phones only (MapLibre's native code is large; x86 emulators can add "x86_64")
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.car.app:app:1.7.0")
    implementation("org.maplibre.gl:android-sdk:13.7.1")
    implementation("androidx.core:core-ktx:1.18.0")
}
