package io.github.eagle10021.realroutes

import android.Manifest
import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** The phone side: where the helper is, the location permission, and whether everything is talking. */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var updateLine: TextView
    private val listener: () -> Unit = { showStatus() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // set up from a computer: adb shell am start -n io.github.eagle10021.realroutes/.MainActivity --es helper_url <url>
        intent?.getStringExtra("helper_url")?.let { HelperClient.setUrl(this, it) }
        val pad = (20 * resources.displayMetrics.density).toInt()
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad * 2, pad, pad); setBackgroundColor(DeviceColors.surface(this@MainActivity)) }
        // the phone's own Material You colours
        val onSurface = DeviceColors.onSurface(this); val variant = DeviceColors.onSurfaceVariant(this); val outline = DeviceColors.outline(this)
        fun text(s: String, size: Float, color: Int = onSurface) = TextView(this).apply { text = s; textSize = size; setTextColor(color); setPadding(0, pad / 3, 0, pad / 3) }
        // M3 Expressive filled buttons: a pill in the phone's primary colour that squeezes on press and springs back
        val primary = DeviceColors.primary(this); val onPrimary = DeviceColors.onPrimary(this)
        fun pill(btn: Button) = btn.apply {
            isAllCaps = false; setTextColor(onPrimary); textSize = 16f; stateListAnimator = null
            background = android.graphics.drawable.GradientDrawable().apply { setColor(primary); cornerRadius = pad * 2f }
            (layoutParams as? LinearLayout.LayoutParams)?.topMargin = pad / 2
            setOnTouchListener { v, e ->
                val bg = v.background as android.graphics.drawable.GradientDrawable
                when (e.actionMasked) {
                    android.view.MotionEvent.ACTION_DOWN -> {
                        v.animate().scaleX(.96f).scaleY(.96f).setInterpolator(M3Spring.SPATIAL_FAST).setDuration(M3Spring.SPATIAL_FAST.durationMs).start()
                        android.animation.ValueAnimator.ofFloat(bg.cornerRadius, pad * .6f).apply {
                            interpolator = M3Spring.SPATIAL_FAST; duration = M3Spring.SPATIAL_FAST.durationMs
                            addUpdateListener { bg.cornerRadius = it.animatedValue as Float }; start() }
                    }
                    android.view.MotionEvent.ACTION_UP, android.view.MotionEvent.ACTION_CANCEL -> {
                        v.animate().scaleX(1f).scaleY(1f).setInterpolator(M3Spring.SPATIAL_FAST).setDuration(M3Spring.SPATIAL_FAST.durationMs).start()
                        android.animation.ValueAnimator.ofFloat(bg.cornerRadius, pad * 2f).apply {
                            interpolator = M3Spring.SPATIAL_FAST; duration = M3Spring.SPATIAL_FAST.durationMs
                            addUpdateListener { bg.cornerRadius = it.animatedValue as Float }; start() }
                    }
                }
                false
            }
        }
        col.addView(text("Real Routes", 30f))
        col.addView(text("Android Auto navigation for BeamNG.Earth: the route, turns, pickups and drop-offs from the Real Routes helper (BeamDeck). Your position comes from BNE Mock GPS when it's running.", 15f, variant))
        col.addView(text("Helper address", 13f, variant))
        val url = EditText(this).apply {
            setText(HelperClient.url(this@MainActivity)); hint = "https://your-laptop.your-tailnet.ts.net:8447"
            inputType = InputType.TYPE_TEXT_VARIATION_URI; setTextColor(onSurface); setHintTextColor(outline)
        }
        col.addView(url)
        col.addView(pill(Button(this)).apply { text = "Save"; setOnClickListener { HelperClient.setUrl(this@MainActivity, url.text.toString()) } })
        col.addView(pill(Button(this)).apply { text = "Allow location"; setOnClickListener {
            requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), 1) } })
        status = text("…", 16f).apply { gravity = Gravity.START }
        col.addView(status)
        updateLine = text("Version ${Updater.installedVersion(this)}", 13f, variant)
        col.addView(updateLine)
        col.addView(pill(Button(this)).apply { text = "Check for updates"; setOnClickListener {
            updateLine.text = "Checking…"
            Updater.check(this@MainActivity, force = true) { updateLine.text = "Version ${Updater.installedVersion(this@MainActivity)} · $it" } } })
        col.addView(text("On the car: Android Auto → Settings → tap Version 10× → ⋮ Developer settings → Unknown sources (on). Then Real Routes shows up in the car's app list.", 13f, outline))
        setContentView(ScrollView(this).apply { addView(col); setBackgroundColor(DeviceColors.surface(this@MainActivity)) })
    }

    override fun onStart() {
        super.onStart(); HelperClient.listeners.add(listener); HelperClient.start(this)
        Updater.check(this) { updateLine.text = "Version ${Updater.installedVersion(this)} · $it" }
    }
    override fun onStop() { super.onStop(); HelperClient.listeners.remove(listener); HelperClient.stop() }

    private fun showStatus() {
        val st = HelperClient.state
        val g = st?.optJSONObject("game")
        status.text = when {
            HelperClient.url(this).isEmpty() -> "Enter the helper's address and tap Save."
            HelperClient.lastError != null && System.currentTimeMillis() - HelperClient.lastOk > 3000 -> "Can't reach the helper: ${HelperClient.lastError}"
            g == null || !g.optBoolean("connected") -> "Helper OK · BeamNG not running"
            !g.optBoolean("ready") -> "Helper OK · waiting for BeamNG.Earth's world"
            else -> "Connected · ${g.optJSONObject("place")?.optString("street") ?: "driving"}" +
                (HelperClient.route?.let { " · route ${"%.1f".format(it.geo.length / 1000)} km, ${it.steps.size} turns" } ?: "")
        }
    }
}
