#!/usr/bin/env python
"""eFlesh tuner: change the magnetometer settings live and watch the effect.

Needs the eflesh_cmd_stream firmware. The settings panel is built from the
firmware's own PARAMS table and the readouts from the values its GET reply
derives, so a setting added to the sketch appears here without touching this
file.

  left    connection, the firmware's settings, derived values (uT/LSB, range,
          conversion time), and per-chip read failures
  right   live X / Y / Z / temperature for one chip or all of them, and a
          per-chip table of mean and noise, in uT and in LSB

Applying a change pauses the stream for a few milliseconds and drops a marker
on the plots, so before and after sit side by side. Noise statistics only use
samples taken after the most recent change.

For a meaningful noise floor leave the sensor untouched and let it settle for
about 30 s after handling it.

Usage
  python visualizer/eflesh_tuner.py                  # choose a port in the window
  python visualizer/eflesh_tuner.py -p /dev/ttyACM0
  python visualizer/eflesh_tuner.py --sim            # simulated firmware, no hardware
"""

import argparse
import os
import sys
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets
from serial.tools import list_ports

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from eflesh.device import DeviceError, EFleshDevice  # noqa: E402
from eflesh.signals import CHIP_NAMES  # noqa: E402

SIM_PORT = "simulator"
PLOTS = (("X", 1, "uT"), ("Y", 2, "uT"), ("Z", 3, "uT"), ("Temperature", 0, "C"))
STATS_COLUMNS = ["chip", "mean x", "mean y", "mean z", "σx", "σy", "σz",
                 "σx LSB", "σy LSB", "σz LSB", "fails/s"]
TICK_MS = 33

# One color per chip position (middle, left, right, top, bottom), so a position
# keeps its color whichever chips are shown; board 1 is drawn dashed. These are
# the first five slots of a categorical palette checked for colour-blind
# separation and >= 3:1 contrast on PLOT_BG.
CHIP_COLORS = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181")
PLOT_BG = "#1a1a19"
PLOT_FG = "#c3c2b7"
LINE_WIDTH = 2
# Antialiased 2 px lines roughly double the draw time, so they are only used
# while few enough curves share a plot to keep the frame rate up.
MAX_SMOOTH_CURVES = 5


def chip_pen(k):
    board, i = divmod(k, len(CHIP_COLORS))
    style = QtCore.Qt.DashLine if board else QtCore.Qt.SolidLine
    return pg.mkPen(CHIP_COLORS[i], width=LINE_WIDTH, style=style)


def chip_icon(k):
    pm = QtGui.QPixmap(22, 10)
    pm.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(chip_pen(k))
    painter.drawLine(1, 5, 21, 5)
    painter.end()
    return QtGui.QIcon(pm)
APPLY_DEBOUNCE_MS = 250


def derived_text(key, value):
    """How to show a value the firmware derived. Unknown keys show as-is."""
    if key.startswith("lsb_"):
        return f"{value:.4g} µT/LSB"
    if key.startswith("range_"):
        return f"± {value:.4g} mT"
    if key == "conv_us" and value:
        return f"{value} µs  (chip limit {1e6 / value:.0f} Hz)"
    return str(value)


class SettingsPanel(QtWidgets.QGroupBox):
    """A form generated from the firmware's PARAMS table."""

    edited = QtCore.Signal()

    def __init__(self):
        super().__init__("Chip settings (from firmware)")
        self._form = QtWidgets.QFormLayout(self)
        self._rows = {}       # name -> (widget, label, Param)
        self._device = {}     # name -> value the device reports

    def build(self, params):
        while self._form.rowCount():
            self._form.removeRow(0)
        self._rows.clear()
        for p in params:
            if (p.lo, p.hi) == (0, 1):
                w = QtWidgets.QCheckBox()
                w.toggled.connect(self._on_edit)
            else:
                w = QtWidgets.QSpinBox()
                w.setRange(p.lo, p.hi)
                w.setSuffix(f"   (default {p.default})")
                w.valueChanged.connect(self._on_edit)
            w.setToolTip(p.desc)
            label = QtWidgets.QLabel(p.name)
            label.setToolTip(p.desc)
            self._form.addRow(label, w)
            self._rows[p.name] = (w, label, p)

    def load(self, state):
        """Show what the device reports and clear any pending edits."""
        self._device = {k: int(state[k]) for k in self._rows if k in state}
        for name, (w, _, _) in self._rows.items():
            w.blockSignals(True)
            v = self._device.get(name, 0)
            w.setChecked(bool(v)) if isinstance(w, QtWidgets.QCheckBox) else w.setValue(v)
            w.blockSignals(False)
        self._restyle()

    def pending(self):
        """Settings edited here but not yet on the device."""
        out = {}
        for name, (w, _, _) in self._rows.items():
            v = int(w.isChecked()) if isinstance(w, QtWidgets.QCheckBox) else w.value()
            if v != self._device.get(name):
                out[name] = v
        return out

    def _on_edit(self, *_):
        self._restyle()
        self.edited.emit()

    def _restyle(self):
        pending = self.pending()
        for name, (_, label, _) in self._rows.items():
            label.setText(f"<b>{name} *</b>" if name in pending else name)


class Tuner(QtWidgets.QMainWindow):
    def __init__(self, port=None, sim=False):
        super().__init__()
        self.setWindowTitle("eFlesh tuner")
        self.resize(1400, 900)
        self.dev = None
        self.sim = None
        self.markers = []        # (host time, [InfiniteLine per plot]) for each applied change
        self.last_change = 0.0
        self.fails = None        # per-chip failed reads per second, from the last check
        self.fw_rate = None      # frames per second the firmware reported at that check
        self._ticks = 0

        self._build_ui()
        self._refresh_ports(select=SIM_PORT if sim else port)

        self._apply_timer = QtCore.QTimer(self, singleShot=True, interval=APPLY_DEBOUNCE_MS)
        self._apply_timer.timeout.connect(self.apply)
        self._tick_timer = QtCore.QTimer(self, interval=TICK_MS)
        self._tick_timer.timeout.connect(self.tick)
        self._tick_timer.start()

        if sim or port:
            QtCore.QTimer.singleShot(0, self.connect_device)

    # ------------------------------------------------------------ layout

    def _build_ui(self):
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)

        conn = QtWidgets.QGroupBox("Connection")
        cg = QtWidgets.QGridLayout(conn)
        self.port_box = QtWidgets.QComboBox()
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(lambda: self._refresh_ports())
        self.connect_btn = QtWidgets.QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.leave_ascii = QtWidgets.QCheckBox("On disconnect, leave the board in ASCII mode")
        self.leave_ascii.setToolTip("Tab-separated text for the Arduino Serial Plotter. "
                                    "Otherwise it is left streaming binary for anyskin.")
        self.info_label = QtWidgets.QLabel("Not connected")
        self.info_label.setWordWrap(True)
        self.info_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        cg.addWidget(self.port_box, 0, 0, 1, 2)
        cg.addWidget(refresh, 0, 2)
        cg.addWidget(self.connect_btn, 1, 0, 1, 3)
        cg.addWidget(self.leave_ascii, 2, 0, 1, 3)
        cg.addWidget(self.info_label, 3, 0, 1, 3)
        lv.addWidget(conn)

        self.settings = SettingsPanel()
        self.settings.edited.connect(self._on_settings_edited)
        lv.addWidget(self.settings)

        buttons = QtWidgets.QHBoxLayout()
        self.auto_apply = QtWidgets.QCheckBox("Apply on change")
        self.auto_apply.setChecked(True)
        apply_btn = QtWidgets.QPushButton("Apply")
        apply_btn.clicked.connect(self.apply)
        revert_btn = QtWidgets.QPushButton("Revert")
        revert_btn.clicked.connect(lambda: self.dev and self.settings.load(self.dev.state))
        defaults_btn = QtWidgets.QPushButton("Defaults")
        defaults_btn.clicked.connect(self.restore_defaults)
        for w in (self.auto_apply, apply_btn, revert_btn, defaults_btn):
            buttons.addWidget(w)
        lv.addLayout(buttons)

        self.error_label = QtWidgets.QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #e06c75;")
        lv.addWidget(self.error_label)

        derived = QtWidgets.QGroupBox("Derived by firmware")
        self.derived_form = QtWidgets.QFormLayout(derived)
        lv.addWidget(derived)

        health = QtWidgets.QGroupBox("Stream health")
        hv = QtWidgets.QVBoxLayout(health)
        self.rate_label = QtWidgets.QLabel("-")
        check = QtWidgets.QPushButton("Check read failures (1 s)")
        check.setToolTip("Counts reads a chip did not answer in time, usually because "
                         "the frame period is shorter than the conversion time.")
        check.clicked.connect(self.check_failures)
        hv.addWidget(self.rate_label)
        hv.addWidget(check)
        lv.addWidget(health)
        lv.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(left.sizeHint().width() + 24)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        controls = QtWidgets.QHBoxLayout()
        self.chip_box = QtWidgets.QComboBox()
        self.chip_box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.chip_box.currentIndexChanged.connect(self._rebuild_curves)
        self.window_s = QtWidgets.QDoubleSpinBox(minimum=0.5, maximum=30.0, value=5.0,
                                                 singleStep=0.5, suffix=" s")
        self.units = QtWidgets.QComboBox()
        self.units.addItems(["µT", "LSB"])
        self.units.currentIndexChanged.connect(self._relabel)
        self.remove_mean = QtWidgets.QCheckBox("Remove mean")
        self.remove_mean.setToolTip("Centre each trace on zero to compare noise across chips.")
        self.pause = QtWidgets.QPushButton("Pause", checkable=True)
        for label, w in (("Chip", self.chip_box), ("Window", self.window_s),
                         ("Units", self.units), (None, self.remove_mean), (None, self.pause)):
            if label:
                controls.addWidget(QtWidgets.QLabel(label))
            controls.addWidget(w)
        controls.addStretch(1)
        rv.addLayout(controls)

        self.graphs = pg.GraphicsLayoutWidget()
        # The legend sits in its own row above the plots so it never covers data.
        self.legend = pg.LegendItem(colCount=5, labelTextColor=PLOT_FG, frame=False)
        self.graphs.addItem(self.legend, row=0, col=0)
        self.plots = []
        for row, (name, _, _) in enumerate(PLOTS, start=1):
            p = self.graphs.addPlot(row=row, col=0)
            p.showGrid(x=True, y=True, alpha=0.15)
            p.setDownsampling(auto=True, mode="peak")
            p.setClipToView(True)
            # Time is set by the Window control; the wheel and drags only act
            # on Y, and the Y range snaps back to the data on the next frame
            # unless the stream is paused.
            p.setMouseEnabled(x=False, y=True)
            p.hideButtons()
            if self.plots:
                p.setXLink(self.plots[0])
            self.plots.append(p)
        self.plots[-1].setLabel("bottom", "time", units="s")
        self._relabel()
        self.curves = [[] for _ in PLOTS]
        rv.addWidget(self.graphs, 3)

        self.table = QtWidgets.QTableWidget(0, len(STATS_COLUMNS))
        self.table.setHorizontalHeaderLabels(STATS_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        rv.addWidget(self.table, 1)

        split = QtWidgets.QSplitter()
        split.addWidget(scroll)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

    def _relabel(self):
        unit = self.units.currentText()
        for p, (name, col, base_unit) in zip(self.plots, PLOTS):
            p.setLabel("left", name, units=unit if col else base_unit)

    def _refresh_ports(self, select=None):
        current = select or self.port_box.currentText()
        self.port_box.clear()
        ports = [p.device for p in sorted(list_ports.comports(), key=lambda p: p.device)]
        self.port_box.addItems(ports + [SIM_PORT])
        if current:
            if self.port_box.findText(current) < 0:
                self.port_box.addItem(current)
            self.port_box.setCurrentText(current)

    # ------------------------------------------------------------ device

    def toggle_connection(self):
        if self.dev:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self):
        port = self.port_box.currentText()
        try:
            if port == SIM_PORT:
                from eflesh.sim import FirmwareSim
                self.sim = FirmwareSim(num_boards=2)
                port = self.sim.port
            dev = EFleshDevice(port)
            dev.open()
            dev.start()
        except Exception as e:  # noqa: BLE001 -- show any connect failure to the user
            self._error(f"Connect failed: {e}")
            if self.sim:
                self.sim.close()
                self.sim = None
            return

        self.dev = dev
        self._error("")
        self.connect_btn.setText("Disconnect")
        self.port_box.setEnabled(False)
        info = dev.info
        boards = "<br>".join(
            f"{b.name} {b.where}: " + " ".join(f"0x{a:02X}" for a in b.addrs)
            for b in info.boards)
        self.info_label.setText(f"{info.fw} · mux {info.mux} · {info.mags} magnetometers"
                                f"<br>{boards}")
        self.settings.build(dev.tunable())
        self.settings.load(dev.state)
        self._show_derived()

        self.chip_box.blockSignals(True)
        self.chip_box.clear()
        self.chip_box.addItem("All chips")
        for k in range(info.mags):
            self.chip_box.addItem(chip_icon(k), self._chip_name(k))
        self.chip_box.blockSignals(False)
        self.fails = None
        self.fw_rate = None
        self.last_change = time.time()
        self.table.setRowCount(info.mags)
        for k in range(info.mags):
            self.table.setItem(k, 0, QtWidgets.QTableWidgetItem(chip_icon(k), self._chip_name(k)))
        self._rebuild_curves()

    def disconnect_device(self):
        if self.dev:
            try:
                self.dev.close(leave_ascii=self.leave_ascii.isChecked())
            except Exception as e:  # noqa: BLE001
                self._error(f"Close failed: {e}")
            self.dev = None
        if self.sim:
            self.sim.close()
            self.sim = None
        self.connect_btn.setText("Connect")
        self.port_box.setEnabled(True)
        self.info_label.setText("Not connected")
        self.rate_label.setText("-")

    def _chip_name(self, k):
        board, i = divmod(k, 5)
        b = self.dev.info.boards[board]
        return f"b{board} {CHIP_NAMES[i]} 0x{b.addrs[i]:02X}"

    def _on_settings_edited(self):
        if self.auto_apply.isChecked():
            self._apply_timer.start()

    def apply(self):
        if not self.dev:
            return
        pending = self.settings.pending()
        if not pending:
            return
        try:
            self.dev.set(**pending)
            self._error("")
            self._mark_change()
        except DeviceError as e:
            self._error(str(e))
        self.settings.load(self.dev.state)
        self._show_derived()

    def restore_defaults(self):
        if not self.dev:
            return
        try:
            self.dev.defaults()
            self._error("")
            self._mark_change()
        except DeviceError as e:
            self._error(str(e))
        self.settings.load(self.dev.state)
        self._show_derived()

    def check_failures(self):
        if not self.dev:
            return
        try:
            self.dev.stats()                        # resets the firmware's counters
        except DeviceError as e:
            self._error(str(e))
            return
        QtCore.QTimer.singleShot(1000, self._read_failures)

    def _read_failures(self):
        if not self.dev:
            return
        try:
            s = self.dev.stats()
        except DeviceError as e:
            self._error(str(e))
            return
        seconds = max(s.get("ms", 1000), 1) / 1000
        self.fails = np.array(s["fails"], dtype=float) / seconds
        self.fw_rate = s.get("frames", 0) / seconds

    def _show_derived(self):
        while self.derived_form.rowCount():
            self.derived_form.removeRow(0)
        for key, value in self.dev.derived().items():
            self.derived_form.addRow(key, QtWidgets.QLabel(derived_text(key, value)))

    def _mark_change(self):
        now = time.time()
        self.last_change = now
        lines = []
        for p in self.plots:
            line = pg.InfiniteLine(angle=90, pen=pg.mkPen((240, 228, 66), style=QtCore.Qt.DashLine))
            p.addItem(line)
            lines.append(line)
        self.markers.append((now, lines))

    def _error(self, text):
        self.error_label.setText(text)

    # ------------------------------------------------------------ plotting

    def _selected_chips(self):
        if not self.dev:
            return []
        idx = self.chip_box.currentIndex()
        return list(range(self.dev.info.mags)) if idx <= 0 else [idx - 1]

    def _rebuild_curves(self):
        for p, curves in zip(self.plots, self.curves):
            for c in curves:
                p.removeItem(c)
            curves.clear()
        self.legend.clear()
        chips = self._selected_chips()
        smooth = len(chips) <= MAX_SMOOTH_CURVES
        for p, curves in zip(self.plots, self.curves):
            for k in chips:
                curves.append(p.plot(pen=chip_pen(k), antialias=smooth))
        if len(chips) > 1:
            for k, curve in zip(chips, self.curves[0]):
                self.legend.addItem(curve, self._chip_name(k))
        self.legend.setVisible(len(chips) > 1)

    def tick(self):
        if not self.dev or self.pause.isChecked():
            return
        t, d = self.dev.snapshot()
        if len(t) < 2:
            return
        now = t[-1]
        keep = t >= now - self.window_s.value()
        t, d = t[keep] - now, d[keep].astype(float)

        lsb = np.array([self.dev.state.get(f"lsb_{a}", 1.0) for a in "xyz"])
        if self.units.currentText() == "LSB":
            d[:, :, 1:] /= lsb

        chips = self._selected_chips()
        for (_, col, _), curves in zip(PLOTS, self.curves):
            for curve, k in zip(curves, chips):
                y = d[:, k, col]
                if self.remove_mean.isChecked():
                    y = y - y.mean()
                curve.setData(t, y)
        for p in self.plots:
            p.setXRange(-self.window_s.value(), 0, padding=0)
            p.enableAutoRange(y=True)

        for when, lines in list(self.markers):
            x = when - now
            if x < -30:
                for p, line in zip(self.plots, lines):
                    p.removeItem(line)
                self.markers.remove((when, lines))
            else:
                for line in lines:
                    line.setValue(x)

        self._ticks += 1
        if self._ticks % 6 == 0:
            self._update_stats(t + now, d, lsb)

    def _update_stats(self, t_abs, d, lsb):
        recent = t_abs >= self.last_change
        if recent.sum() < 2:
            return
        d = d[recent]
        span = t_abs[recent][-1] - t_abs[recent][0]
        rate = (recent.sum() - 1) / span if span > 0 else 0.0
        text = f"{rate:.0f} frames/s received · {self.dev.resyncs} resyncs"
        if self.fw_rate is not None:
            text += f"\nlast check: firmware sent {self.fw_rate:.0f} frames/s"
        self.rate_label.setText(text)

        in_lsb = self.units.currentText() == "LSB"
        xyz = d[:, :, 1:]
        mean = xyz.mean(axis=0)
        std = xyz.std(axis=0)
        std_ut = std * lsb if in_lsb else std
        mean_ut = mean * lsb if in_lsb else mean
        for k in range(d.shape[1]):
            cells = [*mean_ut[k], *std_ut[k], *(std_ut[k] / lsb)]
            for c, v in enumerate(cells, start=1):
                text = f"{v:.1f}" if c <= 3 else f"{v:.2f}"
                self.table.setItem(k, c, QtWidgets.QTableWidgetItem(text))
            fail = "-" if self.fails is None else f"{self.fails[k]:.1f}"
            self.table.setItem(k, len(STATS_COLUMNS) - 1, QtWidgets.QTableWidgetItem(fail))

    def closeEvent(self, event: QtGui.QCloseEvent):
        self.disconnect_device()
        super().closeEvent(event)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--port", help="serial port to connect to at startup")
    p.add_argument("--sim", action="store_true", help="use the simulated firmware")
    args = p.parse_args()

    # Qt's Wayland backend aborts on recent GNOME, which dropped the xsettings
    # "antialiasing" key it reads. X11 through XWayland works; an explicit
    # QT_QPA_PLATFORM still wins.
    if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(background=PLOT_BG, foreground=PLOT_FG)
    win = Tuner(port=args.port, sim=args.sim)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
