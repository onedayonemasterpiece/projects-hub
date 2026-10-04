package com.kenigevents.projectshub;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.CalendarContract;

import org.json.JSONArray;
import org.json.JSONObject;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;

final class CalendarExecutor {
    private final Activity activity;

    CalendarExecutor(Activity activity) {
        this.activity = activity;
    }

    boolean hasReadPermission() {
        return activity.checkSelfPermission(Manifest.permission.READ_CALENDAR)
                == PackageManager.PERMISSION_GRANTED;
    }

    boolean hasWritePermissions() {
        return hasReadPermission()
                && activity.checkSelfPermission(Manifest.permission.WRITE_CALENDAR)
                == PackageManager.PERMISSION_GRANTED;
    }

    boolean hasPermissions() {
        return hasWritePermissions();
    }

    JSONObject readEvents(JSONObject payload) throws Exception {
        if (!hasReadPermission()) throw new SecurityException("Calendar read permission is required");

        long startMs = OffsetDateTime.parse(payload.getString("starts_at")).toInstant().toEpochMilli();
        long endMs = OffsetDateTime.parse(payload.getString("ends_at")).toInstant().toEpochMilli();
        int limit = Math.max(1, Math.min(20, payload.optInt("limit", 20)));

        Uri.Builder builder = CalendarContract.Instances.CONTENT_URI.buildUpon();
        ContentUris.appendId(builder, startMs);
        ContentUris.appendId(builder, endMs);

        String[] projection = {
                CalendarContract.Instances.EVENT_ID,
                CalendarContract.Instances.TITLE,
                CalendarContract.Instances.BEGIN,
                CalendarContract.Instances.END,
                CalendarContract.Instances.ALL_DAY,
                CalendarContract.Instances.EVENT_LOCATION
        };

        JSONArray events = new JSONArray();
        try (Cursor cursor = activity.getContentResolver().query(
                builder.build(),
                projection,
                null,
                null,
                CalendarContract.Instances.BEGIN + " ASC"
        )) {
            if (cursor != null) {
                ZoneId zone = ZoneId.systemDefault();
                while (cursor.moveToNext() && events.length() < limit) {
                    long begin = cursor.getLong(2);
                    long end = cursor.getLong(3);
                    String title = cursor.isNull(1) ? "" : cursor.getString(1);
                    String location = cursor.isNull(5) ? "" : cursor.getString(5);
                    events.put(new JSONObject()
                            .put("event_id", Long.toString(cursor.getLong(0)))
                            .put("title", bounded(title, 240))
                            .put("starts_at", Instant.ofEpochMilli(begin).atZone(zone).toOffsetDateTime().toString())
                            .put("ends_at", Instant.ofEpochMilli(end).atZone(zone).toOffsetDateTime().toString())
                            .put("all_day", cursor.getInt(4) != 0)
                            .put("location", bounded(location, 320)));
                }
            }
        }

        return new JSONObject()
                .put("readback_verified", true)
                .put("provider_status", "present")
                .put("count", events.length())
                .put("events", events);
    }

    private static String bounded(String value, int max) {
        if (value == null) return "";
        String clean = value.trim();
        return clean.length() <= max ? clean : clean.substring(0, max);
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
