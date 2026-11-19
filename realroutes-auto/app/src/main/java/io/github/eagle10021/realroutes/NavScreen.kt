package io.github.eagle10021.realroutes

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Looper
import android.os.SystemClock
import androidx.car.app.CarContext
import androidx.car.app.Screen
import androidx.car.app.model.Action
import androidx.car.app.model.ActionStrip
import androidx.car.app.model.CarColor
import androidx.car.app.model.CarIcon
import androidx.car.app.model.DateTimeWithZone
import androidx.car.app.model.Distance
import androidx.car.app.model.MessageTemplate
import androidx.car.app.model.Template
import androidx.car.app.navigation.NavigationManager
import androidx.car.app.navigation.NavigationManagerCallback
import androidx.car.app.navigation.model.Maneuver
import androidx.car.app.navigation.model.MessageInfo
import androidx.car.app.navigation.model.NavigationTemplate
import androidx.car.app.navigation.model.RoutingInfo
import androidx.car.app.navigation.model.Step
import androidx.car.app.navigation.model.TravelEstimate
import androidx.core.content.ContextCompat
import androidx.core.graphics.drawable.IconCompat
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import org.json.JSONObject
import java.util.TimeZone

/** The main car screen: Real Routes' map with turn-by-turn from BeamNG.Earth's planned route. */
class NavScreen(ctx: CarContext, private val renderer: MapRenderer) : Screen(ctx), DefaultLifecycleObserver {
    private val nav = ctx.getCarService(NavigationManager::class.java)
    private var navigating = false
    private var lastPhoneFix = 0L
    private var lastInvalidate = 0L
    private var lastKey = ""
    private var lastAlong = 0.0
    private var offerShown: String? = null
    private val listener: () -> Unit = { onData() }

    private val gps = object : LocationListener {
        override fun onLocationChanged(l: Location) {
            // on a regular map the phone's (BeamNG.Earth) mock position means nothing: the game's position is used
            if (game()?.optBoolean("flat") == true) return
            lastPhoneFix = SystemClock.elapsedRealtime()
            renderer.setPosition(l)
            progress(l.latitude, l.longitude)
        }
    }

    init {
        lifecycle.addObserver(this)
        nav.setNavigationManagerCallback(object : NavigationManagerCallback {
            override fun onStopNavigation() { navigating = false; invalidate() }
            override fun onAutoDriveEnabled() {}
        })
    }

    @SuppressLint("MissingPermission")
    override fun onCreate(owner: LifecycleOwner) {
        HelperClient.listeners.add(listener)
        HelperClient.start(carContext)
        // the phone's own GPS: BNE Mock GPS replaces it with the car's position from BeamNG.Earth
        if (ContextCompat.checkSelfPermission(carContext, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) {
            val lm = carContext.getSystemService(LocationManager::class.java)
            for (p in listOf(LocationManager.GPS_PROVIDER, LocationManager.FUSED_PROVIDER)) {
                try { if (lm.allProviders.contains(p)) lm.requestLocationUpdates(p, 200L, 0f, gps, Looper.getMainLooper()) } catch (_: Exception) { }
            }
        }
    }

    override fun onDestroy(owner: LifecycleOwner) {
        HelperClient.listeners.remove(listener)
        HelperClient.stop()
        try { carContext.getSystemService(LocationManager::class.java).removeUpdates(gps) } catch (_: Exception) { }
        if (navigating) try { nav.navigationEnded() } catch (_: Exception) { }
    }

    private fun game(): JSONObject? = HelperClient.state?.optJSONObject("game")
    private fun rides(): JSONObject? = HelperClient.state?.optJSONObject("rides")
    /** The freight job, while one is being set up or driven (the tablet's Freight mode); null otherwise. */
    private fun freight(): JSONObject? = HelperClient.state?.optJSONObject("freight")
        ?.takeIf { it.optString("state") in listOf("resolving", "spawning", "to_pickup", "loading", "driving") && it.optJSONObject("job") != null }

    private fun clock(secs: Int): String {
        val s = Math.abs(secs); val h = s / 3600; val m = s % 3600 / 60
        return if (h > 0) "%d:%02d".format(h, m) else "%d:%02d".format(m, s % 60)
    }

    /** New helper data (twice a second). */
    private fun onData() {
        val g = game()
        if (g != null && g.optBoolean("connected"))
            renderer.setMapMode(g.optBoolean("flat"), HelperClient.url(carContext), !g.optBoolean("lights"))
        // no phone fix lately (mock not running): the helper's position stands in
        if (g != null && g.optBoolean("connected") && SystemClock.elapsedRealtime() - lastPhoneFix > 2500 && g.has("lat")) {
            val l = Location("realroutes").apply {
                latitude = g.optDouble("lat"); longitude = g.optDouble("lon")
                bearing = g.optDouble("heading", 0.0).toFloat(); speed = g.optDouble("speed", 0.0).toFloat()
                time = System.currentTimeMillis(); elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos(); accuracy = 3f
            }
            renderer.setPosition(l)
            progress(l.latitude, l.longitude)
        }
        val route = HelperClient.route
        renderer.setRoute(route?.points ?: emptyList())
        renderer.setPins(pins())
        val want = route != null && route.points.size >= 2
        if (want != navigating) {
            navigating = want
            try { if (want) nav.navigationStarted() else nav.navigationEnded() } catch (_: Exception) { }
        }
        emergency(route)
        // a ride request: the car screen shows it like the tablet does
        val r = rides(); val req = r?.optJSONObject("req")
        if (r?.optString("state") == "offered" && req != null) {
            val key = req.optString("name") + req.optJSONObject("dest")?.optString("name")
            if (offerShown != key) { offerShown = key; screenManager.push(OfferScreen(carContext)) }
        } else offerShown = null
        refresh()
    }

    private var emgKey: String? = null
    private var emgSince = 0L
    private var cineDone = false

    /** An emergency rider got in: emergency mode, and once the trip's route is in, the pull-back and draw-in. */
    private fun emergency(route: Route?) {
        val r = rides(); val q = r?.optJSONObject("req")
        val e = q?.opt("emergency")
        val on = q != null && r.optString("state") == "on_trip" && (e == true || e == "medical")
        if (!on) {
            if (emgKey != null) { emgKey = null; renderer.setEmergency(null) }
            return
        }
        val d = q!!.optJSONObject("dest") ?: return
        val key = q.optString("name") + q.optJSONObject("pickup")?.optString("name")
        if (key != emgKey) {
            emgKey = key; emgSince = SystemClock.elapsedRealtime(); cineDone = false
            renderer.setEmergency(MapRenderer.Emergency(d.optString("name"), e == "medical"))
        }
        if (cineDone) return
        val pts = route?.points ?: emptyList()
        val end = pts.lastOrNull()
        val fresh = end != null && kotlin.math.abs(end[0] - d.optDouble("lat")) + kotlin.math.abs(end[1] - d.optDouble("lon")) < 0.006
        if (fresh || SystemClock.elapsedRealtime() - emgSince > 4000) {
            cineDone = true
            val g = game()
            renderer.cinematic(if (fresh) pts else listOf(doubleArrayOf(g?.optDouble("lat") ?: d.optDouble("lat"),
                g?.optDouble("lon") ?: d.optDouble("lon")), doubleArrayOf(d.optDouble("lat"), d.optDouble("lon"))))
        }
    }

    private fun pins(): List<MapRenderer.Pin> {
        val fj = freight()?.takeIf { it.optString("state") in listOf("to_pickup", "driving") }?.optJSONObject("job")
        if (fj != null) {
            val stops = fj.optJSONArray("stops") ?: return emptyList()
            val pk = fj.optJSONObject("pickup")?.takeIf { it.has("lat") }
                ?.let { listOf(MapRenderer.Pin(it.optDouble("lat"), it.optDouble("lon"), "#4fd18b")) } ?: emptyList()
            return pk + (0 until stops.length()).mapNotNull { stops.optJSONObject(it) }
                .filter { !it.optBoolean("done") && it.has("lat") }
                .map { MapRenderer.Pin(it.optDouble("lat"), it.optDouble("lon"), if (it.optBoolean("now")) "#f06ac0" else "#ffb547") }
        }
        val r = rides() ?: return emptyList()
        val q = r.optJSONObject("req") ?: return emptyList()
        val out = mutableListOf<MapRenderer.Pin>()
        val st = r.optString("state")
        if (st != "on_trip") q.optJSONObject("pickup")?.let { out.add(MapRenderer.Pin(it.optDouble("lat"), it.optDouble("lon"), "#4fd18b")) }
        val medical = q.opt("emergency") == "medical"      // a hospital: a red pin
        q.optJSONObject("dest")?.let { out.add(MapRenderer.Pin(it.optDouble("lat"), it.optDouble("lon"), if (medical) "#e8273b" else "#f06ac0")) }
        return out
    }

    private fun progress(lat: Double, lon: Double) {
        val route = HelperClient.route ?: return
        lastAlong = route.geo.project(lat, lon).first
        refresh()
    }

    /** Ask the car for a new template when what it shows would change (at most once a second). */
    private fun refresh() {
        val route = HelperClient.route
        val step = route?.let { Nav.nextStep(it, lastAlong) }
        val bucket = step?.second?.let { if (it > 1000) (it / 100).toInt() else (it / 10).toInt() } ?: -1
        val key = "${navigating}|${step?.first?.offset}|$bucket|${statusLine()}"
        val now = SystemClock.elapsedRealtime()
        if (key != lastKey && now - lastInvalidate > 1000) { lastKey = key; lastInvalidate = now; invalidate() }
    }

    private fun destination(): String? {
        val f = freight(); val job = f?.optJSONObject("job")
        if (job != null && f.optString("state") == "to_pickup")
            return "Pickup: " + (job.optJSONObject("pickup")?.optString("name") ?: "")
        if (job != null && f.optString("state") == "driving") {
            val n = job.optJSONArray("stops")?.length() ?: 0
            val name = job.optJSONObject("stop")?.optString("name")
            if (name != null) return (if (job.optInt("idx") >= n) "Drop-off: " else "Stop ${job.optInt("idx")} of $n: ") + name
        }
        val r = rides(); val q = r?.optJSONObject("req")
        if (q != null && r.optString("state") in listOf("to_pickup", "boarding")) return "Pickup: " + q.optJSONObject("pickup")?.optString("name")
        if (q != null && r.optString("state") == "on_trip") return "Drop-off: " + q.optJSONObject("dest")?.optString("name")
        val shift = HelperClient.state?.optJSONObject("shift")
        val next = shift?.optJSONArray("next")?.optJSONObject(0)
        if (next != null && shift.optString("state") != "done") return "Next stop: " + next.optString("name")
        return game()?.optString("gpsName")?.takeIf { it.isNotBlank() && it != "null" }
    }

    private fun statusLine(): String {
        val g = game()
        if (HelperClient.lastError != null && System.currentTimeMillis() - HelperClient.lastOk > 3000)
            return "Can't reach Real Routes: ${HelperClient.lastError}"
        if (g == null || !g.optBoolean("connected")) return "Waiting for BeamNG"
        if (!g.optBoolean("ready")) return "Waiting for BeamNG.Earth's world"
        freight()?.let { f ->
            val job = f.optJSONObject("job")
            val title = job?.optString("title") ?: "Freight"
            return if (f.optString("state") == "to_pickup") {
                "$title · drive to the pickup · not loaded yet"
            } else if (f.optString("state") == "loading") {
                "$title · loading · stay stopped (${job?.optInt("load_s") ?: 0} s)"
            } else if (f.optString("state") == "driving") {
                val left = job?.optInt("left") ?: 0
                "$title · " + (if (left < 0) "late by ${clock(left)}" else "${clock(left)} left") + " · cargo ${job?.optInt("cond") ?: 100}%"
            } else job?.optString("stage")?.takeIf { it.isNotBlank() } ?: "Setting up the truck…"
        }
        val r = rides()
        return when (r?.optString("state")) {
            "online" -> "Online · finding riders"
            "offered" -> "Ride request!"
            "boarding" -> "${r.optJSONObject("req")?.optString("name")} is getting in…"
            else -> if (HelperClient.state?.optJSONObject("shift") != null) "Bus shift" else "Free drive"
        }
    }

    private fun dist(m: Double): Distance =
        if (m >= 1000) Distance.create(Math.round(m / 100.0) / 10.0, Distance.UNIT_KILOMETERS)
        else Distance.create((Math.round(m / 10.0) * 10).toDouble(), Distance.UNIT_METERS)

    override fun onGetTemplate(): Template {
        val strip = ActionStrip.Builder()
        val r = rides()
        val online = r != null && r.optString("state") != "offline"
        val fj = freight()?.optJSONObject("job")
        if (fj != null) {
            val left = fj.optInt("left")
            strip.addAction(Action.Builder().setTitle(if (freight()?.optString("state") == "driving")
                (if (left < 0) "Late " else "") + clock(left) + " · $" + fj.optInt("pay") else if (freight()?.optString("state") == "to_pickup") "To pickup" else if (freight()?.optString("state") == "loading") "Loading" else "Freight")
                .setOnClickListener { }.build())
        } else strip.addAction(Action.Builder().setTitle(if (online) "Go offline" else "Go online")
            .setOnClickListener { HelperClient.ride(carContext, if (online) "offline" else "online") }.build())
        if (fj == null && r?.optJSONObject("req") != null && r.optString("state") in listOf("to_pickup", "boarding", "on_trip"))
            strip.addAction(Action.Builder().setTitle("Cancel ride").setOnClickListener { HelperClient.ride(carContext, "cancel") }.build())
        val b = NavigationTemplate.Builder().setActionStrip(strip.build())
            .setMapActionStrip(ActionStrip.Builder().addAction(Action.Builder()
                .setIcon(CarIcon.Builder(IconCompat.createWithResource(carContext, R.drawable.m_straight)).build())
                .setOnClickListener { renderer.recenter() }.build()).build())
            .setBackgroundColor(DeviceColors.primaryContainer(carContext).let { CarColor.createCustom(it, it) })   // the turn card, in the phone's colours
        val route = HelperClient.route
        val step = if (navigating && route != null) Nav.nextStep(route, lastAlong) else null
        if (step != null) {
            val (s, d) = step
            val (type, icon) = Nav.maneuver(s)
            val mb = Maneuver.Builder(type).setIcon(CarIcon.Builder(IconCompat.createWithResource(carContext, icon)).build())
            if (type == Maneuver.TYPE_ROUNDABOUT_ENTER_AND_EXIT_CCW) mb.setRoundaboutExitNumber(maxOf(1, s.exit))
            val sb = Step.Builder(Nav.cue(s)).setManeuver(mb.build())
            if (s.name.isNotBlank()) sb.setRoad(s.name)
            b.setNavigationInfo(RoutingInfo.Builder().setCurrentStep(sb.build(), dist(d)).build())
            val g = game()
            val left = g?.optDouble("gpsLeft", Double.NaN)?.takeIf { !it.isNaN() } ?: maxOf(0.0, route!!.geo.length - lastAlong)
            val eta = g?.optDouble("gpsEta", Double.NaN)?.takeIf { !it.isNaN() && it < 1e6 } ?: (left / 8.0)
            b.setDestinationTravelEstimate(TravelEstimate.Builder(dist(left),
                DateTimeWithZone.create(System.currentTimeMillis() + (eta * 1000).toLong(), TimeZone.getDefault()))
                .setRemainingTimeSeconds(eta.toLong()).build())
        } else {
            val mi = MessageInfo.Builder(destination() ?: "Real Routes").setText(statusLine())
            b.setNavigationInfo(mi.build())
        }
        return b.build()
    }
}

/** A ride request on the car screen, with the rider's picture; Accept / Decline go to the helper (the tablet follows). */
class OfferScreen(ctx: CarContext) : Screen(ctx), DefaultLifecycleObserver {
    private val listener: () -> Unit = {
        val r = HelperClient.state?.optJSONObject("rides")
        if (r?.optString("state") != "offered") { try { screenManager.remove(this) } catch (_: Exception) { } }
        else invalidate()
    }

    init { lifecycle.addObserver(this) }
    override fun onCreate(owner: LifecycleOwner) { HelperClient.listeners.add(listener) }
    override fun onDestroy(owner: LifecycleOwner) { HelperClient.listeners.remove(listener) }

    override fun onGetTemplate(): Template {
        val q = HelperClient.state?.optJSONObject("rides")?.optJSONObject("req")
        val name = q?.optString("name") ?: "Rider"
        val msg = if (q == null) "…" else
            "★ %.2f · %s → %s\n%.1f km · ~%d min · $%.2f%s".format(q.optDouble("rating"),
                q.optJSONObject("pickup")?.optString("name"), q.optJSONObject("dest")?.optString("name"),
                q.optDouble("km"), q.optInt("mins"), q.optDouble("fare"),
                q.optString("note").takeIf { it.isNotBlank() && it != "null" }?.let { "\n“$it”" } ?: "")
        val b = MessageTemplate.Builder(msg).setTitle("Ride request: $name").setHeaderAction(Action.APP_ICON)
            .addAction(Action.Builder().setTitle("Accept").setBackgroundColor(CarColor.GREEN)
                .setOnClickListener { HelperClient.ride(carContext, "accept"); screenManager.remove(this) }.build())
            .addAction(Action.Builder().setTitle("Decline")
                .setOnClickListener { HelperClient.ride(carContext, "decline"); screenManager.remove(this) }.build())
        q?.optString("avatar")?.takeIf { it.isNotBlank() && it != "null" }?.let { av ->
            HelperClient.avatar(carContext, av) { invalidate() }?.let { b.setIcon(CarIcon.Builder(IconCompat.createWithBitmap(it)).build()) }
        }
        return b.build()
    }
}
