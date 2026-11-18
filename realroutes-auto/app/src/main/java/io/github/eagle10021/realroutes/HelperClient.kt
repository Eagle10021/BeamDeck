package io.github.eagle10021.realroutes

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/** One turn of the planned route (an OSRM maneuver from BeamNG.Earth's planner). */
data class RouteStep(
    val type: String, val modifier: String, val name: String, val exit: Int,
    val offset: Double, val lat: Double, val lon: Double,
)

/** BeamNG.Earth's planned GPS route, as the Real Routes helper relays it. */
class Route(val version: String?, val points: List<DoubleArray>, val steps: List<RouteStep>, val dist: Double) {
    val geo = Geo(points)

    companion object {
        fun parse(o: JSONObject): Route {
            val pts = mutableListOf<DoubleArray>()
            val pa = o.optJSONArray("points") ?: JSONArray()
            for (i in 0 until pa.length()) {
                val p = pa.optJSONArray(i) ?: continue
                pts.add(doubleArrayOf(p.optDouble(0), p.optDouble(1)))
            }
            val steps = mutableListOf<RouteStep>()
            val sa = o.optJSONArray("steps") ?: JSONArray()
            for (i in 0 until sa.length()) {
                val s = sa.optJSONObject(i) ?: continue
                steps.add(RouteStep(s.optString("type"), s.optString("modifier"), s.optString("name"),
                    s.optInt("exit", 1), s.optDouble("offset", Double.NaN), s.optDouble("lat"), s.optDouble("lon")))
            }
            return Route(o.opt("version")?.toString(), pts, steps.sortedBy { if (it.offset.isNaN()) 0.0 else it.offset },
                o.optDouble("dist", 0.0))
        }
    }
}

/**
 * The link to the Real Routes helper (BeamDeck): polls /api/state twice a second and fetches the route when it changes.
 * The tablet console reads the same state, so both always show the same ride, request and route.
 */
object HelperClient {
    @Volatile var state: JSONObject? = null
    @Volatile var route: Route? = null
    @Volatile var lastOk = 0L
    @Volatile var lastError: String? = null
    val listeners = CopyOnWriteArrayList<() -> Unit>()
    private var exec: ScheduledExecutorService? = null
    private var routeVersion: Any? = "none"
    private val main = Handler(Looper.getMainLooper())
    private val avatars = HashMap<String, Bitmap?>()
    private var users = 0

    fun url(ctx: Context): String =
        ctx.getSharedPreferences("rr", Context.MODE_PRIVATE).getString("helper_url", "")!!.trim().trimEnd('/')

    fun setUrl(ctx: Context, u: String) {
        ctx.getSharedPreferences("rr", Context.MODE_PRIVATE).edit().putString("helper_url", u.trim()).apply()
        routeVersion = "none"
    }

    @Synchronized fun start(ctx: Context) {
        users++
        if (exec != null) return
        val app = ctx.applicationContext
        post(app, "/api/theme", JSONObject().put("seed", DeviceColors.seedHex(app)))   // the tablet can match the phone's colours
        Speaker.start(app)                           // speaks when the helper sends speech to the phone
        exec = Executors.newSingleThreadScheduledExecutor().also {
            it.scheduleWithFixedDelay({ poll(app) }, 0, 500, TimeUnit.MILLISECONDS)
        }
    }

    @Synchronized fun stop() {
        users = maxOf(0, users - 1)
        if (users == 0) { exec?.shutdownNow(); exec = null }
    }

    private fun get(u: String, timeout: Int = 4000): ByteArray {
        val c = URL(u).openConnection() as HttpURLConnection
        c.connectTimeout = timeout; c.readTimeout = timeout
        try {
            if (c.responseCode != 200) throw RuntimeException("HTTP ${c.responseCode}")
            return c.inputStream.use { it.readBytes() }
        } finally { c.disconnect() }
    }

    private fun poll(ctx: Context) {
        val base = url(ctx)
        if (base.isEmpty()) { lastError = "No helper address set"; notifyAll(); return }
        try {
            val st = JSONObject(String(get("$base/api/state?from=aa")))   // from=aa: the helper knows the phone can speak
            state = st
            main.post { Speaker.onState(st.optJSONArray("pa"), st.optJSONObject("prefs")) }
            val v = st.optJSONObject("game")?.opt("routeVersion")
            if (v.toString() != routeVersion.toString()) {
                routeVersion = v
                route = if (v == null || v == JSONObject.NULL) null else Route.parse(JSONObject(String(get("$base/api/route"))))
            }
            lastOk = System.currentTimeMillis(); lastError = null
        } catch (e: Exception) {
            lastError = e.message ?: e.javaClass.simpleName
        }
        notifyAll()
    }

    private fun notifyAll() = main.post { listeners.forEach { it() } }

    /** POST to the helper (ride actions); done off the main thread. */
    fun post(ctx: Context, path: String, body: JSONObject) {
        val base = url(ctx)
        if (base.isEmpty()) return
        Executors.newSingleThreadExecutor().execute {
            try {
                val c = URL("$base$path").openConnection() as HttpURLConnection
                c.requestMethod = "POST"; c.doOutput = true; c.connectTimeout = 4000; c.readTimeout = 4000
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toString().toByteArray()) }
                c.responseCode; c.disconnect()
            } catch (_: Exception) { }
        }
    }

    fun ride(ctx: Context, action: String) = post(ctx, "/api/rides", JSONObject().put("action", action))

    /** A rider's picture from the helper (cached); null until it has loaded. `done` runs on the main thread. */
    fun avatar(ctx: Context, name: String, done: () -> Unit): Bitmap? {
        synchronized(avatars) {
            if (avatars.containsKey(name)) return avatars[name]
            avatars[name] = null                     // in flight
        }
        val base = url(ctx)
        Executors.newSingleThreadExecutor().execute {
            val bmp = try {
                val bytes = get("$base/avatars/" + URLEncoder.encode(name, "UTF-8").replace("+", "%20") + ".jpg")
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            } catch (_: Exception) { null }
            synchronized(avatars) { avatars[name] = bmp }
            main.post(done)
        }
        return null
    }
}
