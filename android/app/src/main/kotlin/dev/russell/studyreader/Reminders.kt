package dev.russell.studyreader

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.app.Notification
import java.util.Calendar

/** The daily reminder: a notification at a chosen time when cards are waiting. */
object Reminders {
    private const val CHANNEL = "reminders"
    private const val NOTIFICATION = 1

    fun enabled(context: Context) = Store.get(context).prefs.getBoolean("reminder", false)
    fun minuteOfDay(context: Context) = Store.get(context).prefs.getInt("reminder_minute", 19 * 60)

    fun configure(context: Context, on: Boolean, minuteOfDay: Int) {
        Store.get(context).prefs.edit().putBoolean("reminder", on).putInt("reminder_minute", minuteOfDay).apply()
        schedule(context)
    }

    private fun intent(context: Context) = PendingIntent.getBroadcast(
        context, 0, Intent(context, ReminderReceiver::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    /** Sets (or clears) the alarm for the next reminder. */
    fun schedule(context: Context) {
        val alarms = context.getSystemService(AlarmManager::class.java)
        alarms.cancel(intent(context))
        if (!enabled(context)) return
        val minute = minuteOfDay(context)
        val at = Calendar.getInstance().apply {
            set(Calendar.HOUR_OF_DAY, minute / 60); set(Calendar.MINUTE, minute % 60)
            set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
            if (timeInMillis <= System.currentTimeMillis() + 1000) add(Calendar.DAY_OF_YEAR, 1)
        }
        // Within 10 minutes of the time is fine, and needs no exact-alarm permission.
        alarms.setWindow(AlarmManager.RTC_WAKEUP, at.timeInMillis, 10 * 60_000L, intent(context))
    }

    fun notify(context: Context, waiting: Int) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Review reminders", NotificationManager.IMPORTANCE_DEFAULT))
        val open = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("$waiting card${if (waiting == 1) "" else "s"} to review")
            .setContentText("A few minutes now keeps them fresh.")
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        try { manager.notify(NOTIFICATION, n) } catch (_: SecurityException) { /* notifications not allowed */ }
    }
}

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val pending = goAsync()
        val store = Store.get(context)
        store.worker.execute {
            try {
                store.sync()
                val waiting = store.waiting()
                if (waiting > 0) Reminders.notify(context, waiting)
            } catch (_: Exception) {
            } finally {
                Reminders.schedule(context)
                pending.finish()
            }
        }
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action == Intent.ACTION_BOOT_COMPLETED) Reminders.schedule(context)
    }
}
