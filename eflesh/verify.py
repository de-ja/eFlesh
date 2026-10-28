#!/usr/bin/env python
"""Guided verification of an eFlesh board, bare and then with its cuboid.

The checks in DEFAULT_CUBOID_SPECS.md ("Verifying an assembled cuboid") as
code, so a new build can be checked in a few minutes and the result saved.
Pure numpy, no UI: the tuner's Verify tab drives it, and so can a script.

Board, bare (needs a small magnet for all but the first step)
  rest        addresses, read failures, noise floor, and that it really is bare
  positions   the magnet held over each chip in turn must move that chip the
              most. On a bare board the chip under the magnet dominates by ~5x
              at 5 mm, so raw dB decides it.
  axes        the magnet held level beside the board, first beyond the right
              edge, then beyond the top edge. Every chip's in-plane vector,
              rotated into the pad frame, must point the same way, along x for
              "right", and turn +90 degrees for "top". The sign of the field
              depends on which face of the magnet points at the board, the turn
              does not, so this catches a wrong chip rotation and a mirrored
              frame without knowing the magnet.

Cuboid seated
  rest        seated, and the magnet polarity the resting profile implies
  flat press  every chip responds; the peaks and the dBz signs are recorded

There is no fingertip version of the position check. The magnets sit on the
corners and the chips on the axes, so a fingertip over the right chip moves
the top-right magnet, which is as close to the top chip (~8 mm) as to the
right one; on hardware this mapped presses to the wrong chip. Only a magnet
held over a chip on the bare board isolates it.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import numpy as np

from .signals import CHIP_NAMES, CONTACT_THRESHOLD_UT, MAGS_PER_FINGER, to_pad_frame

#: Address sets the firmware recognises, in stream (position) order.
KNOWN_SETS = {
    "white": (0x0C, 0x11, 0x12, 0x13, 0x10),
    "consecutive": (0x0C, 0x0D, 0x0E, 0x0F, 0x10),
}

NOISE_PASS_LSB = 2.0       # settled boards measure ~1 LSB
NOISE_WARN_LSB = 4.0       # above this something is wrong, not just unsettled
STILL_MIN_UT = 20.0        # rest field may move this much (or 10x noise) across the window
CUBOID_MIN_UT = 500.0      # bare board: ambient, 40-400 uT; seated: 700+ (alternating), ~14 mT (aligned)
RESPONSE_MIN_SNR = 20.0    # flat-press peak over noise; build #1 measured 300-1000
MAGNET_MARGIN = 1.5        # magnet over a bare chip: ~5x expected at 5 mm
AXIS_SPREAD_DEG = 45.0     # chips beside the magnet's axis see a tilted field
AXIS_TOL_DEG = 30.0

PASS, WARN, FAIL, INFO, SKIP = "pass", "warn", "fail", "info", "skip"
INCOMPLETE = "incomplete"   # overall verdict when steps were skipped or not run


@dataclass
class Check:
    name: str
    status: str
    detail: str
    board: Optional[int] = None


def _fmt(values, digits=0):
    return ", ".join(f"{n} {v:.{digits}f}" for n, v in zip(CHIP_NAMES, values))


def _angle_deg(v):
    return float(np.degrees(np.arctan2(v[1], v[0])))


def _wrap(deg):
    return (deg + 180.0) % 360.0 - 180.0


# ---------------------------------------------------------------- capture

class PressCapture:
    """Watches one board for a press (or a held magnet).

    Feed it frames as they arrive. Without a `baseline` it first waits for a
    quiet 0.5 s and takes that as the baseline; given a `reference` (the board
    at rest), a quiet window far from it is refused, since that is a hand or
    magnet held still rather than rest. With a `baseline` (the bare board at
    rest, for the magnet steps) it instead waits until the field is back near
    it for 0.3 s. Either way a press or magnet left over from the previous
    step cannot become part of the reference. Then it waits for the total
    |dB| to cross the threshold.

    mode "peak": done on release, once the total falls to 5 % of its peak (or
                 0.4x the threshold, if higher); `result` is each chip's dB at
                 its own peak.
    mode "hold": done after `hold_s` of continuous contact; `result` is the
                 mean dB over the last second, for direction measurements.
    """

    QUIET_S = 0.5

    def __init__(self, noise_ut: np.ndarray, mode: str = "peak", hold_s: float = 1.5,
                 threshold_ut: float = CONTACT_THRESHOLD_UT,
                 baseline: Optional[np.ndarray] = None,
                 reference: Optional[np.ndarray] = None):
        self.noise = np.maximum(np.asarray(noise_ut, float), 1.0)       # (5,)
        self.mode = mode
        self.hold_s = hold_s
        self.threshold = threshold_ut
        # settling (or clearing, with a given baseline) -> waiting -> active -> done
        self.state = "settling" if baseline is None else "clearing"
        self.baseline = None if baseline is None else np.asarray(baseline, float)
        self.reference = None if reference is None else np.asarray(reference, float)
        self.held = False                  # settling refused: still pressed / magnet near
        self.reference_offset = 0.0        # uT between the accepted baseline and `reference`
        self._held_since = None
        self._clear_since = None
        self._peak_total = 0.0
        self.result = None
        self.live = np.zeros((MAGS_PER_FINGER, 3))
        self._window = []                  # (t, xyz) while settling, recent frames while held
        self._t_on = 0.0
        self._peak = np.zeros(MAGS_PER_FINGER)

    @property
    def total(self) -> float:
        return float(np.linalg.norm(self.live))

    def feed(self, t: np.ndarray, xyz: np.ndarray) -> str:
        """t: (n,) seconds; xyz: (n, 5, 3) microtesla for this board."""
        for ti, x in zip(t, xyz):
            if self.state == "done":
                break
            getattr(self, f"_on_{self.state}")(float(ti), x.astype(float))
        return self.state

    def _on_settling(self, t, x):
        if self.reference is not None:
            self.live = x - self.reference
        self._window.append((t, x))
        # Keep the shortest window that still spans QUIET_S, so this works at
        # any frame rate (the slowest settings give ~5 Hz).
        while len(self._window) >= 2 and t - self._window[1][0] >= self.QUIET_S:
            self._window.pop(0)
        if t - self._window[0][0] < self.QUIET_S or len(self._window) < 3:
            return
        w = np.stack([f for _, f in self._window])
        if not (w.std(axis=0) <= 4 * self.noise[:, None] + 3.0).all():
            return
        mean = w.mean(axis=0)
        if self.reference is not None:
            off = float(np.linalg.norm(mean - self.reference))
            # A cuboid creeps or reseats by a few percent of its field; a hand
            # held on it moves far more. Past 3 quiet seconds, take it anyway.
            tol = max(0.5 * self.threshold, 0.03 * float(np.linalg.norm(self.reference)))
            if off > tol:
                if self._held_since is None:
                    self._held_since = t
                if t - self._held_since < 3.0:
                    self.held = True
                    return
            self.reference_offset = off
        self.held = False
        self.baseline = mean
        self._window = []
        self.state = "waiting"

    def _on_clearing(self, t, x):
        self.live = x - self.baseline
        if self.total >= 0.5 * self.threshold:
            self._clear_since = None
        elif self._clear_since is None:
            self._clear_since = t
        elif t - self._clear_since >= 0.3:
            self.state = "waiting"

    def _on_waiting(self, t, x):
        self.live = x - self.baseline
        if self.total > self.threshold:
            self.state = "active"
            self._t_on = t
            self._peak[:] = 0
            self._peak_total = 0.0
            self.result = np.zeros_like(self.live)
            self._window = []
            self._on_active(t, x)

    def _on_active(self, t, x):
        self.live = dB = x - self.baseline
        if self.mode == "peak":
            mag = np.linalg.norm(dB, axis=1)
            higher = mag > self._peak
            self._peak[higher] = mag[higher]
            self.result[higher] = dB[higher]
            self._peak_total = max(self._peak_total, self.total)
            # Relative release: an aligned build moves ~8 mT and may not come
            # back within an absolute few tens of uT straight away.
            if self.total < max(0.4 * self.threshold, 0.05 * self._peak_total):
                self.state = "done" if t - self._t_on >= 0.15 else "waiting"
            return
        if self.total < self.threshold:            # let go too early: wait again
            self.state = "waiting"
            return
        self._window.append((t, dB))
        while self._window and t - self._window[0][0] > 1.0:
            self._window.pop(0)
        if t - self._t_on >= self.hold_s:
            self.result = np.mean([f for _, f in self._window], axis=0)
            self.state = "done"


# ---------------------------------------------------------------- checks

def _rest_field(xyz: np.ndarray) -> np.ndarray:
    return np.linalg.norm(xyz.mean(axis=0), axis=1)            # (5,) resting |B|


def health_checks(board: int, addrs: Sequence[int], xyz: np.ndarray, lsb: np.ndarray,
                  fails: Optional[Sequence[int]]) -> (List[Check], dict):
    """Addresses, read failures and noise. xyz: (n, 5, 3) uT of an untouched
    board; lsb: (3,) uT per LSB."""
    checks = []
    addrs = tuple(addrs)
    variant = next((k for k, s in KNOWN_SETS.items() if s == addrs), None)
    shown = " ".join(f"0x{a:02X}" for a in addrs)
    if variant is None:
        checks.append(Check("addresses", FAIL, f"unrecognised set {shown}; streamed in "
                            "ascending order, so positions are unknown", board))
    else:
        note = "" if variant == "white" else " -- not yet mapped, the position check maps it"
        checks.append(Check("addresses", PASS, f"{variant} set {shown}{note}", board))

    if fails is not None:
        bad = {CHIP_NAMES[i]: f for i, f in enumerate(fails) if f}
        checks.append(Check("read failures", FAIL if bad else PASS,
                            f"failed reads: {bad}" if bad else "none", board))

    # Sample-to-sample differences: white noise gives the same sigma, but a slow
    # drift (the board nudged, a magnet moving nearby) does not inflate it.
    sigma = np.diff(xyz, axis=0).std(axis=0) / np.sqrt(2)    # (5, 3)
    sigma_lsb = sigma / lsb
    noise_ut = np.sqrt((sigma ** 2).mean(axis=1))
    worst = float(sigma_lsb.max())
    status = PASS if worst <= NOISE_PASS_LSB else WARN if worst <= NOISE_WARN_LSB else FAIL
    detail = f"worst {worst:.2f} LSB; per chip (uT, rms of x/y/z): " + _fmt(noise_ut, 1)
    if status != PASS:
        detail += ". Leave it untouched ~30 s after handling and rerun."
    checks.append(Check("noise", status, detail, board))
    checks.append(still_check(board, xyz, noise_ut))
    return checks, {"variant": variant, "noise_ut": noise_ut.tolist(),
                    "noise_lsb": sigma_lsb.tolist()}


def still_check(board: int, xyz: np.ndarray, noise_ut: np.ndarray) -> Check:
    """Did the field move during a rest window? It becomes a reference later."""
    n = max(len(xyz) // 6, 1)
    moved = np.linalg.norm(xyz[-n:].mean(axis=0) - xyz[:n].mean(axis=0), axis=1)
    limit = np.maximum(STILL_MIN_UT, 10 * noise_ut)
    if (moved <= limit).all():
        return Check("still", PASS, f"field steady, moved at most {moved.max():.0f} uT", board)
    return Check("still", WARN, f"field moved up to {moved.max():.0f} uT during the measurement "
                 "-- something moved nearby (a magnet, the board, a phone). Keep still and "
                 "rerun.", board)


def bare_check(board: int, xyz: np.ndarray) -> (Check, dict):
    """The magnet steps need the board without its cuboid."""
    mag = _rest_field(xyz)
    bare = bool((mag < CUBOID_MIN_UT).all())
    detail = "resting |B| uT: " + _fmt(mag)
    if not bare:
        detail = ("reads like a cuboid or magnet is on the board -- take it off for the "
                  "board checks. " + detail)
    return Check("bare board", PASS if bare else WARN, detail, board), {
        "bare_ut": mag.tolist(), "bare_baseline_ut": xyz.mean(axis=0).tolist()}


def cuboid_checks(board: int, xyz: np.ndarray,
                  noise_ut: Optional[np.ndarray] = None) -> (List[Check], dict):
    """Seated, still, and the polarity the resting profile implies.

    Seating is judged on the four arms only: with alternating magnets the
    centre sits on a field null and may read near ambient on a good build."""
    mag = _rest_field(xyz)
    low = mag[1:] < CUBOID_MIN_UT
    if low.all():
        note = (f"every arm reads under {CUBOID_MIN_UT:.0f} uT, which is a bare board; a seated "
                "cuboid gives 700+ (alternating) or ~14 mT (aligned). Seat it and Retry. ")
    elif low.any():
        note = (f"{', '.join(CHIP_NAMES[i + 1] for i in np.where(low)[0])} under "
                f"{CUBOID_MIN_UT:.0f} uT while other arms are higher -- cuboid off-centre or a "
                "magnet missing? ")
    else:
        note = ""
    status = FAIL if low.all() else WARN if low.any() else PASS
    checks = [Check("cuboid seated", status, note + "resting |B| uT: " + _fmt(mag), board)]
    checks.append(still_check(board, xyz, np.full(MAGS_PER_FINGER, 3.0)
                              if noise_ut is None else np.asarray(noise_ut)))
    polarity = polarity_from_rest(mag) if not low.all() else "unclear"
    checks.append(Check("polarity (rest)", INFO, polarity_text(polarity, mag), board))
    return checks, {"rest_ut": mag.tolist(), "polarity_rest": polarity}


def polarity_from_rest(mag: np.ndarray) -> str:
    """Four alternating magnets cancel over the centre chip; aligned ones give
    five near-equal readings (build #1: centre 0.38x the arms; the aligned
    cuboid: all five within 7 %, centre 0.95x). Anything else -- including a
    'stripes' mis-assembly with two adjacent magnets flipped -- is unclear."""
    arms = np.median(mag[1:])
    if mag[0] < 0.6 * arms:
        return "alternating"
    if mag[0] >= 0.8 * arms and (mag.max() - mag.min()) / np.median(mag) < 0.15:
        return "aligned"
    return "unclear"


def polarity_text(polarity: str, mag: np.ndarray) -> str:
    ratio = mag[0] / np.median(mag[1:])
    return f"{polarity}: centre reads {ratio:.2f}x the arms' median"


def flat_press_checks(board: int, peak_dB: np.ndarray, noise_ut: np.ndarray,
                      polarity_rest: Optional[str]) -> (List[Check], dict):
    """peak_dB: (5, 3) each chip's dB at its own peak during a flat palm press.

    No polarity verdict here: the dBz sign pattern does not separate the two
    builds (the upstream aligned dataset presses mixed-sign), so it is only
    recorded."""
    checks = []
    mag = np.linalg.norm(peak_dB, axis=1)
    snr = mag / np.maximum(noise_ut, 1.0)
    weak = [i for i in np.where(snr < RESPONSE_MIN_SNR)[0]]
    names = ", ".join(CHIP_NAMES[i] for i in weak)
    if not weak:
        status, note = PASS, ""
    elif weak == [0] and polarity_rest == "alternating":
        # The centre sits on the alternating array's field null.
        status, note = WARN, ("middle barely moved; expected to be weakest with alternating "
                              "magnets, but build #1 still reached SNR 315. ")
    else:
        status, note = FAIL, f"{names} barely moved -- dead chip or missing magnet? "
    checks.append(Check("all chips respond", status, note + "peak |dB| uT: " + _fmt(mag), board))

    dz = peak_dB[:, 2]
    pattern = " ".join(f"{n}{'+' if z > 0 else '-'}" for n, z in zip(CHIP_NAMES, dz))
    centre = mag[0] / np.median(mag[1:])
    checks.append(Check("press profile", INFO, f"centre {centre:.2f}x the arms' median "
                        f"(build #1: 0.31 alternating, 0.46 aligned); dBz signs {pattern}",
                        board))
    return checks, {"sensitivity_ut": mag.tolist()}


def magnet_position_check(board: int, position: int, dB: np.ndarray) -> (Check, dict):
    """The magnet held over `position` on a bare board: which chip moved most?"""
    mag = np.linalg.norm(dB, axis=1)
    order = np.argsort(mag)[::-1]
    top, second = int(order[0]), int(order[1])
    margin = mag[top] / max(mag[second], 1e-9)
    name = CHIP_NAMES[position]
    if top != position:
        status, detail = FAIL, (f"magnet over {name}, but stream index {top} "
                                f"({CHIP_NAMES[top]}) moved most")
    elif margin < MAGNET_MARGIN:
        status, detail = WARN, (f"{name} moved most, only {margin:.2f}x ahead of "
                                f"{CHIP_NAMES[second]}; hold the magnet closer, right over it")
    else:
        status, detail = PASS, f"{name} moved most, {margin:.1f}x ahead of {CHIP_NAMES[second]}"
    detail += "  [|dB| uT: " + _fmt(mag) + "]"
    return Check(f"magnet over {name}", status, detail, board), {
        "responder": top, "margin": float(margin), "dB_ut": mag.tolist()}


def mapping_summary(board: int, responders: Dict[int, int]) -> Check:
    """responders: position -> stream index that moved most."""
    if len(responders) < MAGS_PER_FINGER:
        missing = [CHIP_NAMES[p] for p in range(MAGS_PER_FINGER) if p not in responders]
        return Check("position map", SKIP, f"not checked: {', '.join(missing)} -- needs the "
                     "magnet steps on the bare board", board)
    if all(p == k for p, k in responders.items()):
        return Check("position map", PASS, "stream order is middle, left, right, top, bottom", board)
    table = ", ".join(f"{CHIP_NAMES[p]} -> index {k}" for p, k in sorted(responders.items()))
    if sorted(responders.values()) == list(range(MAGS_PER_FINGER)):
        return Check("position map", FAIL, f"stream order is permuted: {table}. Fix the "
                     "address order in eflesh_cmd_stream's orderAddresses().", board)
    return Check("position map", FAIL, f"inconsistent: {table}. Repeat with the magnet "
                 "held right over each chip, about 5 mm up.", board)


def axis_checks(board: int, right_dB: np.ndarray, top_dB: np.ndarray) -> (List[Check], dict):
    """Mean dB (5, 3) with the magnet level to the right of the pad, then above
    the top edge, the same face toward the pad both times."""
    checks = []
    angles = {}
    for label, dB in (("right", right_dB), ("top", top_dB)):
        v = to_pad_frame(dB)                                   # (5, 2)
        units = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
        mean = units.sum(axis=0)
        mean_deg = _angle_deg(mean)
        dev = np.array([abs(_wrap(_angle_deg(u) - mean_deg)) for u in units])
        angles[label] = (mean_deg, dev)
        off = [f"{CHIP_NAMES[i]} {dev[i]:.0f} deg" for i in np.where(dev > AXIS_SPREAD_DEG)[0]]
        checks.append(Check(f"chips agree ({label})", FAIL if off else PASS,
                            (f"off from the rest: {', '.join(off)} -- that chip's rotation "
                             "is wrong. " if off else "")
                            + "per chip deg: " + _fmt([_angle_deg(u) for u in units]), board))

    right_deg = angles["right"][0]
    along_x = min(abs(_wrap(right_deg)), abs(_wrap(right_deg - 180)))
    checks.append(Check("x axis", PASS if along_x <= AXIS_TOL_DEG else FAIL,
                        f"magnet on the right gives {right_deg:.0f} deg, {along_x:.0f} deg off "
                        "the x axis" + ("" if along_x <= AXIS_TOL_DEG else
                                       " -- the pad frame is rotated"), board))
    turn = _wrap(angles["top"][0] - right_deg)
    if abs(turn - 90) <= AXIS_TOL_DEG:
        status, note = PASS, ""
    elif abs(turn + 90) <= AXIS_TOL_DEG:
        # The MLX90393 is right-handed with z out of its top face, and
        # to_pad_frame only rotates, so seen from the pad side +90 is expected.
        # The sign was never measured on hardware before, so flag, don't fail.
        status, note = WARN, (" -- mirrored: either the board was viewed from underneath "
                              "(left/right would then also be swapped), or the pad frame is "
                              "y-down. Record it in DEFAULT_CUBOID_SPECS.md 'Chip rotations'.")
    else:
        status, note = FAIL, " -- not a quarter turn; was the same magnet face toward the board?"
    checks.append(Check("handedness", status,
                        f"right to top turns {turn:+.0f} deg (expect +90){note}", board))
    return checks, {"right_deg": right_deg, "top_deg": angles["top"][0], "turn_deg": turn}


# ---------------------------------------------------------------- report

@dataclass
class Report:
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    firmware: str = ""
    port: str = ""
    settings: dict = field(default_factory=dict)
    boards: Dict[int, dict] = field(default_factory=dict)       # measurements per board
    steps: Dict[str, List[Check]] = field(default_factory=dict)  # step key -> its checks
    not_run: List[str] = field(default_factory=list)              # step titles never run

    def board(self, b: int) -> dict:
        return self.boards.setdefault(b, {})

    def set_step(self, key: str, checks: List[Check]) -> None:
        """Record a step's checks, replacing those of an earlier run of it."""
        self.steps[key] = list(checks)

    @property
    def checks(self) -> List[Check]:
        return [c for checks in self.steps.values() for c in checks]

    @property
    def verdict(self) -> str:
        statuses = {c.status for c in self.checks}
        if FAIL in statuses:
            return FAIL
        if WARN in statuses:
            return WARN
        if SKIP in statuses or self.not_run or PASS not in statuses:
            return INCOMPLETE
        return PASS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["verdict"] = self.verdict
        d["boards"] = {str(b): v for b, v in d["boards"].items()}
        return d

    def to_markdown(self) -> str:
        lines = [f"# eFlesh verification, {self.started}", "",
                 f"- firmware: {self.firmware}", f"- port: {self.port}",
                 f"- verdict: **{self.verdict}**", "",
                 "Settings: " + " ".join(f"{k}={v}" for k, v in self.settings.items()), ""]
        groups = [(f"Board {b}", b) for b in
                  sorted({c.board for c in self.checks if c.board is not None})]
        if any(c.board is None for c in self.checks):
            groups.append(("All boards", None))
        for title, b in groups:
            lines += [f"## {title}", "", "| check | result | detail |", "|---|---|---|"]
            for c in self.checks:
                if c.board == b:
                    lines.append(f"| {c.name} | {c.status} | {c.detail.replace('|', chr(92) + '|')} |")
            lines.append("")
        if self.not_run:
            lines += ["## Not run", ""] + [f"- {t}" for t in self.not_run] + [""]
        return "\n".join(lines)
