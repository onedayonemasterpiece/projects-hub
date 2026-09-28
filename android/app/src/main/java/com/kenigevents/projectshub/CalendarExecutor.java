package com.kenigevents.projectshub;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;

import org.json.JSONObject;

import java.time.OffsetDateTime;

final class CalendarExecutor {
    private final Activity activity;

    CalendarExecutor(Activity activity) {
        this.activity = activity;
    }

    boolean hasPermissions() {
        return activity.checkSelfPermission(Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED
                && activity.checkSelfPermission(Manifest.permission.WRITE_CALENDAR) == PackageManager.PERMISSION_GRANTED;
    }

    JSONObject createEvent(JSONObject payload) throws Exception {
        if (!hasPermissions()) throw new SecurityException("Calendar permission is required");

        long calendarId = findWritableCalendar();
        if (calendarId < 0) {
            throw new IllegalStateException("No writable calendar is available on this device");
        }

        long startMs = OffsetDateTime.parse(payload.getString("starts_at")).toInstant().toEpochMilli();
        long endMs = OffsetDateTime.parse(payload.getString("ends_at")).toInstant().toEpochMilli();
        String title = payload.getString("title");
        String timezone = payload.getString("timezone");

        ContentValues values = new ContentValues();
        values.put(CalendarContract.Events.CALENDAR_ID, calendarId);
        values.put(CalendarContract.Events.TITLE, title);
        values.put(CalendarContract.Events.DTSTART, startMs);
        values.put(CalendarContract.Events.DTEND, endMs);
        values.put(CalendarContract.Events.EVENT_TIMEZONE, timezone);
        values.put(CalendarContract.Events.DESCRIPTION, payload.optString("description", ""));
        values.put(CalendarContract.Events.EVENT_LOCATION, payload.optString("location", ""));

        ContentResolver resolver = activity.getContentResolver();
        Uri inserted = resolver.insert(CalendarContract.Events.CONTENT_URI, values);
        if (inserted == null) throw new IllegalStateException("Calendar provider rejected the event");

        String eventId = inserted.getLastPathSegment();
        boolean verified = verifyReadback(inserted, title, startMs, endMs);
        if (!verified) {
            throw new IllegalStateException("Calendar event was inserted but readback did not match");
        }

        return new JSONObject()
                .put("readback_verified", true)
                .put("event_id", eventId == null ? inserted.toString() : eventId)
                .put("provider_status", "present")
                .put("calendar_id", calendarId);
    }

    private long findWritableCalendar() {
        String[] projection = {
                CalendarContract.Calendars._ID,
                CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL,
                CalendarContract.Calendars.VISIBLE,
                CalendarContract.Calendars.IS_PRIMARY
        };
        String selection = CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL + ">=? AND "
                + CalendarContract.Calendars.VISIBLE + "=1";
        String[] args = {
                Integer.toString(CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR)
        };
        String order = CalendarContract.Calendars.IS_PRIMARY + " DESC, "
                + CalendarContract.Calendars._ID + " ASC";

        try (Cursor cursor = activity.getContentResolver().query(
                CalendarContract.Calendars.CONTENT_URI,
                projection,
                selection,
                args,
                order
        )) {
            if (cursor != null && cursor.moveToFirst()) {
                return cursor.getLong(0);
            }
        }
        return -1L;
    }

    private boolean verifyReadback(Uri uri, String title, long startMs, long endMs) {
        String[] projection = {
                CalendarContract.Events.TITLE,
                CalendarContract.Events.DTSTART,
                CalendarContract.Events.DTEND
        };
        try (Cursor cursor = activity.getContentResolver().query(
                uri,
                projection,
                null,
                null,
                null
        )) {
            if (cursor == null || !cursor.moveToFirst()) return false;
            return title.equals(cursor.getString(0))
                    && startMs == cursor.getLong(1)
                    && endMs == cursor.getLong(2);
        }
    }
}
