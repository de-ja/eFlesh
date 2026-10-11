#!/usr/bin/env python
"""Multi-signal eFlesh dashboard for a two-finger gripper.

Shows, live, everything the sensor supports without a calibration rig:

  per finger   pad view with per-chip normal (circles) and shear (vectors),
               contact centroid, force / normal / shear / vibration readouts,
               and rolling strip charts
  across both  grip force and left-right balance

The three quantities the eFlesh paper characterises -- normal force, shear
force, spatial resolution -- appear here as uncalibrated proxies computed from
geometry. To read them in newtons and millimetres you need probe data from your
own rig; train with characterization/train.py and pass the models in through
eflesh.FingerProcessor(calibration=...).

Keys
  b       re-baseline (do this untouched, after the sensor has settled)
  r       start/stop recording to CSV
  space   freeze the display
  q/esc   quit

Usage
  python visualizer/viz_gripper.py -p /dev/ttyACM0
  python visualizer/viz_gripper.py -p /dev/ttyACM0 --fingers 1
"""

import argparse
import os
import sys
import time
from collections import deque
from datetime import datetime

import numpy as np

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from eflesh import (  # noqa: E402
    CHIP_NAMES,
    CHIP_POSITIONS_MM,
    EFleshSource,
    FakeEFleshSource,
    to_pad_frame,
)

# ----------------------------------------------------------------- appearance

W, H = 1280, 860
BG = (18, 20, 24)
PANEL = (28, 31, 37)
EDGE = (52, 57, 66)
TEXT = (222, 226, 232)
MUTED = (128, 136, 149)
PAD = (44, 48, 56)

# Okabe-Ito, so the two fingers stay distinguishable for colourblind viewers.
FINGER_COLORS = [(86, 180, 233), (230, 159, 0)]
NORMAL_COLOR = (213, 94, 0)
SHEAR_COLOR = (0, 158, 115)
CENTROID_COLOR = (240, 228, 66)
ALERT = (204, 121, 167)

PAD_MM = 20.0          # half-width of the drawn pad, millimetres
PAD_PX = 168           # half-width on screen, pixels
MM_TO_PX = PAD_PX / PAD_MM


class Strip:
    """A rolling strip chart with a decaying autoscale."""

    def __init__(self, label, rect, n_series, width_px, unit="uT", floor=1.0):
        self.label = label
        self.rect = pygame.Rect(rect)
        self.unit = unit
        self.floor = floor
        self.series = [deque(maxlen=width_px) for _ in range(n_series)]
        self._scale = floor

    def push(self, values):
        for buf, v in zip(self.series, values):
            buf.append(float(v))
        peak = max((max(b) if b else 0.0) for b in self.series)
        # Rise immediately, fall slowly, so a spike stays readable then relaxes.
        self._scale = max(peak, self._scale * 0.995, self.floor)

    def draw(self, surf, fonts):
        r = self.rect
        pygame.draw.rect(surf, PANEL, r, border_radius=6)
        pygame.draw.rect(surf, EDGE, r, width=1, border_radius=6)

        label = fonts["small"].render(self.label, True, MUTED)
        surf.blit(label, (r.x + 10, r.y + 6))
        top = fonts["small"].render(f"{self._scale:.0f} {self.unit}", True, EDGE)
        surf.blit(top, (r.right - top.get_width() - 10, r.y + 6))

        plot_top = r.y + 24
        plot_h = r.height - 32
        for color, buf in zip(FINGER_COLORS, self.series):
            if len(buf) < 2:
                continue
            pts = [
                (r.x + 6 + i, plot_top + plot_h - (v / self._scale) * plot_h)
                for i, v in enumerate(buf)
            ]
            pygame.draw.lines(surf, color, False, pts, 2)


def draw_pad(surf, fonts, origin, finger, color, name, scaling):
    """Draw one finger's pad: chips, per-chip normal and shear, centroid."""
    cx, cy = origin
    pad_rect = pygame.Rect(cx - PAD_PX - 16, cy - PAD_PX - 16,
                           2 * (PAD_PX + 16), 2 * (PAD_PX + 16))
    pygame.draw.rect(surf, PANEL, pad_rect, border_radius=10)
    pygame.draw.rect(surf, color if finger.contact else EDGE, pad_rect,
                     width=2 if finger.contact else 1, border_radius=10)

    inner = pygame.Rect(cx - PAD_PX, cy - PAD_PX, 2 * PAD_PX, 2 * PAD_PX)
    pygame.draw.rect(surf, PAD, inner, border_radius=8)

    title = fonts["mid"].render(name, True, color)
    surf.blit(title, (pad_rect.x + 10, pad_rect.y + 8))
    if not finger.contact:
        nc = fonts["small"].render("no contact", True, MUTED)
        surf.blit(nc, (pad_rect.right - nc.get_width() - 10, pad_rect.y + 12))

    xy = to_pad_frame(finger.dB)
    for i, pos_mm in enumerate(CHIP_POSITIONS_MM):
        px = int(cx + pos_mm[0] * MM_TO_PX)
        py = int(cy - pos_mm[1] * MM_TO_PX)

        pygame.draw.circle(surf, EDGE, (px, py), 4, 1)

        # Normal component: radius is |dBz|, hollow when the sign is negative.
        dbz = finger.dB[i, 2]
        radius = min(abs(dbz) / scaling, PAD_PX * 0.9)
        if radius > 1:
            pygame.draw.circle(surf, NORMAL_COLOR, (px, py), int(radius),
                               2 if dbz < 0 else 0)

        # Shear component: in-plane vector in the shared pad frame.
        vec = xy[i] / scaling
        if np.linalg.norm(vec) > 2:
            pygame.draw.line(surf, SHEAR_COLOR, (px, py),
                             (px + vec[0], py + vec[1]), 4)

        tag = fonts["tiny"].render(CHIP_NAMES[i], True, MUTED)
        surf.blit(tag, (px - tag.get_width() // 2, py + 10))

    if finger.centroid is not None:
        gx = int(cx + finger.centroid[0] * MM_TO_PX)
        gy = int(cy - finger.centroid[1] * MM_TO_PX)
        pygame.draw.line(surf, CENTROID_COLOR, (gx - 9, gy), (gx + 9, gy), 2)
        pygame.draw.line(surf, CENTROID_COLOR, (gx, gy - 9), (gx, gy + 9), 2)
        pygame.draw.circle(surf, CENTROID_COLOR, (gx, gy), 13, 1)

    return pad_rect


def draw_readout(surf, fonts, rect, signals):
    """Numeric panel: per-finger values plus the cross-finger balance bar."""
    pygame.draw.rect(surf, PANEL, rect, border_radius=8)
    pygame.draw.rect(surf, EDGE, rect, width=1, border_radius=8)

    y = rect.y + 12
    hdr = fonts["mid"].render("signals", True, TEXT)
    surf.blit(hdr, (rect.x + 14, y))
    y += 32

    for idx, f in enumerate(signals.fingers):
        color = FINGER_COLORS[idx]
        name = fonts["small"].render(f"finger {idx}", True, color)
        surf.blit(name, (rect.x + 14, y))
        y += 22

        pos = "--" if f.centroid is None else f"{f.centroid[0]:+.1f}, {f.centroid[1]:+.1f} mm"
        rows = [
            ("force", f"{f.force:8.0f} uT"),
            ("normal", f"{f.normal:8.0f} uT"),
            ("shear", f"{f.shear_magnitude:8.0f} uT"),
            ("angle", f"{np.degrees(f.shear_angle):8.0f} deg"),
            ("position", pos),
            ("vibration", f"{f.vibration:8.1f} LSB"),
        ]
        for k, v in rows + [(k, f"{v:.2f}") for k, v in f.calibrated.items()]:
            surf.blit(fonts["tiny"].render(k, True, MUTED), (rect.x + 24, y))
            val = fonts["tiny"].render(v, True, TEXT)
            surf.blit(val, (rect.right - val.get_width() - 16, y))
            y += 18
        y += 12

    balance = signals.balance
    if balance is not None:
        surf.blit(fonts["small"].render("balance", True, MUTED), (rect.x + 14, y))
        y += 22
        bar = pygame.Rect(rect.x + 16, y, rect.width - 32, 14)
        pygame.draw.rect(surf, PAD, bar, border_radius=7)
        mid = bar.centerx
        pygame.draw.line(surf, EDGE, (mid, bar.y), (mid, bar.bottom), 1)
        knob = int(mid + balance * (bar.width / 2 - 6))
        col = FINGER_COLORS[0] if balance > 0 else FINGER_COLORS[1]
        pygame.draw.circle(surf, col if abs(balance) > 0.05 else MUTED,
                           (knob, bar.centery), 6)
        y += 24
        txt = f"{balance:+.2f}   grip {signals.grip_force:.0f} uT"
        surf.blit(fonts["tiny"].render(txt, True, MUTED), (rect.x + 16, y))


def run(port, num_fingers, scaling, settle, fake=False):
    if fake:
        source = FakeEFleshSource(num_fingers=num_fingers or 2)
    else:
        source = EFleshSource(port=port, num_fingers=num_fingers or None,
                              settle=settle)
        print(f"Settling for {settle:.0f} s before baselining -- "
              "do not touch the sensor.")
    source.start()
    num_fingers = source.num_fingers
    print(f"{num_fingers} finger(s), baseline captured.")

    pygame.init()
    window = pygame.display.set_mode((W, H))
    pygame.display.set_caption("eFlesh gripper")
    clock = pygame.time.Clock()
    fonts = {
        "big": pygame.font.Font(None, 34),
        "mid": pygame.font.Font(None, 26),
        "small": pygame.font.Font(None, 22),
        "tiny": pygame.font.Font(None, 19),
    }

    chart_w = W - 48 - 12
    charts = [
        Strip("force  ||dB||", (24, 496, W - 48, 112), num_fingers, chart_w, floor=200),
        Strip("shear  |in-plane|", (24, 616, W - 48, 112), num_fingers, chart_w, floor=100),
        Strip("vibration", (24, 736, W - 48, 112), num_fingers, chart_w,
              unit="LSB", floor=2.0),
    ]

    recording, rec_rows, rec_name = False, [], None
    frozen = False
    last = None
    running = True

    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif event.key == pygame.K_b:
                    source.rebaseline()
                    print("Re-baselined.")
                elif event.key == pygame.K_SPACE:
                    frozen = not frozen
                elif event.key == pygame.K_r:
                    recording = not recording
                    if recording:
                        os.makedirs("data", exist_ok=True)
                        rec_name = ("data/gripper_"
                                    + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                                    + ".csv")
                        rec_rows = []
                        print(f"Recording to {rec_name}")
                    else:
                        np.savetxt(rec_name, np.array(rec_rows), delimiter=",")
                        print(f"Wrote {len(rec_rows)} rows to {rec_name}")

        if not frozen:
            reading = source.read_latest()
            t, flat, signals = reading.timestamp, reading.values, reading.signals
            last = signals
            charts[0].push([f.force for f in signals.fingers])
            charts[1].push([f.shear_magnitude for f in signals.fingers])
            charts[2].push([f.vibration for f in signals.fingers])
            if recording:
                row = [t] + list(flat)
                for f in signals.fingers:
                    row += [f.force, f.normal, f.shear_magnitude, f.vibration,
                            *(f.centroid if f.centroid is not None else (np.nan, np.nan))]
                rec_rows.append(row)

        if last is None:
            continue

        window.fill(BG)

        title = fonts["big"].render("eFlesh gripper", True, TEXT)
        window.blit(title, (24, 18))
        state = "CONTACT" if last.any_contact else "open"
        col = ALERT if last.any_contact else MUTED
        st = fonts["mid"].render(state, True, col)
        window.blit(st, (24 + title.get_width() + 20, 24))
        hint = "b baseline    r record    space freeze    q quit"
        window.blit(fonts["tiny"].render(hint, True, EDGE), (W - 330, 30))
        if recording:
            pygame.draw.circle(window, (220, 60, 60), (W - 360, 36), 6)
        if frozen:
            window.blit(fonts["tiny"].render("FROZEN", True, ALERT), (W - 400, 30))

        centres = [(224, 268), (632, 268)][:num_fingers]
        for idx, (finger, centre) in enumerate(zip(last.fingers, centres)):
            draw_pad(window, fonts, centre, finger, FINGER_COLORS[idx],
                     f"finger {idx}", scaling)

        draw_readout(window, fonts, pygame.Rect(856, 76, 400, 400), last)
        for c in charts:
            c.draw(window, fonts)

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()
    if recording and rec_rows:
        np.savetxt(rec_name, np.array(rec_rows), delimiter=",")
        print(f"Wrote {len(rec_rows)} rows to {rec_name}")
    source.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--port", default="/dev/ttyACM0")
    p.add_argument("-f", "--fingers", type=int, default=0, choices=(0, 1, 2),
                   help="0 = detect from the stream (default)")
    p.add_argument("-s", "--scaling", type=float, default=10.0,
                   help="microtesla per pixel for the pad overlays")
    p.add_argument("--settle", type=float, default=5.0,
                   help="seconds to wait before baselining")
    p.add_argument("--fake", action="store_true",
                   help="synthetic data, no hardware")
    a = p.parse_args()
    run(a.port, a.fingers, a.scaling, a.settle, a.fake)
