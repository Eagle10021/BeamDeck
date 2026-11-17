package io.github.eagle10021.realroutes

import android.animation.TimeInterpolator
import android.content.Context
import android.os.Build
import kotlin.math.cos
import kotlin.math.exp
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * Material 3 Expressive motion: springs instead of fixed curves. Each token is a damping ratio and stiffness; the
 * interpolator is the spring's step response, so it overshoots and settles like the real thing. The duration to pair
 * with it is how long that spring takes to settle (within 0.2 %).
 */
class M3Spring(private val damping: Float, stiffness: Float) : TimeInterpolator {
    private val w = sqrt(stiffness)
    val durationMs: Long = settle()

    private fun x(t: Float): Float {
        if (damping >= 1f) return 1f - exp(-w * t) * (1f + w * t)
        val wd = w * sqrt(1f - damping * damping)
        return 1f - exp(-damping * w * t) * (cos(wd * t) + damping * w / wd * sin(wd * t))
    }

    private fun settle(): Long {
        var last = 0f; var t = 0f
        while (t < 3f) { if (kotlin.math.abs(x(t) - 1f) > 0.002f) last = t; t += 0.001f }
        return (last * 1000).toLong().coerceAtLeast(16)
    }

    override fun getInterpolation(f: Float): Float = if (f >= 1f) 1f else x(f * durationMs / 1000f)

    companion object {
        // the M3 Expressive motion scheme
        val SPATIAL_FAST = M3Spring(0.6f, 800f)
        val SPATIAL = M3Spring(0.8f, 380f)
        val SPATIAL_SLOW = M3Spring(0.8f, 200f)
        val EFFECTS_FAST = M3Spring(1f, 3800f)
        val EFFECTS = M3Spring(1f, 1600f)
        val EFFECTS_SLOW = M3Spring(1f, 800f)
    }
}

/**
 * The phone's own Material You colours (from its wallpaper), dark scheme. Android 14+ has the surface roles directly;
 * on 12-13 they come from the neutral palette's nearest tones.
 */
object DeviceColors {
    private fun c(ctx: Context, id: Int) = ctx.getColor(id)

    fun primary(ctx: Context) = c(ctx, android.R.color.system_accent1_200)
    fun onPrimary(ctx: Context) = c(ctx, android.R.color.system_accent1_800)
    fun primaryContainer(ctx: Context) = c(ctx, android.R.color.system_accent1_700)
    fun onPrimaryContainer(ctx: Context) = c(ctx, android.R.color.system_accent1_100)
    fun onSurface(ctx: Context) = c(ctx, android.R.color.system_neutral1_100)
    fun onSurfaceVariant(ctx: Context) = c(ctx, android.R.color.system_neutral2_200)
    fun outline(ctx: Context) = c(ctx, android.R.color.system_neutral2_400)

    fun surface(ctx: Context) =
        if (Build.VERSION.SDK_INT >= 34) c(ctx, android.R.color.system_surface_dark) else c(ctx, android.R.color.system_neutral1_900)
    fun surfaceContainer(ctx: Context) =
        if (Build.VERSION.SDK_INT >= 34) c(ctx, android.R.color.system_surface_container_dark) else c(ctx, android.R.color.system_neutral1_800)

    /** The wallpaper's seed, as #rrggbb, for the tablet to build the same scheme from. */
    fun seedHex(ctx: Context) = String.format("#%06x", c(ctx, android.R.color.system_accent1_500) and 0xFFFFFF)
}
