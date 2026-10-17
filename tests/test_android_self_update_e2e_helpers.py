from scripts.android_self_update_e2e import unknown_sources_control


def test_unknown_sources_control_prefers_switch_widget():
    xml = """<hierarchy>
      <node text="Projects Hub" bounds="[10,10][400,80]" />
      <node text="Allow from this source" bounds="[20,120][500,220]" />
      <node resource-id="android:id/switch_widget"
            class="android.widget.Switch"
            checkable="true"
            checked="false"
            bounds="[800,120][1040,240]" />
    </hierarchy>"""
    assert unknown_sources_control(xml) == ((800, 120, 1040, 240), False)


def test_unknown_sources_control_reports_checked_switch():
    xml = """<hierarchy>
      <node resource-id="com.android.settings:id/switch_widget"
            class="android.widget.Switch"
            checkable="true"
            checked="true"
            bounds="[780,120][1030,230]" />
    </hierarchy>"""
    assert unknown_sources_control(xml) == ((780, 120, 1030, 230), True)


def test_unknown_sources_control_falls_back_to_settings_row():
    xml = """<hierarchy>
      <node text="Allow from this source"
            class="android.widget.TextView"
            bounds="[40,140][760,240]" />
    </hierarchy>"""
    assert unknown_sources_control(xml) == ((40, 140, 760, 240), None)


ANR = """<hierarchy><node resource-id="android:id/alertTitle" package="android"
 text="Pixel Launcher isn't responding"/><node resource-id="android:id/aerr_close" package="android"
 enabled="true" clickable="true" bounds="[70,1174][1010,1300]"/></hierarchy>"""
SETTINGS = """<hierarchy><node resource-id="com.android.settings:id/switch_widget"
 class="android.widget.Switch" checkable="true" checked="false" bounds="[800,120][1040,240]"/></hierarchy>"""


def test_launcher_anr_recognition_does_not_hide_product_failure():
    from scripts.android_self_update_e2e import launcher_anr_close_bounds
    assert launcher_anr_close_bounds(ANR) == (70,1174,1010,1300)
    assert launcher_anr_close_bounds(ANR.replace("Pixel Launcher", "Projects Hub")) is None
    assert launcher_anr_close_bounds(ANR.replace("Pixel Launcher", "Settings")) is None
    assert launcher_anr_close_bounds(ANR.replace('package="android"', 'package="unknown"')) is None


def test_launcher_close_then_real_settings_toggle_and_readonly_appop(monkeypatch):
    from scripts import android_self_update_e2e as helper
    windows = iter([ANR, SETTINGS, SETTINGS])
    appops = iter(["default mode: default", "REQUEST_INSTALL_PACKAGES: allow"])
    calls=[]
    def run(*args,**kwargs):
        calls.append(args)
        if args[:3] == ("adb", "exec-out", "cat"):
            return next(windows)
        if "appops" in args:
            assert args[3] == "get"
            return next(appops)
        return ""
    monkeypatch.setattr(helper,"run",run)
    monkeypatch.setattr(helper.time,"sleep",lambda _:None)
    helper.enable_unknown_sources_via_settings()
    taps=[call for call in calls if call[:4] == ("adb","shell","input","tap")]
    assert taps == [("adb","shell","input","tap","540","1237"),
                    ("adb","shell","input","tap","920","180")]
    assert not any("set" in call or "grant" in call or "install" in call for call in calls)


def test_launcher_anr_recurrence_stops_without_blind_retry(monkeypatch):
    import pytest
    from scripts import android_self_update_e2e as helper
    calls=[]
    def run(*args,**kwargs):
        calls.append(args)
        return ANR if args[:3] == ("adb","exec-out","cat") else ""
    monkeypatch.setattr(helper,"run",run)
    monkeypatch.setattr(helper.time,"sleep",lambda _:None)
    with pytest.raises(RuntimeError,match="ANR recurred"):
        helper.enable_unknown_sources_via_settings()
    assert len([call for call in calls if "tap" in call]) == 1
