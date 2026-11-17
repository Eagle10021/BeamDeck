package io.github.eagle10021.realroutes

import android.content.Intent
import androidx.car.app.AppManager
import androidx.car.app.CarAppService
import androidx.car.app.Screen
import androidx.car.app.Session
import androidx.car.app.validation.HostValidator

/** Real Routes on Android Auto: a navigation app (its own map) fed by the Real Routes helper. */
class RRCarAppService : CarAppService() {
    // a personal, side-loaded app (Android Auto's developer "Unknown sources"): any host may bind
    override fun createHostValidator(): HostValidator = HostValidator.ALLOW_ALL_HOSTS_VALIDATOR

    override fun onCreateSession(): Session = object : Session() {
        override fun onCreateScreen(intent: Intent): Screen {
            val renderer = MapRenderer(carContext)
            carContext.getCarService(AppManager::class.java).setSurfaceCallback(renderer)
            return NavScreen(carContext, renderer)
        }
    }
}
