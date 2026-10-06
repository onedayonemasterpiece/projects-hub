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
