package io.github.eagle10021.realroutes

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * Real Routes announcements and directions spoken by the phone, when the helper says speech goes to the phone
 * ("Speak on: Android Auto"). Spoken as navigation guidance, so Android Auto plays it through the car and other
 * sound dips under it. Riders each get one of the phone's other English voices and their own pitch.
 */
object Speaker {
    private var tts: TextToSpeech? = null
    private var ready = false
    private var lastId: Long = -1
    private var voices: List<Voice> = emptyList()
    private var am: AudioManager? = null
    private var focus: AudioFocusRequest? = null
    private val attrs = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build()

    fun start(ctx: Context) {
        if (tts != null) return
        val app = ctx.applicationContext
        am = app.getSystemService(AudioManager::class.java)
        tts = TextToSpeech(app) { status ->
            ready = status == TextToSpeech.SUCCESS
            val t = tts ?: return@TextToSpeech
            if (!ready) return@TextToSpeech
            t.setAudioAttributes(attrs)
            voices = (t.voices ?: emptySet()).filter { it.locale.language == "en" && !it.isNetworkConnectionRequired }
                .sortedBy { it.name }
            t.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(id: String?) {}
                override fun onDone(id: String?) { if (!t.isSpeaking) release() }
                @Deprecated("Deprecated in Java")
                override fun onError(id: String?) { release() }
            })
        }
    }

    private fun release() { focus?.let { am?.abandonAudioFocusRequest(it) }; focus = null }

    /** New entries in the helper's announcement list ("pa"); only those meant for the phone are spoken. */
    fun onState(pa: JSONArray?, prefs: JSONObject?) {
        if (pa == null) return
        val top = if (pa.length() > 0) pa.optJSONObject(pa.length() - 1)?.optLong("id") ?: 0 else 0
        if (lastId < 0 || top < lastId) { lastId = top; return }       // first look: say nothing old
        for (i in 0 until pa.length()) {
            val a = pa.optJSONObject(i) ?: continue
            val id = a.optLong("id")
            if (id <= lastId) continue
            lastId = id
            if (a.optString("to") != "aa" || a.optBoolean("quiet")) continue
            if (System.currentTimeMillis() / 1000.0 - a.optDouble("t") > 20) continue
            say(a, prefs?.optDouble("speechVol", 0.9) ?: 0.9)
        }
    }

    private fun say(a: JSONObject, vol: Double) {
        val t = tts ?: return
        if (!ready) return
        val rider = a.optString("kind") == "rider"
        if (rider && voices.size > 1) {
            val h = (a.optString("who").hashCode() and 0x7fffffff)
            t.voice = voices[h % voices.size]
            t.setPitch(0.8f + (h % 50) / 100f)
        } else {
            t.voice = t.defaultVoice ?: t.voice
            t.language = Locale.CANADA
            t.setPitch(1f)
        }
        if (focus == null) {
            focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK).setAudioAttributes(attrs).build()
            focus?.let { am?.requestAudioFocus(it) }
        }
        val rate = a.optDouble("rate", 0.85).coerceIn(0.5, 1.5)             // the speaking speed set on the tablet
        t.setSpeechRate(rate.toFloat())
        val params = Bundle().apply { putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, vol.coerceIn(0.0, 1.0).toFloat()) }
        t.speak(a.optString("say", a.optString("text")), TextToSpeech.QUEUE_ADD, params, "rr" + a.optLong("id"))
    }
}
