package io.github.eagle10021.realroutes

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.core.content.FileProvider
import org.json.JSONArray
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * Checks the repo's GitHub Releases for a newer Real Routes app and offers to install it.
 * Releases are tagged v<version>-realroutes (for example v0.2.0-realroutes) and carry the .apk as an asset.
 * The new APK must be signed with the same key as the installed one, or Android refuses the update.
 */
object Updater {
    private const val REPO = "Eagle10021/BeamDeck"
    private const val TAG_SUFFIX = "-realroutes"
    private const val CHECK_EVERY_MS = 6 * 3600_000L
    private val exec = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    data class Release(val version: String, val notes: String, val apkUrl: String, val apkSize: Long, val page: String)

    @Volatile var latest: Release? = null
        private set

    fun installedVersion(ctx: Context): String =
        ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "0"

    /** Compares dotted versions numerically: 0.10.0 > 0.9.2. */
    fun newer(a: String, b: String): Boolean {
        val x = a.split('.').map { it.toIntOrNull() ?: 0 }
        val y = b.split('.').map { it.toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(x.size, y.size)) {
            val d = x.getOrElse(i) { 0 } - y.getOrElse(i) { 0 }
            if (d != 0) return d > 0
        }
        return false
    }

    private fun fetchLatest(): Release? {
        val c = URL("https://api.github.com/repos/$REPO/releases?per_page=30").openConnection() as HttpURLConnection
        c.connectTimeout = 8000; c.readTimeout = 8000
        c.setRequestProperty("Accept", "application/vnd.github+json")
        c.setRequestProperty("User-Agent", "RealRoutes-Auto")
        try {
            if (c.responseCode != 200) throw IllegalStateException("GitHub answered ${c.responseCode}")
            val arr = JSONArray(c.inputStream.bufferedReader().readText())
            var best: Release? = null
            for (i in 0 until arr.length()) {
                val r = arr.getJSONObject(i)
                if (r.optBoolean("draft") || r.optBoolean("prerelease")) continue
                val tag = r.optString("tag_name")
                if (!tag.endsWith(TAG_SUFFIX)) continue
                val version = tag.removePrefix("v").removeSuffix(TAG_SUFFIX)
                val assets = r.optJSONArray("assets") ?: continue
                val apk = (0 until assets.length()).map { assets.getJSONObject(it) }
                    .firstOrNull { it.optString("name").endsWith(".apk") } ?: continue
                val rel = Release(version, r.optString("body").trim(), apk.optString("browser_download_url"),
                    apk.optLong("size"), r.optString("html_url"))
                if (best == null || newer(rel.version, best.version)) best = rel
            }
            return best
        } finally { c.disconnect() }
    }

    /**
     * Looks for an update (at most every 6 hours unless [force]) and shows the prompt when one is found.
     * [done] gets a short status line for the screen, on the main thread.
     */
    fun check(act: Activity, force: Boolean = false, done: (String) -> Unit = {}) {
        val prefs = act.getSharedPreferences("updater", Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        if (!force && now - prefs.getLong("checked", 0) < CHECK_EVERY_MS) {
            latest?.let { if (shouldPrompt(act, it)) prompt(act, it) }
            return
        }
        exec.execute {
            val msg = try {
                val rel = fetchLatest()
                prefs.edit().putLong("checked", now).apply()
                latest = rel
                val mine = installedVersion(act)
                if (rel != null && newer(rel.version, mine)) {
                    main.post { if (!act.isFinishing && (force || shouldPrompt(act, rel))) prompt(act, rel) }
                    "Update available: ${rel.version}"
                } else "Up to date ($mine)"
            } catch (e: Exception) {
                "Couldn't check for updates: ${e.message ?: e.javaClass.simpleName}"
            }
            main.post { done(msg) }
        }
    }

    private fun shouldPrompt(ctx: Context, rel: Release) =
        newer(rel.version, installedVersion(ctx)) &&
            ctx.getSharedPreferences("updater", Context.MODE_PRIVATE).getString("skipped", null) != rel.version

    private fun prompt(act: Activity, rel: Release) {
        val notes = rel.notes.lines().filter { it.isNotBlank() }.take(8).joinToString("\n")
        val size = if (rel.apkSize > 0) " (%.0f MB)".format(rel.apkSize / 1_048_576.0) else ""
        AlertDialog.Builder(act)
            .setTitle("Update available")
            .setMessage("Real Routes ${rel.version}$size is out. This device has ${installedVersion(act)}." +
                if (notes.isNotEmpty()) "\n\n$notes" else "")
            .setPositiveButton("Update") { _, _ -> download(act, rel) }
            .setNegativeButton("Later", null)
            .setNeutralButton("Skip this version") { _, _ ->
                act.getSharedPreferences("updater", Context.MODE_PRIVATE).edit().putString("skipped", rel.version).apply()
            }
            .show()
    }

    /** Downloads the APK into the app's cache and hands it to Android's installer. */
    private fun download(act: Activity, rel: Release) {
        if (!act.packageManager.canRequestPackageInstalls()) {
            AlertDialog.Builder(act)
                .setTitle("Allow installs")
                .setMessage("Android needs permission for Real Routes to install its own updates. Turn on \"Allow from this source\", then come back and tap Update again.")
                .setPositiveButton("Open settings") { _, _ ->
                    act.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${act.packageName}")))
                }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }
        val progress = AlertDialog.Builder(act).setTitle("Downloading ${rel.version}").setMessage("Starting…")
            .setCancelable(false).show()
        exec.execute {
            val dir = File(act.cacheDir, "updates").apply { mkdirs(); listFiles()?.forEach { it.delete() } }
            val file = File(dir, "RealRoutes-Auto-${rel.version}.apk")
            try {
                var url = URL(rel.apkUrl)
                var c: HttpURLConnection
                // GitHub asset links redirect to another host, which HttpURLConnection won't follow on its own
                while (true) {
                    c = url.openConnection() as HttpURLConnection
                    c.instanceFollowRedirects = false
                    c.connectTimeout = 10000; c.readTimeout = 20000
                    c.setRequestProperty("User-Agent", "RealRoutes-Auto")
                    if (c.responseCode in 300..399) { url = URL(url, c.getHeaderField("Location")); c.disconnect(); continue }
                    break
                }
                if (c.responseCode != 200) throw IllegalStateException("download answered ${c.responseCode}")
                val total = c.contentLengthLong.takeIf { it > 0 } ?: rel.apkSize
                c.inputStream.use { input ->
                    file.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024)
                        var got = 0L; var shown = -1
                        while (true) {
                            val n = input.read(buf); if (n < 0) break
                            out.write(buf, 0, n); got += n
                            val pct = if (total > 0) (got * 100 / total).toInt() else -1
                            if (pct != shown) { shown = pct; main.post { progress.setMessage(if (pct >= 0) "$pct %" else "${got / 1_048_576} MB") } }
                        }
                    }
                }
                c.disconnect()
                main.post {
                    progress.dismiss()
                    val uri = FileProvider.getUriForFile(act, "${act.packageName}.updates", file)
                    act.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
                }
            } catch (e: Exception) {
                file.delete()
                main.post {
                    progress.dismiss()
                    AlertDialog.Builder(act).setTitle("Update failed")
                        .setMessage("${e.message ?: e.javaClass.simpleName}\n\nThe release page has the APK too.")
                        .setPositiveButton("Open release page") { _, _ ->
                            act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(rel.page)))
                        }
                        .setNegativeButton("Close", null).show()
                }
            }
        }
    }
}
