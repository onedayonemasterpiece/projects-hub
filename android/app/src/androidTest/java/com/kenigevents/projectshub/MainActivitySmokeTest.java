package com.kenigevents.projectshub;

import static org.junit.Assert.assertNotNull;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class MainActivitySmokeTest {
    @Test
    public void activityLaunches() {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            scenario.onActivity(activity -> assertNotNull(activity));
        }
    }
}
