# eFlesh Default Cuboid — Measured Build Specs

Specs for `eFlesh-DefaultCuboid.stl`, from the [eFlesh STLs Google Drive folder](https://drive.google.com/drive/folders/171oadIm0CILk12-vTXybU6pnAGJ5Sp9b)
linked off [e-flesh.com](https://e-flesh.com).

The authors publish no spec sheet, BOM, or assembly notes with the STLs. Everything
below was **measured from the mesh**, not read from documentation. Reproduce it with
`inspect_eflesh.py` (see [Provenance](#provenance)).

Measured 2026-08-19. All dimensions in mm, in the STL's own coordinate frame.

---

## Body

| Property | Value |
|---|---|
| Lattice body | **40.33 × 40.33** × 23.70 (z = 0.62 → 24.32) |
| Base flange | **43.00 × 43.00** × 0.62 (z = 0 → 0.62) |
| Overall bounding box | 43.00 × 43.00 × 24.324, `[0.00, 18.00, 0.00]` → `[43.00, 61.00, 24.32]` |
| Lattice cell size | **8.0** (autocorrelation period on all three axes) |
| Solid volume | 6.46 cm³ — 14.4% of bounding box |
| TPU required | ~7.8 g at 1.21 g/cm³ |
| Bottom skin | solid 100%-fill slab, z = 0 → 0.62, **oversized to 43.00 square** |
| Sides and top | open lattice, no perimeter wall |

**The part is 40 mm, not 43 mm.** The 43.00 square is the base slab alone; every
cross-section above z = 0.62 measures 40.33. The slab overhangs the body by 1.335 mm on
all four sides, forming a lip — consistent with a bonding surface for mounting to a
holder, though the STL records only the geometry, not the intent. Quoting 43 mm as the
sensor's size (as an earlier revision of this file did) measures the flange and matches
neither the paper nor the lattice.

The 8.0 mm cell size matches the `cell_size = 8` default in
[`cut-cell.ipynb`](microstructure/microstructure_inflators/cut-cell.ipynb). At 43 mm the
part is 5.375 cells across, i.e. the lattice is trimmed to the input surface rather than
snapped to whole cells — expected for the cut-cell pipeline.

## Magnets

**4 magnets, all identical: McMaster [5862K104](https://www.mcmaster.com/5862K104/)** —
N52 neodymium, 1/8" thick × 3/8" diameter (3.175 × 9.525 mm). This is the part the
[README](README.md) names for the standard cuboid, and the pouch geometry confirms it.

### Pouch profile

Each pouch is a stepped pocket, not a plain hole:

| Feature | Diameter | z range | Height |
|---|---|---|---|
| Bore | Ø10.00 | 10.09 → 13.11 | 3.02 |
| Retention neck | Ø8.00 | 13.11 → 13.93 | 0.82 |
| Roof closes | — | 13.99 | — |

The step is load-bearing design, so don't flatten it if you remodel:

- The magnet is Ø9.525 and the neck is Ø8.00, so the magnet **cannot escape upward**.
- The magnet is 3.175 thick in a 3.02 bore, so it stands **0.125–0.155 proud**. The
  printer lays the first neck layer directly onto the magnet's rim and squashes it.
- Radial clearance is 0.24 (0.48 diametral) — a slip fit going in.

### Positions

| # | x | y | z (bore center) |
|---|---|---|---|
| 1 | 13.50 | 29.50 | 11.60 |
| 2 | 29.50 | 29.50 | 11.60 |
| 3 | 13.50 | 45.50 | 11.60 |
| 4 | 29.50 | 45.50 | 11.60 |

Pitch is 16.00 on both axes.

> **The array is not centered.** Array center is (21.50, 37.50); part center is
> (21.50, 39.50) — a **2.00 mm offset in −y**. Preserve it to match the published
> sensor's response, and account for it when aligning the PCB.

In [`create_pouch.py`](microstructure/README.md) format — `[diameter, thickness, [cx, cy, cz]]`:

```python
list_of_magnets = [
    [10.0, 3.02, [13.5, 29.5, 11.60]],
    [10.0, 3.02, [29.5, 29.5, 11.60]],
    [10.0, 3.02, [13.5, 45.5, 11.60]],
    [10.0, 3.02, [29.5, 45.5, 11.60]],
]
```

Note this reproduces the bore only — `create_pouch.py` cuts a plain cylinder, so the
Ø8.00 retention neck has to be added separately.

## No magnetometer slot

**This STL has no PCB pocket.** A full-height sweep found the four magnet pouches as the
*only* enclosed cavities in the part, and the base is an unbroken 0.62 mm skin.

The cuboid therefore mounts **on top of** the magnetometer board, as AnySkin and ReSkin
skins do. You need your own holder or adhesive; nothing in the download covers mounting.

## Electronics

| Property | Value |
|---|---|
| Magnetometers | 5 × MLX90393 (ReSkin / AnySkin rigid PCB) |
| I²C addresses | `0x0C, 0x0D, 0x0E, 0x0F, 0x10` (alt set: `0x0C, 0x10, 0x11, 0x12, 0x13`) |
| Microcontroller | Adafruit QT Py (Trinket M0 also supported) |
| Firmware | [`arduino/eflesh_cmd_stream/eflesh_cmd_stream.ino`](arduino/eflesh_cmd_stream/eflesh_cmd_stream.ino) (default settings) |
| Stream width | 15 channels — 5 sensors × 3 axes |

5 magnetometers read 4 magnets.

## Printing

Per the [CAD2eFlesh docs](microstructure/README.md): TPU 95A, Bambu X1C, 0.4 mm nozzle,
no supports needed for the lattice.

**Magnet insertion pause:** pause at the first layer at or above **z = 13.11** — the top
of the Ø10 bore, where it steps down to Ø8. At 0.2 mm layers that is the layer starting
at 13.2. Insert all four magnets, then resume.

**Polarity: depends on whether this sensor will sit near other eFlesh sensors.** The
paper ([arXiv:2506.09994](https://arxiv.org/abs/2506.09994)) gives two arrangements, and
they are not in conflict.

*Baseline, §4.1 Fabrication of eFlesh:*

> the user inserts magnets into the pouch packets, with the north poles facing upwards
> in this case, but in general, polarities may be altered based on the considerations
> outlined in the previous sections

*Modification, §2.7.1 Effect of magnetic interference:*

> To address this issue, we introduce a simple yet effective modification to the eFlesh
> design: the embedded magnets are arranged in alternating instead of aligned polarities

| | Baseline (§4.1) | Alternating (§2.7.1) |
|---|---|---|
| Arrangement | all four north-up | two north-up, two north-down |
| In a 2x2 array | — | same-polarity pairs fall on a diagonal |
| Purpose | default build | cut stray field between *separate* sensor units |
| Reported effect | — | ~2 orders of magnitude less stray-field intensity |

**Which to use.** A single sensor working alone: north-up aligned, per §4.1. Several
eFlesh sensors mounted close together — a dexterous hand, adjacent fingertips: alternate
them, which is what §2.7.1 exists for. Note the target is interference *between sensor
units*, not between the five magnetometers inside one cuboid, so alternating buys
nothing for a lone sensor.

In a 2x2 array "alternating" forces the checkerboard: each magnet opposes both edge
neighbours, so the two north-up magnets land on one diagonal. Which diagonal is a free
choice -- the options are the same sensor mirrored.

Polarity is not measurable from the STL, which encodes four identical pouches and no
magnetization data. This section is sourced entirely from the paper, unlike the geometry
above.

---

## Verifying an assembled cuboid

Measured on the first build, 2026-09-02. These are procedures for checking the
next cuboid, not just facts about this one. All are non-destructive -- the
magnets are sealed at z = 13.99 and cannot be inspected once printed.

### Channel to chip position

Not documented upstream. Recovered from `visualizer/viz_eflesh.py`'s
`chip_locations`, which is indexed by channel, and confirmed with a handheld
magnet on a bare board:

| channel | address | position |
|---|---|---|
| 0 | `0x0C` | middle |
| 1 | `0x11` | left |
| 2 | `0x12` | right |
| 3 | `0x13` | top |
| 4 | `0x10` | bottom |

Channel 0 being the centre is corroborated by the ReSkin BOM: `0x0C` is the only
`MLX90393ELW-ABA-011-SP`, the other four being `-012-SP`. It also reads the
lowest resting field, as the centre should.

This is for the **white** address variant. A board reporting
`0x0C, 0x0D, 0x0E, 0x0F, 0x10` has not been mapped.

### Two checks for alternating polarity

Both work on a sealed part.

1. **Resting field.** With the cuboid seated and nothing touching it, the centre
   chip should read distinctly the weakest -- four magnets cancelling at the
   centre. Aligned magnets would instead sum there and make it strongest.
2. **Flat press.** Press the whole top face with a palm or flat plate and watch
   the circles in `viz_eflesh.py`. Alternating polarity gives a *mixed* pattern
   of filled and hollow circles under one uniform press, because filled/hollow
   encodes the sign of dBz and that sign follows the nearby magnet's direction.
   Aligned magnets give all-filled or all-hollow.

### Reference profile, build #1

Resting field, cuboid seated, nothing touching:

| chip | position | \|B\| (uT) |
|---|---|---|
| 0 | middle | 712 |
| 1 | left | 1839 |
| 2 | right | 1911 |
| 3 | top | 1255 |
| 4 | bottom | 2230 |

Ambient with no magnets present is 40-100 uT, so these are 20-40x ambient.
Nothing saturates at `setGainSel(0x1)`, and per-channel noise is ~2.5 uT.

**The arms are not equal, and by symmetry they should be.** A 2x2 alternating
array is invariant under 90-degree rotation plus a polarity flip, so all four
edge chips ought to read the same magnitude. The observed spread is 1.8x
(1255 to 2230), and it reproduces across reseating, so it is a property of the
build rather than mounting chance. Candidate causes: PCB offset from the array
centre (remember the array sits 2.00 mm -y of the part centre), magnet seating
depth variation, or unit variation in the magnets or chips.

**It has no functional consequence.** See the press response below: the arm
with the weakest resting field responds as strongly as the others. The
asymmetry is a static offset, which baseline subtraction removes. Still worth
measuring on the next build -- two cuboids is enough to tell a fixed jig error
from per-part variation -- but it does not need fixing before the sensor is
usable.

### Press response, build #1

Flat palm press over the whole top face, peak dB against a 2 s baseline,
5555 frames. Resting values differ slightly from the table above because the
cuboid was reseated; reseating shifts the field by a few percent.

| chip | position | resting (uT) | noise (uT) | peak \|dB\| (uT) | SNR |
|---|---|---|---|---|---|
| 0 | middle | 823 | 3.0 | 955 | 315 |
| 1 | left | 1972 | 3.1 | 2523 | 814 |
| 2 | right | 1964 | 3.1 | 3220 | 1033 |
| 3 | top | 1268 | 3.8 | 3176 | 826 |
| 4 | bottom | 2327 | 3.3 | 3081 | 944 |

Two findings.

**The centre chip is about 3x less responsive than the arms.** The four arms
cluster inside a 1.28x spread (2523 to 3220) while the centre reaches 955. An
uneven palm press would scatter the arms too; they are tight, so the gap is
the field null at the centre, not the press. This is the measured cost of
alternating polarity, and it is a cost in *response*, not just in resting
field -- 3.1x versus the 2.3x deficit in the resting value.

**It does not matter at press amplitudes, and might at slip amplitudes.**
Centre SNR is 315 against a 3 uT noise floor: two orders of magnitude of
headroom, so normal-force sensing and contact localisation are unaffected.
Small-amplitude signals are the exposure. A perturbation at 1% of a press is
~10 uT at the centre against ~30 uT at the arms, SNR 3 versus 10, so the
centre's threshold for faint contact is ~3x higher. Slip is exactly that kind
of signal. Alternating polarity does not break slip detection, but the centre
chip contributes a weaker vote on the faintest events.

Note also that no frame in 5555 was corrupt, with firmware that does not check
`readBurstData()` status at all. Bad reads are not a property of these chips.

### Aligned vs alternating, measured

Same board, same press protocol, a second cuboid built with all four magnets
north-up.

| chip | resting (uT) | noise (uT) | peak \|dB\| (uT) | SNR |
|---|---|---|---|---|
| middle | 13663 | 11.6 | 4308 | 372 |
| left | 14377 | 13.0 | 11290 | 871 |
| right | 14598 | 10.8 | 10697 | 988 |
| top | 14236 | 16.5 | 5163 | 313 |
| bottom | 14668 | 9.7 | 8087 | 833 |

Means against the alternating build:

| | alternating | aligned | ratio |
|---|---|---|---|
| resting | 1671 | 14308 | 8.6x |
| peak \|dB\| | 2591 | 7909 | 3.05x |
| noise | 3.3 | 12.3 | 3.78x |
| **SNR** | **786** | **675** | **0.86x** |

**Aligned is about 3x more sensitive.** Signal rises 3x and the noise floor is
unchanged, so the SNR ratio is just the signal ratio.

> **Superseded reading.** The press run above measured 9.7-16.5 uT noise on the
> aligned build and 3.0-3.8 uT on the alternating one, which suggested noise
> scaled with field magnitude and that SNR was a wash. That does not reproduce.
> Two later 3 s windows on both boards, with the aligned cuboid still at 14 mT,
> give 2.4-3.2 uT. The inflated figure came from `press.py` taking its baseline
> three seconds after the cuboid was handled and mounted -- thermal and
> mechanical settling widening the standard deviation. **Measure noise only
> after ~30 s of settling.**

With XY quantisation at 2.4 uT/LSB, both builds sit at roughly one LSB: they
are quantisation-limited, not physics-limited, and share a noise floor.

| | alternating | aligned |
|---|---|---|
| mean peak \|dB\| | 2591 uT | 7909 uT |
| noise floor | ~2.5 uT | ~2.5 uT |
| **SNR** | **~1040** | **~3160** |

**Aligned does not make the centre chip strong either.** All five resting
fields land within 7% at ~14 mT: the centre is 11.3 mm from every magnet while
the arms are 8.1 mm from their two nearest, and that distance penalty cancels
the four-versus-two advantage. Centre SNR is 372 aligned against 315
alternating -- 18% apart, not 3x.

So most of the centre's weakness is **geometric, not polarity**. A 1/r^4
gradient argument predicts the centre responds 1.9x less than the arms; the
aligned build measures 2.0x. The alternating build measures 3.1x, so the null
adds ~1.5x on top of geometry, not 3x. Revise the section above accordingly:
the null is real but secondary.

**Conclusion: unresolved, and it depends on a number not yet measured.**
Alternating costs a real 3x in SNR, and buys far-field isolation between
neighbouring sensors. Whether that is a good trade depends on how much two
cuboids actually pollute each other at working separation, which has not been
measured. If crosstalk is negligible at gripper spacing, prefer aligned and
take the 3x. If it is significant, alternating earns its cost.

To measure it: two boards behind an I2C mux, cuboids at a known separation,
hold one still and press the other, and look for signal on the stationary one.
Repeat at several distances. Note that the boards must be *deliberately*
placed -- an incidental bench layout tells you nothing about gripper spacing.

Unexplained: `top` has the weakest resting field in the alternating build but
responds normally there (SNR 826), while in the aligned build it responds worst
(SNR 313). Different cuboids, same board, no consistent story. Revisit only if
it recurs on a third build.

### Full scale and quantisation

From the library, with the sketch's `setGainSel(0x1)` (x4),
`setResolution(0x2,0x2,0x2)` (x4) and the chip's default `HALLCONF` 0xC
(0.150 uT XY, 0.242 uT Z base sensitivity):

- **1 LSB = 2.4 uT on XY, 3.87 uT on Z**
- **Full scale = +/-78.6 mT on XY, +/-126.9 mT on Z**

Even the aligned build, at 14 mT resting with 11 mT excursions, uses about a
third of XY range. Saturation is not a concern for either polarity.

This also identifies the corrupt reads seen during ESP32-C3 bring-up:
`78640.8 uT` is exactly `(65535 - 32768) * 2.4`, i.e. raw `0xFFFF`. Those were
failed I2C reads returning all ones, not chip noise.

### Chip rotations

`chip_xy_rotations` in `viz_eflesh.py` maps each chip's in-plane axes into
screen coordinates. Upstream ships two rows, "black" and "white", with nothing
saying which applies to a given board. **The black row is correct for this
build**, verified with a handheld magnet on a bare board:

- held to one side of a chip, the arrow points along the chip-magnet line --
  the in-plane field of a vertical magnet is radial;
- directly overhead, the arrow collapses to zero while the circle peaks, since
  the radial component is proportional to lateral offset;
- all five chips behave identically.

The white row reads 90 degrees off on channels 1-4. The two rows already agree
on channel 0 (`-pi/2` and `3pi/2` are the same angle), so the centre never
differed between them. Note the row is unrelated to the board's address variant
despite the shared "white" label.

---

## Provenance

Measured with `inspect_eflesh.py`, which sweeps horizontal cross-sections and detects
enclosed rings in each slice polygon.

Connected-component analysis does **not** work on these meshes: the cut-cell lattice is
not watertight and splits into ~10⁵ fragments, so component-based cavity detection
returns tens of thousands of false pouches. Slice sweeping is the reliable approach.

```
pip install trimesh shapely scipy rtree networkx
python inspect_eflesh.py eFlesh-DefaultCuboid.stl --step 0.05
```

Figures quoted here come from a 0.02 mm sweep near the transitions; the 0.05 mm default
puts boundaries within about 0.04 mm of these values.
