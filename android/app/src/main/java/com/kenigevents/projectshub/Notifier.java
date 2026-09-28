package com.kenigevents.projectshub;

import android.Manifest;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;

final class Notifier {
    private static final String CHANNEL = "projects_hub";
    private final Context context;
    private final NotificationManager manager;

    Notifier(Context context) {
        this.context = context;
        manager = context.getSystemService(NotificationManager.class);
        manager.createNotificationChannel(new NotificationChannel(
                CHANNEL,
                "Projects Hub",
                NotificationManager.IMPORTANCE_DEFAULT
        ));
    }

    void eventCreated(String title) {
        notify(101, "Событие добавлено", title);
    }

    void updateAvailable(String version) {
        notify(102, "Доступно обновление Projects Hub", version);
    }

    private void notify(int id, String title, String text) {
        if (Build.VERSION.SDK_INT >= 33
                && context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            return;
        }

        Intent open = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pending = PendingIntent.getActivity(
                context,
                id,
                open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        android.app.Notification notification = new android.app.Notification.Builder(context, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(text)
                .setContentIntent(pending)
                .setAutoCancel(true)
                .build();
        manager.notify(id, notification);
    }
}
