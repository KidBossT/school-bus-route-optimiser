package com.kidbosst.busgps;

import android.Manifest;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.Build;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onResume() {
        super.onResume();
        askForNotificationsOnce();
    }

    /**
     * Android 13+: the location service's "recording this run" notification is
     * only shown once notifications are allowed. Tracking works either way; the
     * notification shows the didi it is running. Asked once, and only after
     * location has been granted, so the two permission dialogs never collide
     * (the page asks for location as soon as it opens).
     */
    private void askForNotificationsOnce() {
        if (Build.VERSION.SDK_INT < 33) return;
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) return;
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED) return;
        SharedPreferences prefs = getSharedPreferences("bus_gps", MODE_PRIVATE);
        if (prefs.getBoolean("asked_notifications", false)) return;
        prefs.edit().putBoolean("asked_notifications", true).apply();
        ActivityCompat.requestPermissions(this, new String[] { Manifest.permission.POST_NOTIFICATIONS }, 7);
    }
}
