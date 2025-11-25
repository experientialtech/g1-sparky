#!/usr/bin/env python3
"""Desktop power monitor widget for Jetson Thor on G1 battery"""
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk, Pango
import subprocess

class PowerWidget(Gtk.Window):
    def __init__(self):
        super().__init__(title="Power")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_default_size(220, 120)
        self.move(10, 10)

        # Dark background
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window { background-color: rgba(20, 20, 20, 0.85); border-radius: 10px; }
            label { color: white; }
            .title { font-size: 11px; font-weight: bold; color: #aaa; }
            .value { font-size: 14px; font-weight: bold; }
            .gpu { color: #4fc3f7; }
            .cpu { color: #81c784; }
            .sys { color: #ffb74d; }
            .total { color: #fff; font-size: 18px; font-weight: bold; }
            .warn { color: #ff5252; }
            .bar-bg { background-color: #333; border-radius: 3px; min-height: 6px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Title
        title = Gtk.Label(label="POWER MONITOR")
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)

        # Total power with bar
        self.total_label = Gtk.Label(label="-- W")
        self.total_label.get_style_context().add_class("total")
        box.pack_start(self.total_label, False, False, 0)

        self.bar = Gtk.LevelBar()
        self.bar.set_min_value(0)
        self.bar.set_max_value(120)
        self.bar.set_value(0)
        self.bar.set_size_request(-1, 8)
        box.pack_start(self.bar, False, False, 0)

        # Grid for details
        grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        self.gpu_label = Gtk.Label(label="GPU:  --W")
        self.gpu_label.get_style_context().add_class("gpu")
        self.gpu_label.set_xalign(0)
        self.cpu_label = Gtk.Label(label="CPU:  --W")
        self.cpu_label.get_style_context().add_class("cpu")
        self.cpu_label.set_xalign(0)
        self.sys_label = Gtk.Label(label="5V:   --W")
        self.sys_label.get_style_context().add_class("sys")
        self.sys_label.set_xalign(0)
        grid.attach(self.gpu_label, 0, 0, 1, 1)
        grid.attach(self.cpu_label, 1, 0, 1, 1)
        grid.attach(self.sys_label, 0, 1, 1, 1)
        box.pack_start(grid, False, False, 0)

        self.add(box)
        self.show_all()

        # Enable drag
        self.drag_x = 0
        self.drag_y = 0
        self.connect("button-press-event", self.on_press)
        self.connect("motion-notify-event", self.on_drag)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK)

        GLib.timeout_add(1000, self.update)

    def on_press(self, widget, event):
        self.drag_x = event.x
        self.drag_y = event.y

    def on_drag(self, widget, event):
        if event.state & Gdk.ModifierType.BUTTON1_MASK:
            x, y = self.get_position()
            self.move(x + int(event.x - self.drag_x), y + int(event.y - self.drag_y))

    def read_sensor(self, path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except:
            return 0

    def update(self):
        gv = self.read_sensor("/sys/class/hwmon/hwmon5/in1_input")
        gc = self.read_sensor("/sys/class/hwmon/hwmon5/curr1_input")
        gw = gv * gc / 1000000

        cv = self.read_sensor("/sys/class/hwmon/hwmon5/in2_input")
        cc = self.read_sensor("/sys/class/hwmon/hwmon5/curr2_input")
        cw = cv * cc / 1000000

        sv = self.read_sensor("/sys/class/hwmon/hwmon5/in3_input")
        sc = self.read_sensor("/sys/class/hwmon/hwmon5/curr3_input")
        sw = sv * sc / 1000000

        tp = self.read_sensor("/sys/class/hwmon/hwmon6/power1_input")
        tw = tp / 1000000

        self.gpu_label.set_text(f"GPU: {gw:.1f}W")
        self.cpu_label.set_text(f"CPU: {cw:.1f}W")
        self.sys_label.set_text(f"5V:  {sw:.1f}W")
        self.total_label.set_text(f"{tw:.1f} W")

        self.bar.set_value(min(tw, 120))

        ctx = self.total_label.get_style_context()
        if tw > 80:
            ctx.add_class("warn")
        else:
            ctx.remove_class("warn")

        return True

if __name__ == "__main__":
    win = PowerWidget()
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
