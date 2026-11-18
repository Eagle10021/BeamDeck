package io.github.eagle10021.realroutes

import androidx.car.app.navigation.model.Maneuver
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/** A route line in local metres (equirectangular round its first point) for "how far along am I". */
class Geo(private val pts: List<DoubleArray>) {
    private val lat0 = pts.firstOrNull()?.get(0) ?: 0.0
    private val lon0 = pts.firstOrNull()?.get(1) ?: 0.0
    private val kx = 111320.0 * cos(Math.toRadians(lat0))
    private val ky = 110574.0
    private val xy = pts.map { doubleArrayOf((it[1] - lon0) * kx, (it[0] - lat0) * ky) }
    val cum = DoubleArray(pts.size).also {
        for (i in 1 until pts.size) it[i] = it[i - 1] + hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1])
    }
    val length get() = if (cum.isEmpty()) 0.0 else cum.last()

    /** (metres along the route, metres off it) of the nearest point. */
    fun project(lat: Double, lon: Double): Pair<Double, Double> {
        if (xy.size < 2) return 0.0 to Double.MAX_VALUE
        val x = (lon - lon0) * kx; val y = (lat - lat0) * ky
        var best = 0.0; var bestD = Double.MAX_VALUE
        for (i in 0 until xy.size - 1) {
            val ax = xy[i][0]; val ay = xy[i][1]; val dx = xy[i + 1][0] - ax; val dy = xy[i + 1][1] - ay
            val l2 = max(dx * dx + dy * dy, 1e-9)
            val t = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / l2))
            val d = hypot(x - (ax + t * dx), y - (ay + t * dy))
            if (d < bestD) { bestD = d; best = cum[i] + t * Math.sqrt(l2) }
        }
        return best to bestD
    }
}

object Nav {
    /** The next turn ahead of `along` metres (the start of the route is skipped), and how far it is. */
    fun nextStep(route: Route, along: Double): Pair<RouteStep, Double>? {
        for (s in route.steps) {
            if (s.offset.isNaN() || !worthShowing(s)) continue
            if (s.offset > along + 3) return s to (s.offset - along)
        }
        return null
    }

    /** A real manoeuvre, not the road bending or changing its name (the helper's nav.py uses the same rule). */
    fun worthShowing(s: RouteStep): Boolean = when {
        s.type == "depart" -> false
        s.type in listOf("new name", "continue", "notification", "use lane") ->
            s.modifier in listOf("left", "right", "sharp left", "sharp right", "uturn")
        s.type == "turn" && s.modifier in listOf("", "straight") -> false
        else -> true
    }

    /** OSRM maneuver -> Android Auto maneuver type and our arrow. */
    fun maneuver(s: RouteStep): Pair<Int, Int> {
        val m = s.modifier
        val left = m.contains("left"); val right = m.contains("right")
        return when (s.type) {
            "arrive" -> when {
                left -> Maneuver.TYPE_DESTINATION_LEFT; right -> Maneuver.TYPE_DESTINATION_RIGHT
                else -> Maneuver.TYPE_DESTINATION
            } to R.drawable.m_arrive
            "depart" -> Maneuver.TYPE_DEPART to R.drawable.m_depart
            "roundabout", "rotary", "roundabout turn", "exit roundabout", "exit rotary" ->
                Maneuver.TYPE_ROUNDABOUT_ENTER_AND_EXIT_CCW to R.drawable.m_roundabout
            "merge" -> Maneuver.TYPE_MERGE_SIDE_UNSPECIFIED to R.drawable.m_merge
            "fork" -> (if (left) Maneuver.TYPE_FORK_LEFT else Maneuver.TYPE_FORK_RIGHT) to
                (if (left) R.drawable.m_slight_left else R.drawable.m_slight_right)
            "on ramp" -> (if (left) Maneuver.TYPE_ON_RAMP_NORMAL_LEFT else Maneuver.TYPE_ON_RAMP_NORMAL_RIGHT) to
                (if (left) R.drawable.m_slight_left else R.drawable.m_slight_right)
            "off ramp" -> (if (left) Maneuver.TYPE_OFF_RAMP_NORMAL_LEFT else Maneuver.TYPE_OFF_RAMP_NORMAL_RIGHT) to
                (if (left) R.drawable.m_slight_left else R.drawable.m_slight_right)
            else -> when (m) {
                "uturn" -> Maneuver.TYPE_U_TURN_LEFT to R.drawable.m_uturn
                "sharp left" -> Maneuver.TYPE_TURN_SHARP_LEFT to R.drawable.m_sharp_left
                "sharp right" -> Maneuver.TYPE_TURN_SHARP_RIGHT to R.drawable.m_sharp_right
                "slight left" -> Maneuver.TYPE_TURN_SLIGHT_LEFT to R.drawable.m_slight_left
                "slight right" -> Maneuver.TYPE_TURN_SLIGHT_RIGHT to R.drawable.m_slight_right
                "left" -> Maneuver.TYPE_TURN_NORMAL_LEFT to R.drawable.m_left
                "right" -> Maneuver.TYPE_TURN_NORMAL_RIGHT to R.drawable.m_right
                else -> Maneuver.TYPE_STRAIGHT to R.drawable.m_straight
            }
        }
    }

    /** The words on the turn card: "Turn right onto Merivale Road". */
    fun cue(s: RouteStep): String {
        val road = s.name.ifBlank { "" }
        val onto = if (road.isNotEmpty()) " onto $road" else ""
        val m = s.modifier
        return when (s.type) {
            "arrive" -> "Arrive" + if (m.contains("left")) " (on the left)" else if (m.contains("right")) " (on the right)" else ""
            "roundabout", "rotary", "roundabout turn" -> "At the roundabout, take exit ${max(1, s.exit)}$onto"
            "merge" -> "Merge$onto"
            "fork" -> "Keep ${if (m.contains("left")) "left" else "right"}$onto"
            "on ramp" -> "Take the ramp$onto"
            "off ramp" -> "Take the exit$onto"
            "end of road" -> "At the end of the road, turn ${if (m.contains("left")) "left" else "right"}$onto"
            else -> when (m) {
                "uturn" -> "Make a U-turn$onto"
                "straight" -> "Continue$onto"
                "" -> "Continue$onto"
                "slight left", "slight right" -> "Bear ${if (m.contains("left")) "left" else "right"}$onto"
                "sharp left", "sharp right" -> "Turn sharply ${if (m.contains("left")) "left" else "right"}$onto"
                else -> "Turn $m$onto"
            }
        }.replaceFirstChar { it.uppercase() }
    }
}
