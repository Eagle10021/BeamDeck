package io.github.eagle10021.realroutes

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import android.app.Presentation
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.location.Location
import android.view.ContextThemeWrapper
import androidx.car.app.CarContext
import androidx.car.app.SurfaceCallback
import androidx.car.app.SurfaceContainer
import org.maplibre.android.MapLibre
import org.maplibre.android.camera.CameraUpdateFactory
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.geometry.LatLngBounds
import org.maplibre.android.location.LocationComponentActivationOptions
import org.maplibre.android.location.LocationComponentOptions
import org.maplibre.android.location.modes.CameraMode
import org.maplibre.android.location.modes.RenderMode
import org.maplibre.android.maps.MapLibreMap
import org.maplibre.android.maps.MapView
import org.maplibre.android.maps.Style
import org.maplibre.android.style.expressions.Expression
import org.maplibre.android.style.layers.CircleLayer
import org.maplibre.android.style.layers.FillExtrusionLayer
import org.maplibre.android.style.layers.FillLayer
import org.maplibre.android.style.layers.LineLayer
import org.maplibre.android.style.layers.Property
import org.maplibre.android.style.layers.PropertyFactory
import org.maplibre.android.style.layers.SymbolLayer
import org.maplibre.android.style.sources.GeoJsonOptions
import org.maplibre.android.style.sources.GeoJsonSource
import org.maplibre.geojson.Feature
import org.maplibre.geojson.FeatureCollection
import org.maplibre.geojson.LineString
import org.maplibre.geojson.Point

/**
 * The map on the car screen: MapLibre drawing OpenFreeMap's dark style into a virtual display on Android Auto's
 * surface (the usual way to put a full map view on a car app's surface). Same look as the tablet console: grey 3D
 * buildings, an orange route, pickup / drop-off pins, a chase camera; no trail.
 */
class MapRenderer(private val ctx: CarContext) : SurfaceCallback {
    companion object {
        const val DARK = "https://tiles.openfreemap.org/styles/dark"
        const val LIGHT = "https://tiles.openfreemap.org/styles/liberty"   // headlights off: a light map for daylight
    }

    private var display: VirtualDisplay? = null
    private var presentation: Presentation? = null
    private var mapView: MapView? = null
    private var map: MapLibreMap? = null
    private var style: Style? = null
    private var height = 480
    private var width = 800
    private var stable = Rect()
    private var visible = Rect()
    private var pendingRoute: List<DoubleArray> = emptyList()
    private var pendingPins: List<Pin> = emptyList()

    data class Pin(val lat: Double, val lon: Double, val color: String)

    /** An emergency ride in progress: the map goes into emergency mode (red route, siren light, banner). */
    data class Emergency(val dest: String, val medical: Boolean)

    private var sirenL: View? = null
    private var sirenR: View? = null
    private var banner: TextView? = null
    private var emg: Emergency? = null
    private val anims = mutableListOf<ValueAnimator>()
    private val main = Handler(Looper.getMainLooper())
    private val RED = "#ff2d46"
    private val ORANGE = "#ff7a1a"

    // a regular BeamNG map: the level's own roads (style from the helper) instead of OpenFreeMap's real streets
    private var flat = false
    private var base = ""
    private var light = false

    /** Which map: BeamNG.Earth's real streets or a regular map's own roads; light (headlights off) or dark. */
    fun setMapMode(isFlat: Boolean, helperUrl: String, isLight: Boolean) {
        if (isFlat == flat && helperUrl == base && isLight == light) return
        flat = isFlat; base = helperUrl; light = isLight
        loadStyle()
    }

    private fun loadStyle() {
        val m = map ?: return
        if (!flat) { m.setStyle(Style.Builder().fromUri(if (light) LIGHT else DARK)) { st -> style = st; dress(st); follow(m, st) }; return }
        val url = base
        java.util.concurrent.Executors.newSingleThreadExecutor().execute {
            val json = try {
                val c = java.net.URL("$url/api/mapstyle?light=${if (light) 1 else 0}&base=" + java.net.URLEncoder.encode(url, "UTF-8")).openConnection() as java.net.HttpURLConnection
                c.connectTimeout = 5000; c.readTimeout = 8000
                org.json.JSONObject(c.inputStream.bufferedReader().readText()).optJSONObject("style")?.toString()
            } catch (_: Exception) { null } ?: return@execute
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                map?.setStyle(Style.Builder().fromJson(json)) { st -> style = st; dress(st); follow(m, st) }
            }
        }
    }

    override fun onSurfaceAvailable(c: SurfaceContainer) {
        val surface = c.surface ?: return
        height = c.height; width = c.width
        MapLibre.getInstance(ctx.applicationContext)
        val dm = ctx.getSystemService(DisplayManager::class.java)
        val vd = dm.createVirtualDisplay("RealRoutesMap", c.width, c.height, c.dpi, surface, 0)
        display = vd
        val themed = ContextThemeWrapper(ctx, android.R.style.Theme_DeviceDefault_NoActionBar)
        val p = Presentation(themed, vd.display)
        val mv = MapView(p.context)
        // the map, with the emergency overlays on top: red / blue light at the edges and a banner
        val root = FrameLayout(p.context)
        root.addView(mv, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        val edge = (c.width * 0.18).toInt()
        sirenL = View(p.context).apply {
            background = GradientDrawable(GradientDrawable.Orientation.LEFT_RIGHT, intArrayOf(0x99FF2840.toInt(), 0)); alpha = 0f }
        sirenR = View(p.context).apply {
            background = GradientDrawable(GradientDrawable.Orientation.RIGHT_LEFT, intArrayOf(0x99286EFF.toInt(), 0)); alpha = 0f }
        root.addView(sirenL, FrameLayout.LayoutParams(edge, ViewGroup.LayoutParams.MATCH_PARENT, Gravity.START))
        root.addView(sirenR, FrameLayout.LayoutParams(edge, ViewGroup.LayoutParams.MATCH_PARENT, Gravity.END))
        banner = TextView(p.context).apply {
            setTextColor(Color.WHITE); gravity = Gravity.CENTER; visibility = View.GONE
            setTextSize(TypedValue.COMPLEX_UNIT_PX, c.height * 0.042f)
            val pad = (c.height * 0.025f).toInt(); setPadding(pad * 2, pad, pad * 2, pad)
            background = GradientDrawable(GradientDrawable.Orientation.TL_BR, intArrayOf(0xF0B0122A.toInt(), 0xF06D0A1C.toInt())).apply {
                cornerRadius = c.height * 0.03f; setStroke(2, 0xFFFF4D63.toInt()) }
        }
        root.addView(banner, FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.BOTTOM or Gravity.START))
        p.setContentView(root)
        mv.onCreate(null); mv.onStart(); mv.onResume()
        p.show()
        presentation = p; mapView = mv
        mv.getMapAsync { m ->
            map = m
            m.uiSettings.isCompassEnabled = false
            m.uiSettings.isLogoEnabled = false
            loadStyle()
        }
    }

    override fun onSurfaceDestroyed(c: SurfaceContainer) {
        mapView?.let { it.onPause(); it.onStop(); it.onDestroy() }
        presentation?.dismiss(); display?.release()
        anims.forEach { it.cancel() }; anims.clear(); main.removeCallbacksAndMessages(null)
        mapView = null; presentation = null; display = null; map = null; style = null
        sirenL = null; sirenR = null; banner = null
    }

    override fun onStableAreaChanged(r: Rect) { stable = r; padding() }

    // split screen (map + a media / app card) or the turn card: the part of the map nobody covers
    override fun onVisibleAreaChanged(r: Rect) { visible = r; padding() }

    /** Tints, 3D buildings, the route and the pins, all under the map's labels. */
    private fun dress(st: Style) {
        if (!light) for (l in st.layers) {
            if (l is FillLayer) when (l.sourceLayer) {
                "water" -> l.setProperties(PropertyFactory.fillColor("#16303f"))
                "park" -> l.setProperties(PropertyFactory.fillColor("#17251b"))
                "landcover" -> l.setProperties(PropertyFactory.fillColor("#171f1a"))
            }
        }
        val below = st.layers.firstOrNull { it is SymbolLayer }?.id
        val add = { layer: org.maplibre.android.style.layers.Layer ->
            if (below != null) st.addLayerBelow(layer, below) else st.addLayer(layer)
        }
        st.addSource(GeoJsonSource("route", GeoJsonOptions().withLineMetrics(true)))   // line progress: the draw-in
        st.addSource(GeoJsonSource("pins"))
        add(LineLayer("route-glow", "route").withProperties(
            PropertyFactory.lineColor("#ff7a1a"), PropertyFactory.lineWidth(18f), PropertyFactory.lineOpacity(0.22f),
            PropertyFactory.lineBlur(6f), PropertyFactory.lineCap(Property.LINE_CAP_ROUND), PropertyFactory.lineJoin(Property.LINE_JOIN_ROUND)))
        add(LineLayer("route", "route").withProperties(
            PropertyFactory.lineColor("#ff7a1a"), PropertyFactory.lineWidth(7f),
            PropertyFactory.lineCap(Property.LINE_CAP_ROUND), PropertyFactory.lineJoin(Property.LINE_JOIN_ROUND)))
        // only shown while an emergency route draws itself in (a gradient cut off at the drawn part)
        add(LineLayer("route-draw", "route").withProperties(
            PropertyFactory.lineWidth(9f), PropertyFactory.visibility(Property.NONE),
            PropertyFactory.lineCap(Property.LINE_CAP_ROUND), PropertyFactory.lineJoin(Property.LINE_JOIN_ROUND)))
        val bld = FillExtrusionLayer("bld3d", "openmaptiles").withSourceLayer("building").withProperties(
            PropertyFactory.fillExtrusionColor(if (light) "#ddd6cb" else "#5b616b"), PropertyFactory.fillExtrusionOpacity(1f),
            PropertyFactory.fillExtrusionHeight(Expression.coalesce(Expression.get("render_height"), Expression.literal(8))),
            PropertyFactory.fillExtrusionBase(Expression.coalesce(Expression.get("render_min_height"), Expression.literal(0))))
        bld.minZoom = 14f
        if (st.getSource("openmaptiles") != null) add(bld)
        st.addLayer(CircleLayer("pins", "pins").withProperties(
            PropertyFactory.circleColor(Expression.toColor(Expression.get("color"))), PropertyFactory.circleRadius(10f),
            PropertyFactory.circleStrokeColor("#ffffff"), PropertyFactory.circleStrokeWidth(3f),
            PropertyFactory.circlePitchAlignment(Property.CIRCLE_PITCH_ALIGNMENT_MAP)))
        setRoute(pendingRoute); setPins(pendingPins)
        routeColor()
    }

    private fun line(id: String) = style?.getLayerAs<LineLayer>(id)

    private fun routeColor() {
        val c = if (emg != null) RED else ORANGE
        line("route")?.setProperties(PropertyFactory.lineColor(c))
        line("route-glow")?.setProperties(PropertyFactory.lineColor(c))
    }

    /** The part of the screen nobody covers (split screen's media card, the stable area), for placing things. */
    private fun area(): Rect = when {
        visible.width() > 0 && visible.height() > 0 -> visible
        stable.width() > 0 && stable.height() > 0 -> stable
        else -> Rect(0, 0, width, height)
    }

    /** Emergency mode on (an emergency rider is in the car) or off (null). */
    fun setEmergency(e: Emergency?) {
        if (e == emg) return
        emg = e
        anims.forEach { it.cancel() }; anims.clear()
        routeColor()
        val b = banner
        if (e == null) {
            sirenL?.animate()?.alpha(0f)?.setInterpolator(M3Spring.EFFECTS)?.setDuration(M3Spring.EFFECTS.durationMs)?.start()
            sirenR?.animate()?.alpha(0f)?.setInterpolator(M3Spring.EFFECTS)?.setDuration(M3Spring.EFFECTS.durationMs)?.start()
            // the banner shrinks away (spatial) as it fades (effects)
            b?.animate()?.scaleX(.8f)?.scaleY(.8f)?.translationY(height * 0.08f)?.alpha(0f)
                ?.setInterpolator(M3Spring.SPATIAL)?.setDuration(M3Spring.SPATIAL.durationMs)
                ?.withEndAction { b.visibility = View.GONE; b.scaleX = 1f; b.scaleY = 1f }?.start()
            line("route-glow")?.setProperties(PropertyFactory.lineOpacity(0.22f), PropertyFactory.lineWidth(18f))
            line("route-draw")?.setProperties(PropertyFactory.visibility(Property.NONE))
            line("route")?.setProperties(PropertyFactory.visibility(Property.VISIBLE))
            line("route-glow")?.setProperties(PropertyFactory.visibility(Property.VISIBLE))
            return
        }
        // siren light: red on the left, then blue on the right, for the first 10 seconds
        val siren = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 1100; repeatCount = ValueAnimator.INFINITE
            addUpdateListener { v -> val first = (v.animatedValue as Float) < 0.5f
                sirenL?.alpha = if (first) 0.9f else 0.15f; sirenR?.alpha = if (first) 0.15f else 0.9f }
            start()
        }
        anims += siren
        main.postDelayed({
            if (emg != e) return@postDelayed
            siren.cancel()
            sirenL?.animate()?.alpha(0f)?.setInterpolator(M3Spring.EFFECTS_SLOW)?.setDuration(1000)?.start()
            sirenR?.animate()?.alpha(0f)?.setInterpolator(M3Spring.EFFECTS_SLOW)?.setDuration(1000)?.start()
        }, 10000)
        // the route's glow breathes
        anims += ValueAnimator.ofFloat(0f, (2 * Math.PI).toFloat()).apply {
            duration = 2600; repeatCount = ValueAnimator.INFINITE
            addUpdateListener { v -> val s = kotlin.math.sin(v.animatedValue as Float)
                line("route-glow")?.setProperties(PropertyFactory.lineOpacity(0.25f + 0.25f * s), PropertyFactory.lineWidth(18f + 8f * s)) }
            start()
        }
        if (b != null) {
            b.text = "EMERGENCY\n${e.dest}\n" + if (e.medical) "Medical: to the hospital" else "As fast as you can"
            placeBanner(b)
            // springs up into place with a little bounce (spatial fast) while fading in (effects)
            b.alpha = 0f; b.translationY = height * 0.2f; b.scaleX = .7f; b.scaleY = .7f; b.visibility = View.VISIBLE
            b.animate().translationY(0f).scaleX(1f).scaleY(1f)
                .setInterpolator(M3Spring.SPATIAL_FAST).setDuration(M3Spring.SPATIAL_FAST.durationMs * 3 / 2).start()
            android.animation.ObjectAnimator.ofFloat(b, View.ALPHA, 0f, 1f).apply {
                interpolator = M3Spring.EFFECTS; duration = M3Spring.EFFECTS_SLOW.durationMs; start() }
            main.postDelayed({                                          // then a small chip, with a squish as it shrinks
                if (emg != e) return@postDelayed
                b.text = "EMERGENCY"; placeBanner(b)
                b.scaleX = 1.14f; b.scaleY = .82f
                b.animate().scaleX(1f).scaleY(1f).setInterpolator(M3Spring.SPATIAL_FAST).setDuration(M3Spring.SPATIAL_FAST.durationMs).start()
            }, 7000)
        }
    }

    private fun placeBanner(b: TextView) {
        val a = area()
        b.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED)
        (b.layoutParams as FrameLayout.LayoutParams).apply {
            leftMargin = (a.left + (a.width() - b.measuredWidth) / 2).coerceAtLeast(0)
            bottomMargin = (height - a.bottom).coerceAtLeast(0) + (height * 0.04f).toInt()
        }
        b.requestLayout()
    }

    /** The rider is in: pull back to show the whole route, draw it in, then swoop back down behind the car. */
    fun cinematic(points: List<DoubleArray>) {
        val m = map ?: return
        if (emg == null || points.size < 2) return
        val lc = try { m.locationComponent } catch (_: Exception) { return }
        val bounds = LatLngBounds.Builder()
        points.forEach { bounds.include(LatLng(it[0], it[1])) }
        lc.lastKnownLocation?.let { bounds.include(LatLng(it.latitude, it.longitude)) }
        val a = area()
        val pad = (height * 0.12).toInt()
        try {
            lc.cameraMode = CameraMode.NONE
            m.animateCamera(CameraUpdateFactory.newLatLngBounds(bounds.build(), 0.0, 0.0,
                a.left + pad, a.top + pad, (width - a.right) + pad, (height - a.bottom) + pad), 1600)
        } catch (_: Exception) { }
        // the route draws itself from the car to the destination
        line("route")?.setProperties(PropertyFactory.visibility(Property.NONE))
        line("route-glow")?.setProperties(PropertyFactory.visibility(Property.NONE))
        val draw = line("route-draw")
        draw?.setProperties(PropertyFactory.visibility(Property.VISIBLE))
        val red = Color.parseColor(RED)
        anims += ValueAnimator.ofFloat(0f, 1f).apply {
            startDelay = 700; duration = 1800
            interpolator = M3Spring.SPATIAL_SLOW                       // overshoot is clamped: the line can't pass its end
            addUpdateListener { v ->
                val f = (v.animatedValue as Float).coerceIn(0.0001f, 1f)
                draw?.setProperties(PropertyFactory.lineGradient(Expression.step(Expression.lineProgress(),
                    Expression.color(red), Expression.stop(f, Expression.color(Color.TRANSPARENT)))))
            }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(an: android.animation.Animator) {
                    draw?.setProperties(PropertyFactory.visibility(Property.NONE))
                    line("route")?.setProperties(PropertyFactory.visibility(Property.VISIBLE))
                    line("route-glow")?.setProperties(PropertyFactory.visibility(Property.VISIBLE))
                }
            })
            start()
        }
        main.postDelayed({                                    // back down behind the car
            try {
                lc.cameraMode = CameraMode.TRACKING_GPS
                lc.zoomWhileTracking(17.5, 1800)
                lc.tiltWhileTracking(60.0, 1800)
                padding()
            } catch (_: Exception) { }
        }, 4300)
    }

    /** The car: an orange-tinted arrow, followed by a tilted heading-up camera (like the phone's 3D chase). */
    @SuppressLint("MissingPermission")
    private fun follow(m: MapLibreMap, st: Style) {
        val opts = LocationComponentOptions.builder(ctx)
            .gpsDrawable(R.drawable.puck).accuracyAlpha(0f)
            .minZoomIconScale(2.0f).maxZoomIconScale(2.6f).build()   // car screens are low-density: a bigger car
        val lc = m.locationComponent
        lc.activateLocationComponent(LocationComponentActivationOptions.builder(ctx, st)
            .locationComponentOptions(opts).useDefaultLocationEngine(false).build())
        lc.isLocationComponentEnabled = true
        lc.cameraMode = CameraMode.TRACKING_GPS
        lc.renderMode = RenderMode.GPS
        lc.zoomWhileTracking(17.0)
        lc.tiltWhileTracking(60.0)
        padding()
    }

    private fun padding() {
        val m = map ?: return
        // the car sits in the middle of the uncovered part, low down, so more road shows ahead
        val area = when {
            visible.width() > 0 && visible.height() > 0 -> visible
            stable.width() > 0 && stable.height() > 0 -> stable
            else -> Rect(0, 0, width, height)
        }
        // split screen puts its media card on the right: keep clear of it. The turn card on the left only covers
        // the top corner, so the car stays centred left to right in full screen.
        val left = 0.0
        val right = (width - area.right).toDouble().coerceAtLeast(0.0)
        val top = area.top + area.height() * 0.45
        val bottom = (height - area.bottom).toDouble().coerceAtLeast(0.0)
        try { m.locationComponent.paddingWhileTracking(doubleArrayOf(left, top, right, bottom), 400) } catch (_: Exception) { }
    }

    private var framed = false

    fun setPosition(loc: Location) {
        val m = map ?: return
        try {
            val lc = m.locationComponent
            if (!lc.isLocationComponentActivated) return
            lc.forceLocationUpdate(loc)
            if (!framed) {                       // zoom and tilt only take hold once there is a position to track
                framed = true
                lc.cameraMode = CameraMode.TRACKING_GPS
                lc.zoomWhileTracking(17.5, 800)
                lc.tiltWhileTracking(60.0, 800)
            }
        } catch (_: Exception) { }
    }

    fun setRoute(points: List<DoubleArray>) {
        pendingRoute = points
        val src = style?.getSourceAs<GeoJsonSource>("route") ?: return
        if (points.size < 2) src.setGeoJson(FeatureCollection.fromFeatures(emptyList<Feature>()))
        else src.setGeoJson(Feature.fromGeometry(LineString.fromLngLats(points.map { Point.fromLngLat(it[1], it[0]) })))
    }

    fun setPins(pins: List<Pin>) {
        pendingPins = pins
        val src = style?.getSourceAs<GeoJsonSource>("pins") ?: return
        src.setGeoJson(FeatureCollection.fromFeatures(pins.map {
            Feature.fromGeometry(Point.fromLngLat(it.lon, it.lat)).also { f -> f.addStringProperty("color", it.color) }
        }))
    }

    /** Back to following the car (the camera can drift after a style or route change). */
    fun recenter() {
        val m = map ?: return
        try { m.locationComponent.cameraMode = CameraMode.TRACKING_GPS } catch (_: Exception) { }
        framed = false
    }
}
