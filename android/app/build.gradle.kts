import java.net.URI
import java.security.MessageDigest

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Версия одна на все сборки: rtltv/__init__.py (__version__ = "X.Y.Z") — как у Windows-архива и тега vX.Y.Z.
val repoRoot = rootDir.parentFile
val appVersion: String = Regex("""__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"""")
    .find(repoRoot.resolve("rtltv/__init__.py").readText())?.groupValues?.get(1)
    ?: error("не нашёл __version__ в rtltv/__init__.py")
val appVersionCode: Int = appVersion.split(".").map { it.toInt() }.let { (a, b, c) -> a * 10000 + b * 100 + c }

// ---------------------------------------------------------------------------------------------
// Исходники драйвера: libusb и librtlsdr (сборка rtl-sdr-blog — с поддержкой RTL-SDR Blog V4) и mbelib,
// закреплённые по SHA-256. Скачиваются один раз в android/deps и собираются CMake под каждую ABI.
// ---------------------------------------------------------------------------------------------
data class NativeDep(val name: String, val url: String, val sha256: String, val dir: String)

val nativeDeps = listOf(
    NativeDep("libusb", "https://github.com/libusb/libusb/releases/download/v1.0.30/libusb-1.0.30.tar.bz2",
        "fea36f34f9156400209595e300840767ab1a385ede1dc7ee893015aea9c6dbaf", "libusb-1.0.30"),
    NativeDep("rtl-sdr-blog", "https://github.com/rtlsdrblog/rtl-sdr-blog/archive/refs/tags/V1.4.0.tar.gz",
        "89520433e63b1a1371b45d8deea198cad9dbd1f0d55cc3a706aafb7b187bd325", "rtl-sdr-blog-1.4.0"),
    // вокодер AMBE+2 для голоса DMR (тот же mbelib, что rtltv/driver/mbelib.dll на ПК)
    NativeDep("mbelib", "https://github.com/szechyjs/mbelib/archive/9a04ed5c78176a9965f3d43f7aa1b1f5330e771f.tar.gz",
        "c7d6ebbbf3ca44bc10ee0755dede7f74fd65c31d1568d4174f29a68ae2f92278", "mbelib-9a04ed5c78176a9965f3d43f7aa1b1f5330e771f"),
    // драйверы HackRF (открывается через usb_fd_shim) и Airspy (airspy_open_fd есть только на master)
    NativeDep("hackrf", "https://github.com/greatscottgadgets/hackrf/archive/refs/tags/v2024.02.1.tar.gz",
        "16e4707b80d0d16db3a9e2149745d7a1bc253dea88c9c542d3ae062df5cbd9e3", "hackrf-2024.02.1"),
    NativeDep("airspyone_host", "https://github.com/airspy/airspyone_host/archive/fc61ab6be57ed61f0e2bdd9c6dfae74cacef57d0.tar.gz",
        "5318d8c9dfb1e84b424b6f33d027dc8501ee18db83e5e7519207ecda130d5afd", "airspyone_host-fc61ab6be57ed61f0e2bdd9c6dfae74cacef57d0"),
)
val depsDir: File = rootDir.resolve("deps")

val fetchNativeDeps by tasks.registering {
    description = "Скачать и проверить исходники libusb и librtlsdr"
    outputs.dir(depsDir)
    doLast {
        depsDir.mkdirs()
        for (d in nativeDeps) {
            val marker = depsDir.resolve("${d.dir}/.sha256")
            if (marker.isFile && marker.readText().trim() == d.sha256) continue
            val archive = depsDir.resolve(d.url.substringAfterLast('/'))
            if (!archive.isFile || sha256(archive) != d.sha256) {
                logger.lifecycle("Скачиваю ${d.name}: ${d.url}")
                val tmp = File(archive.path + ".part")
                URI(d.url).toURL().openStream().use { inp -> tmp.outputStream().use { out -> inp.copyTo(out) } }
                val got = sha256(tmp)
                if (got != d.sha256) {
                    tmp.delete()
                    throw GradleException("${d.name}: SHA-256 $got, ожидалось ${d.sha256}")
                }
                tmp.renameTo(archive)
            }
            delete(depsDir.resolve(d.dir))
            copy {
                from(tarTree(if (archive.name.endsWith(".bz2")) resources.bzip2(archive) else resources.gzip(archive)))
                into(depsDir)
            }
            marker.writeText(d.sha256)
        }
    }
}

fun sha256(f: File): String {
    val md = MessageDigest.getInstance("SHA-256")
    f.inputStream().use { s ->
        val buf = ByteArray(1 shl 16)
        while (true) {
            val n = s.read(buf)
            if (n < 0) break
            md.update(buf, 0, n)
        }
    }
    return md.digest().joinToString("") { "%02x".format(it) }
}

// Справка — та же, что в Windows-версии (rtltv/guide), открывается в WebView.
val guideAssets: File = layout.buildDirectory.dir("generated/guideAssets").get().asFile
// Цвета тёмной темы — из rtltv/themes.py («Тёмная»): SVG-схемы справки содержат шаблоны {{fg}}, {{panel}} …,
// которые окно справки на ПК подставляет при показе (render_svg), а здесь — при сборке.
val darkTheme: Map<String, String> = repoRoot.resolve("rtltv/themes.py").readText().let { src ->
    // ключ темы может быть обёрнут в N_() для перевода: N_("Тёмная"): dict(...)
    val body = Regex("""(?:N_\()?"Тёмная"\)?: dict\((.*?)cmap""", RegexOption.DOT_MATCHES_ALL).find(src)?.groupValues?.get(1)
        ?: error("не нашёл тему «Тёмная» в rtltv/themes.py")
    Regex("""(\w+)="(#[0-9a-fA-F]{6})"""").findAll(body).associate { it.groupValues[1] to it.groupValues[2] }
}

// Картинки страниц ссылаются на ../assets/sdr4noob-256.png — кладём рядом, как в настольной раскладке.
val copyGuide by tasks.registering(Sync::class) {
    inputs.property("theme", darkTheme)
    into(guideAssets)
    filteringCharset = "UTF-8"
    from(repoRoot.resolve("rtltv/guide")) {
        into("guide")
        filesMatching("**/*.svg") {
            filter { line -> darkTheme.entries.fold(line) { acc, (k, v) -> acc.replace("{{$k}}", v) } }
        }
    }
    from(repoRoot.resolve("rtltv/assets")) { include("*.png"); into("assets") }
}

// Значок — тот же логотип, что у Windows-версии (rtltv/assets, источник sdr4noob.svg → tools/make_icon.py).
val iconRes: File = layout.buildDirectory.dir("generated/iconRes").get().asFile
val copyIcon by tasks.registering(Copy::class) {
    from(repoRoot.resolve("rtltv/assets/sdr4noob-256.png")) { rename { "ic_launcher.png" } }
    into(iconRes.resolve("mipmap-xxxhdpi"))
}

android {
    namespace = "io.github.mikeileech.sdr4noob"
    compileSdk = 35
    buildToolsVersion = "35.0.0"
    ndkVersion = "27.2.12479018"

    defaultConfig {
        applicationId = "io.github.mikeileech.sdr4noob"
        minSdk = 24
        targetSdk = 35
        versionCode = appVersionCode
        versionName = appVersion
        ndk { abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64") }
        externalNativeBuild {
            cmake { arguments += listOf("-DSDR4N_DEPS=${depsDir.absolutePath.replace('\\', '/')}", "-DANDROID_STL=none") }
        }
    }

    // Ключ релиза — из секретов CI (ANDROID_KEYSTORE = путь к .jks). Без них релиз подписывается отладочным
    // ключом: такой APK ставится, но обновить им копию с настоящей подписью нельзя.
    val ks = System.getenv("ANDROID_KEYSTORE")?.let(::file)?.takeIf { it.isFile }
    signingConfigs {
        if (ks != null) {
            create("release") {
                storeFile = ks
                storePassword = System.getenv("ANDROID_KEYSTORE_PASS")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS") ?: "sdr4noob"
                keyPassword = System.getenv("ANDROID_KEY_PASS") ?: System.getenv("ANDROID_KEYSTORE_PASS")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (ks != null) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    sourceSets["main"].assets.srcDir(guideAssets)
    sourceSets["main"].res.srcDir(iconRes)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging { jniLibs { useLegacyPackaging = false } }

    testOptions { unitTests.isReturnDefaultValues = true }

    buildFeatures { buildConfig = true }
}

tasks.named("preBuild") { dependsOn(fetchNativeDeps, copyGuide, copyIcon) }
// I18nTest читает исходники и словарь перевода с диска — без этого правка одного словаря не перезапускает тесты
tasks.withType<Test>().configureEach {
    inputs.dir("src/main/assets/locale").withPropertyName("i18nCatalog")
    inputs.dir("src/main/java").withPropertyName("i18nSources")
}
tasks.configureEach {
    if (name.startsWith("configureCMake") || name.startsWith("buildCMake")) dependsOn(fetchNativeDeps)
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")   // настоящий org.json для тестов UpdateCheck (в android.jar — заглушки)
}
