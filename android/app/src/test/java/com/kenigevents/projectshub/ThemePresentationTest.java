package com.kenigevents.projectshub;
import org.junit.Test;
import static org.junit.Assert.*;
public class ThemePresentationTest {
    @Test public void orderingAndActorReset() {
        ThemePresentation state = new ThemePresentation();
        assertTrue(state.admit("light", 1, false));
        assertTrue(state.admit("light", 1, false));
        assertFalse(state.admit("dark", 1, false));
        assertFalse(state.admit("dark", 0, false));
        assertTrue(state.admit("dark", 2, false));
        assertFalse(state.admit("system", 3, false));
        assertFalse(state.admit("light", -1, false));
        assertFalse(state.admit("light", 0, true));
        assertTrue(state.admit("dark", 0, true));
        assertTrue(state.admit("light", 1, false));
    }
}
