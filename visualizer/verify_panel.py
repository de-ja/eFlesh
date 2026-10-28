"""The tuner's Verify tab: a guided check of each board, one step at a time.

The checks live in eflesh/verify.py; this file walks the user through them,
feeds them live frames and shows where each chip sits on the pad.
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from eflesh import verify as V
from eflesh.signals import CHIP_NAMES, CHIP_POSITIONS_MM, MAGS_PER_FINGER, to_pad_frame
from tuner_style import CHIP_COLORS, PLOT_BG, PLOT_FG

REST_S = 3.0
REST_MIN_FRAMES = 20        # the rest step runs longer at slow settings to collect these
REST_MAX_S = 20.0
RESTS = ("bare", "cuboid")
TICK_MS = 50
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                          "verify_reports")

# Status is shown as a mark and a word as well as a color, never color alone.
MARKS = {V.PASS: "✓", V.WARN: "!", V.FAIL: "✗", V.INFO: "i", V.SKIP: "–",
         V.INCOMPLETE: "…", "pending": "·", "running": "▶"}
COLORS = {V.PASS: "#3fb950", V.WARN: "#d29922", V.FAIL: "#f85149", V.INFO: PLOT_FG,
          V.SKIP: "#8b949e", V.INCOMPLETE: "#8b949e", "pending": "#8b949e",
          "running": "#58a6ff"}


@dataclass
class Step:
    key: str
    title: str
    kind: str                       # bare, mag, axis, cuboid, flat
    board: Optional[int] = None
    position: Optional[int] = None
    side: str = ""                  # axis steps: right, top
    status: str = "pending"
    manual: bool = False            # needs a setup change first, so never auto-started


def worst(checks):
    statuses = {c.status for c in checks}
    for s in (V.FAIL, V.WARN, V.PASS):
        if s in statuses:
            return s
    if statuses == {V.INFO}:
        return V.INFO
    return V.SKIP if statuses == {V.SKIP} else V.PASS


class PadDiagram(pg.PlotWidget):
    """One board seen from above in the pad frame: a dot per chip that grows
    with its response, a ring on the chip to press, and in-plane arrows."""

    RANGE_MM = 19

    def __init__(self):
        super().__init__(background=PLOT_BG)
        self.setAspectLocked(True)
        self.hideAxis("left")
        self.hideAxis("bottom")
        self.setMouseEnabled(x=False, y=False)
        self.hideButtons()
        self.setRange(xRange=(-self.RANGE_MM, self.RANGE_MM),
                      yRange=(-self.RANGE_MM, self.RANGE_MM), padding=0)
        self.setMinimumSize(320, 320)
        outline = pg.QtWidgets.QGraphicsRectItem(-12, -12, 24, 24)
        outline.setPen(pg.mkPen(PLOT_FG, width=1, style=QtCore.Qt.DotLine))
        self.addItem(outline)
        self.dots = pg.ScatterPlotItem(pxMode=True)
        self.ring = pg.ScatterPlotItem(pxMode=True, brush=None, pen=pg.mkPen("w", width=2))
        self.arrows = pg.PlotDataItem(connect="pairs", pen=pg.mkPen("w", width=2))
        self.magnet = pg.ScatterPlotItem(pxMode=True, symbol="s", size=18,
                                         brush=pg.mkBrush("#8b949e"), pen=None)
        for item in (self.ring, self.dots, self.arrows, self.magnet):
            self.addItem(item)
        # Names sit outward from the pad centre, clear of the dots and arrows.
        anchors = {"middle": (-0.15, 1.6), "left": (1.25, 0.5), "right": (-0.25, 0.5),
                   "top": (0.5, 1.6), "bottom": (0.5, -0.6)}
        for (x, y), name in zip(CHIP_POSITIONS_MM, CHIP_NAMES):
            label = pg.TextItem(name, color=PLOT_FG, anchor=anchors[name])
            label.setPos(x, y)
            self.addItem(label)
        self.show_state(np.zeros(MAGS_PER_FINGER), None, None, "")

    def show_state(self, level, target, vectors, side):
        """level: (5,) 0..1 response; target: position to ring or None;
        vectors: (5, 2) pad-frame unit-ish arrows or None; side: magnet marker."""
        sizes = 14 + 34 * np.clip(level, 0, 1)
        self.dots.setData(pos=CHIP_POSITIONS_MM, size=sizes,
                          brush=[pg.mkBrush(c) for c in CHIP_COLORS], pen=None)
        if target is None:
            self.ring.setData([])
        else:
            self.ring.setData(pos=CHIP_POSITIONS_MM[[target]], size=[54])
        if vectors is None:
            self.arrows.setData([], [])
        else:
            ends = CHIP_POSITIONS_MM + 4.5 * vectors
            pts = np.empty((2 * MAGS_PER_FINGER, 2))
            pts[0::2], pts[1::2] = CHIP_POSITIONS_MM, ends
            self.arrows.setData(pts[:, 0], pts[:, 1])
        spot = {"right": (16, 0), "top": (0, 16)}.get(side)
        self.magnet.setData(pos=[spot] if spot else [])


class VerifyPanel(QtWidgets.QWidget):
    def __init__(self, get_device):
        super().__init__()
        self._dev = get_device
        self.steps = []
        self.index = 0
        self.running = False
        self.capture = None
        self.report = V.Report()
        self._rest = []                  # (t, frame) during the rest step
        self._t0 = 0.0
        self._last_t = 0.0
        self._axis_right = {}            # board -> mean dB with the magnet on the right
        self._axis_base = {}             # board -> baseline the right-edge capture used
        self._port = ""
        self._gen = 0                    # bumped on every reset/selection; stale timers check it
        self._prev_status = "pending"    # restored if a Retry is abandoned
        self._build_ui()
        self._timer = QtCore.QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        self.reset(None)

    # ------------------------------------------------------------ layout

    def _build_ui(self):
        left = QtWidgets.QVBoxLayout()
        self.step_list = QtWidgets.QListWidget()
        self.step_list.setMinimumWidth(220)
        self.step_list.currentRowChanged.connect(self._select)
        left.addWidget(self.step_list, 1)
        restart = QtWidgets.QPushButton("Start over")
        restart.clicked.connect(lambda: self.reset(self._dev()))
        self.save_btn = QtWidgets.QPushButton("Save report…")
        self.save_btn.clicked.connect(self.save_report)
        left.addWidget(restart)
        left.addWidget(self.save_btn)

        mid = QtWidgets.QVBoxLayout()
        self.title = QtWidgets.QLabel()
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.title.setFont(font)
        self.instructions = QtWidgets.QLabel()
        self.instructions.setWordWrap(True)
        self.instructions.setTextFormat(QtCore.Qt.RichText)
        self.state_label = QtWidgets.QLabel()
        sfont = self.state_label.font()
        sfont.setPointSizeF(sfont.pointSizeF() * 1.2)
        self.state_label.setFont(sfont)
        self.diagram = PadDiagram()
        buttons = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.clicked.connect(self.start_step)
        self.skip_btn = QtWidgets.QPushButton("Skip")
        self.skip_btn.clicked.connect(self.skip_step)
        for w in (self.start_btn, self.skip_btn):
            buttons.addWidget(w)
        buttons.addStretch(1)
        for w in (self.title, self.instructions, self.state_label):
            mid.addWidget(w)
        mid.addWidget(self.diagram, 1)
        mid.addLayout(buttons)

        self.results = QtWidgets.QTextBrowser()
        self.results.setMinimumWidth(360)

        row = QtWidgets.QHBoxLayout(self)
        row.addLayout(left, 0)
        row.addLayout(mid, 2)
        row.addWidget(self.results, 2)

    # ------------------------------------------------------------ steps

    def reset(self, dev, port=""):
        self.running = False
        self.capture = None
        self._axis_right = {}
        self._axis_base = {}
        self._gen += 1
        self._port = port or self._port
        self.report = V.Report()
        self.steps = []
        if dev is not None:
            self.report.firmware = dev.info.fw
            self.report.port = self._port
            self.report.settings = {p.name: p.value for p in dev.params}
            boards = range(len(dev.info.boards))
            # Stage 1, the bare board: health, then a magnet maps positions and axes.
            self.steps.append(Step("bare", "Bare board: rest and health", "bare"))
            for b in boards:
                for p, name in enumerate(CHIP_NAMES):
                    self.steps.append(Step(f"b{b}.mag{p}", f"b{b} magnet over {name}", "mag", b, p))
                for side in ("right", "top"):
                    self.steps.append(Step(f"b{b}.axis.{side}", f"b{b} magnet beside {side}",
                                           "axis", b, side=side))
            # Stage 2, the cuboid seated.
            self.steps.append(Step("cuboid", "Cuboid: rest", "cuboid", manual=True))
            for b in boards:
                self.steps.append(Step(f"b{b}.flat", f"b{b} flat press", "flat", b))
        self.step_list.blockSignals(True)
        self.step_list.clear()
        for s in self.steps:
            self.step_list.addItem(s.title)
        self.step_list.blockSignals(False)
        self.index = 0
        self._refresh_list()
        self._show_results()
        enabled = dev is not None
        for w in (self.start_btn, self.skip_btn, self.save_btn):
            w.setEnabled(enabled)
        if enabled:
            self.step_list.setCurrentRow(0)
            self._select(0)
        else:
            self.title.setText("Verify")
            self.instructions.setText("Connect a board to run the guided check.")
            self.state_label.setText("")

    def _select(self, row):
        if row < 0 or row >= len(self.steps):
            return
        if row != self.index:
            self._gen += 1                           # cancels a pending auto-advance
        if self.running and row != self.index:
            self.running = False
            self.capture = None
            if self.steps[self.index].status == "running":
                self.steps[self.index].status = self._prev_status
        self.index = row
        step = self.steps[row]
        self.title.setText(step.title)
        self.instructions.setText(self._instructions(step))
        self.state_label.setText("Press Start when ready." if step.kind in RESTS
                                 else "Press Start, then follow the instructions.")
        self.start_btn.setText("Retry" if step.status not in ("pending", "running") else "Start")
        self.skip_btn.setText("Skip (no magnet)" if step.kind in ("mag", "axis") else "Skip")
        self._refresh_list()
        self._draw(np.zeros((MAGS_PER_FINGER, 3)))

    def _instructions(self, step):
        b = f"board b{step.board}" if step.board is not None else ""
        if step.kind == "bare":
            return ("<b>Take the cuboid off</b> so the board is bare, keep magnets away and "
                    "don't touch it. For a fair noise reading, wait ~30 s after handling it. "
                    f"Measures for {REST_S:.0f} s.")
        if step.kind == "mag":
            return (f"Hold a small magnet directly over the <b>{CHIP_NAMES[step.position].upper()}"
                    f"</b> chip on {b} (ringed below), about 5 mm above it or touching, and keep "
                    "it still until the step completes. The dots grow with each chip's "
                    "response. Then take it away for the next chip.")
        if step.kind == "axis" and step.side == "right":
            return (f"Hold the magnet <b>level</b> with {b}, 2-3 cm beyond its <b>RIGHT</b> edge "
                    "(grey square), one face pointing at the board, and keep it still until the "
                    "step completes. Remember which face you pointed.")
        if step.kind == "axis":
            return ("Now the same magnet, held the same way, 2-3 cm beyond the <b>TOP</b> edge, "
                    "<b>the same face</b> pointing at the board.")
        if step.kind == "cuboid":
            return ("<b>Seat the cuboid</b> on each board and leave it untouched. "
                    f"Measures for {REST_S:.0f} s.")
        return (f"Press the whole top face of {b} <b>flat with your palm</b>, "
                "hold for a moment, then release.")

    def start_step(self):
        dev = self._dev()
        if dev is None or not self.steps:
            return
        step = self.steps[self.index]
        if step.status != "running":
            self._prev_status = step.status
        step.status = "running"
        self._refresh_list()
        self._last_t = time.time()
        if step.kind in RESTS:
            self._stats_ok = False
            if step.kind == "bare":
                try:
                    dev.stats()                      # zero the firmware's fail counters
                    self._stats_ok = True
                except Exception:  # noqa: BLE001 -- reported as a missing check
                    pass
            self._rest = []
            self._t0 = time.time()
            self._last_t = self._t0
        else:
            data = self._board_data(step.board)
            noise = np.array(data.get("noise_ut", [3.0] * MAGS_PER_FINGER))
            if step.kind in ("mag", "axis"):
                # Against the bare board at rest, so a magnet still hovering from
                # the previous step has to go before this one arms. Without a
                # bare rest, the top-edge step reuses the right-edge baseline.
                base = data.get("bare_baseline_ut")
                if base is None and step.side == "top":
                    base = self._axis_base.get(step.board)
                self.capture = V.PressCapture(noise, mode="hold", hold_s=1.0,
                                              baseline=None if base is None else np.array(base))
            else:
                # Fresh baseline, but not one taken while still pressing.
                ref = data.get("cuboid_baseline_ut")
                self.capture = V.PressCapture(noise, mode="peak",
                                              reference=None if ref is None else np.array(ref))
        self.running = True
        self.start_btn.setText("Retry")

    def skip_step(self):
        if not self.steps:
            return
        self.running = False
        self.capture = None
        step = self.steps[self.index]
        data = self._board_data(step.board)
        if step.kind == "mag" and data.get("positions"):
            # A skipped position must not keep an earlier press in the map.
            data["positions"].pop(CHIP_NAMES[step.position], None)
            self._update_map(step.board)
        if step.kind == "axis" and step.side == "right":
            self._axis_right.pop(step.board, None)
            self._axis_base.pop(step.board, None)
        self._finish(step, [V.Check(step.title, V.SKIP, "skipped", step.board)])

    def _finish(self, step, checks):
        self.running = False
        self.capture = None
        self.report.set_step(step.key, checks)
        step.status = worst(checks)
        self._refresh_list()
        self._show_results()
        if step.status == V.FAIL:
            self.state_label.setText(f"<span style='color:{COLORS[V.FAIL]}'>✗ Failed</span>"
                                     " -- see the results, then Retry or move on.")
            return
        self.state_label.setText(f"<span style='color:{COLORS[step.status]}'>"
                                 f"{MARKS[step.status]} {step.status}</span>")
        if self.index + 1 < len(self.steps):
            nxt, gen = self.index + 1, self._gen
            # Chain into the next step; its capture waits for the user anyway.
            QtCore.QTimer.singleShot(700, lambda: self._advance(nxt, gen))

    def _advance(self, row, gen):
        if gen != self._gen or self.running or row >= len(self.steps) or self.index != row - 1:
            return                     # Start over, a disconnect or the user moved on meanwhile
        self.step_list.setCurrentRow(row)
        step = self.steps[row]
        if step.manual:
            self.state_label.setText("Set up as described above, then press Start.")
        elif step.status == "pending":
            self.start_step()

    # ------------------------------------------------------------ live data

    def tick(self):
        dev = self._dev()
        if not self.running or dev is None:
            return
        step = self.steps[self.index]
        t, d = dev.snapshot(4000)
        new = t > self._last_t
        t, d = t[new], d[new]
        if len(t):
            self._last_t = t[-1]

        if step.kind in RESTS:
            self._rest.extend(zip(t, d))
            elapsed = time.time() - self._t0
            counted = sum(1 for ft, _ in self._rest if ft >= self._t0 + 0.3)
            if elapsed < REST_S:
                self.state_label.setText(f"Measuring, keep still… {REST_S - elapsed:.1f} s")
            else:
                # Slow settings (~5 Hz) need longer to collect enough frames.
                self.state_label.setText(f"Measuring, keep still… {counted}/{REST_MIN_FRAMES} "
                                         "frames")
            if (elapsed >= REST_S and counted >= REST_MIN_FRAMES) or elapsed >= REST_MAX_S:
                self._finish_rest(dev)
            return

        xyz = d[:, step.board * MAGS_PER_FINGER:(step.board + 1) * MAGS_PER_FINGER, 1:]
        state = self.capture.feed(t, xyz)
        self._draw(self.capture.live)
        self.state_label.setText(self._state_text(step, state))
        if state == "done":
            self._finish_capture(step, self.capture.result)

    def _state_text(self, step, state):
        if state == "settling":
            if self.capture is not None and self.capture.held:
                return "Still pressed? Take your hand away so it can take a baseline…"
            return "Hands off, taking a baseline…"
        if state == "clearing":
            return "Take the magnet away…"
        act = {"flat": "press flat with your palm",
               "mag": "bring the magnet over the ringed chip",
               "axis": f"bring the magnet in beyond the {step.side} edge"}[step.kind]
        if state == "waiting":
            return f"Ready: {act}."
        if step.kind in ("mag", "axis"):
            return "Hold the magnet still…"
        return "Got it, now release."

    def _draw(self, dB):
        step = self.steps[self.index] if self.steps else None
        if step is None:
            return
        mag = np.linalg.norm(dB, axis=1)
        # Raw response against the strongest chip, never scaled per chip: dividing
        # by a chip's own flat-press response made the weakest chip look loudest.
        ref = np.full(MAGS_PER_FINGER, max(float(mag.max()), 300.0))
        vectors = None
        if step.kind == "axis" and mag.max() > 0:
            v = to_pad_frame(dB)
            vectors = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
            vectors *= (mag > 5)[:, None]
        target = step.position if step.kind == "mag" else None
        self.diagram.show_state(mag / np.maximum(ref, 1.0), target, vectors, step.side)

    def _board_data(self, b):
        return self.report.boards.get(b, {}) if b is not None else {}

    def _update_map(self, b):
        positions = self._board_data(b).get("positions", {})
        self.report.steps.pop(f"b{b}.map", None)
        if positions:
            responders = {CHIP_NAMES.index(n): m["responder"] for n, m in positions.items()}
            self.report.set_step(f"b{b}.map", [V.mapping_summary(b, responders)])

    # ------------------------------------------------------------ results

    def _finish_rest(self, dev):
        frames = [f for t, f in self._rest if t >= self._t0 + 0.3]
        if len(frames) < 3:
            msg = ("no data arrived; is the stream running?" if not frames else
                   f"only {len(frames)} frames in {REST_MAX_S:.0f} s; is the stream stalling?")
            self._finish(self.steps[self.index], [V.Check("rest", V.FAIL, msg)])
            return
        data = np.stack(frames)
        fails = None
        if self._stats_ok:
            try:
                fails = dev.stats()["fails"]
            except Exception:  # noqa: BLE001
                pass
        lsb = np.array([dev.state.get(f"lsb_{a}", 1.0) for a in "xyz"])
        step = self.steps[self.index]
        checks = []
        for b, board in enumerate(dev.info.boards):
            sl = slice(b * MAGS_PER_FINGER, (b + 1) * MAGS_PER_FINGER)
            xyz = data[:, sl, 1:]
            record = self.report.board(b)
            if step.kind == "bare":
                c, measured = V.health_checks(b, board.addrs, xyz, lsb,
                                              fails[sl] if fails else None)
                if fails is None:
                    c.insert(1, V.Check("read failures", V.SKIP,
                                        "STATS unavailable, not checked", b))
                bare, bare_data = V.bare_check(b, xyz)
                checks += c + [bare]
                record.update(measured, **bare_data, where=board.where,
                              addresses=[f"0x{a:02X}" for a in board.addrs])
            else:
                c, measured = V.cuboid_checks(b, xyz, record.get("noise_ut"))
                checks += c
                record.update(measured, cuboid_baseline_ut=xyz.mean(axis=0).tolist())
        self._finish(step, checks)

    def _finish_capture(self, step, dB):
        b = step.board
        data = self.report.board(b)
        data.setdefault("captures", {})[step.key] = np.round(dB, 1).tolist()
        noise = np.array(data.get("noise_ut", [3.0] * MAGS_PER_FINGER))
        if step.kind == "flat":
            checks, measured = V.flat_press_checks(b, dB, noise, data.get("polarity_rest"))
            data.update(measured)
            off = self.capture.reference_offset
            if self.capture.reference is not None and \
                    off > 0.03 * np.linalg.norm(self.capture.reference):
                checks.append(V.Check("rest moved", V.INFO, f"before the press the field was "
                                      f"{off:.0f} uT from the cuboid rest reading; if the cuboid "
                                      "was reseated, rerun 'Cuboid: rest'", b))
        elif step.kind == "mag":
            check, measured = V.magnet_position_check(b, step.position, dB)
            data.setdefault("positions", {})[CHIP_NAMES[step.position]] = measured
            checks = [check]
            self.report.steps.setdefault(step.key, [])     # keep the map after the presses
            self._update_map(b)
        elif step.side == "right":
            self._axis_right[b] = dB
            self._axis_base[b] = self.capture.baseline
            checks = [V.Check("magnet right", V.INFO, "captured; now the top edge", b)]
        else:
            if b not in self._axis_right:
                checks = [V.Check("axes", V.SKIP, "magnet-right step not done", b)]
            else:
                checks, measured = V.axis_checks(b, self._axis_right[b], dB)
                data.update(axes=measured)
        self._finish(step, checks)

    def _refresh_list(self):
        for i, s in enumerate(self.steps):
            item = self.step_list.item(i)
            if item is None:
                continue
            item.setText(f"{MARKS[s.status]}  {s.title}")
            item.setForeground(pg.mkColor(COLORS[s.status]))

    def _sync_report(self):
        self.report.not_run = [s.title for s in self.steps if s.status in ("pending", "running")]
        dev = self._dev()
        if dev is not None:
            self.report.settings = {p.name: p.value for p in dev.params}

    def _show_results(self):
        self._sync_report()
        if not self.report.checks:
            self.results.setHtml("<p>Results appear here as each step finishes.</p>")
            return
        verdict = self.report.verdict
        html = [f"<h3>Overall: <span style='color:{COLORS[verdict]}'>"
                f"{MARKS[verdict]} {verdict}</span></h3>"]
        boards = sorted({c.board for c in self.report.checks if c.board is not None})
        for b in boards:
            html.append(f"<h4>Board b{b}</h4><table cellspacing=4>")
            for c in self.report.checks:
                if c.board != b:
                    continue
                html.append(f"<tr><td style='color:{COLORS[c.status]}'>{MARKS[c.status]}"
                            f"&nbsp;{c.status}</td><td><b>{c.name}</b></td>"
                            f"<td>{c.detail}</td></tr>")
            html.append("</table>")
        if self.report.not_run:
            html.append(f"<p style='color:{COLORS['pending']}'>Not run yet: "
                        f"{len(self.report.not_run)} step(s)</p>")
        loose = [c for c in self.report.checks if c.board is None]
        for c in loose:
            html.append(f"<p style='color:{COLORS[c.status]}'>{MARKS[c.status]} {c.name}: "
                        f"{c.detail}</p>")
        self.results.setHtml("".join(html))

    def save_report(self):
        if not self.report.checks:
            return
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save verification report",
            os.path.join(REPORT_DIR, f"eflesh_verify_{stamp}.md"), "Markdown (*.md)")
        if path:
            self.write_report(path)

    def write_report(self, path):
        """Write the report as Markdown at `path` and JSON beside it."""
        self._sync_report()
        stem = os.path.splitext(path)[0]
        with open(stem + ".md", "w") as f:
            f.write(self.report.to_markdown())
        with open(stem + ".json", "w") as f:
            json.dump(self.report.to_dict(), f, indent=2)
        return stem + ".md", stem + ".json"
