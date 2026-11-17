package io.github.eagle10021.realroutes

import android.annotation.SuppressLint
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
        p.setContentView(mv)
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
        mapView = null; presentation = null; display = null; map = null; style = null
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
        st.addSource(GeoJsonSource("route"))
        st.addSource(GeoJsonSource("pins"))
        add(LineLayer("route-glow", "route").withProperties(
            PropertyFactory.lineColor("#ff7a1a"), PropertyFactory.lineWidth(18f), PropertyFactory.lineOpacity(0.22f),
            PropertyFactory.lineBlur(6f), PropertyFactory.lineCap(Property.LINE_CAP_ROUND), PropertyFactory.lineJoin(Property.LINE_JOIN_ROUND)))
        add(LineLayer("route", "route").withProperties(
            PropertyFactory.lineColor("#ff7a1a"), PropertyFactory.lineWidth(7f),
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
