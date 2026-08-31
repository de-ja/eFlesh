#!/usr/bin/env python3
"""Recover eFlesh build specs from a finished sensor STL.

Connected-component analysis does NOT work on these meshes -- the cut-cell
lattice is not watertight and shatters into ~10^5 fragments. Instead we sweep
horizontal cross-sections: a magnet pouch shows up as an *interior ring* (an
enclosed void) in the slice polygon, and its bore diameter and z-extent fall
straight out.

    pip install trimesh shapely scipy rtree networkx
    python inspect_eflesh.py sensor.stl [--step 0.05]
"""
import argparse
import warnings

import numpy as np
import trimesh
from shapely.geometry import Polygon

warnings.filterwarnings("ignore")

# McMaster N52 magnets named in the eFlesh README: (thickness_mm, diameter_mm)
MAGNETS = {
    '5862K104  1/8" x 3/8"   (standard / medium-large)': (3.175, 9.525),
    '5862K139  1/16" x 3/16" (fingertip)': (1.5875, 4.7625),
}
MIN_BORE = 2.0  # mm; ignore interior rings smaller than this (lattice noise)


def slice_at(mesh, z):
    """Return (total_area, [interior rings]) at height z, in mesh coordinates."""
    sec = mesh.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    if sec is None:
        return 0.0, []
    planar, _ = sec.to_planar(to_2D=np.eye(4))  # identity => planar xy == mesh xy
    rings = []
    for poly in planar.polygons_full:
        for interior in poly.interiors:
            ring = Polygon(interior)
            x0, y0, x1, y1 = ring.bounds
            width = x1 - x0
            if width >= MIN_BORE:
                c = ring.centroid
                rings.append({"x": c.x, "y": c.y, "d": width,
                              "round": (y1 - y0) / width})
    return sum(p.area for p in planar.polygons_full), rings


def cell_size(mesh):
    """Lattice period per axis, via autocorrelation of vertex density."""
    out = []
    for axis in range(3):
        col = mesh.vertices[:, axis]
        hist, _ = np.histogram(col, bins=int((col.max() - col.min()) / 0.1))
        h = hist - hist.mean()
        ac = np.correlate(h, h, "full")[len(h) - 1:]
        ac = ac / ac[0]
        best, best_score = None, 0.2
        for i in range(20, min(len(ac) - 1, 300)):  # ignore lags under 2 mm
            if ac[i] > ac[i - 1] and ac[i] > ac[i + 1] and ac[i] > best_score:
                best, best_score = i * 0.1, ac[i]
        out.append((best, best_score))
    return out


def match_magnet(diameter, depth):
    best, best_err = None, 1e9
    for name, (t, d) in MAGNETS.items():
        if diameter < d:            # magnet physically cannot fit
            continue
        err = (diameter - d) + abs(depth - t)
        if err < best_err:
            best, best_err = name, err
    return (best, best_err) if best_err < 1.5 else (None, best_err)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("--step", type=float, default=0.05)
    args = ap.parse_args()

    mesh = trimesh.load(args.stl, force="mesh", process=False)
    lo, hi = mesh.bounds
    print(f"file      : {args.stl}")
    print(f"triangles : {len(mesh.faces):,}")
    print(f"extents   : {mesh.extents[0]:.2f} x {mesh.extents[1]:.2f} "
          f"x {mesh.extents[2]:.3f} mm")
    print(f"origin    : [{lo[0]:.2f}, {lo[1]:.2f}, {lo[2]:.2f}] -> "
          f"[{hi[0]:.2f}, {hi[1]:.2f}, {hi[2]:.2f}]")
    print(f"solid vol : {abs(mesh.volume) / 1000:.2f} cm^3 "
          f"({100 * abs(mesh.volume) / np.prod(mesh.extents):.1f}% of bbox)")
    print(f"TPU mass  : ~{abs(mesh.volume) / 1000 * 1.21:.1f} g at 1.21 g/cm^3")

    for name, (period, score) in zip("xyz", cell_size(mesh)):
        print(f"cell size {name}: {period} mm (autocorr {score:.2f})"
              if period else f"cell size {name}: no clear period")

    footprint = mesh.extents[0] * mesh.extents[1]
    zs = np.arange(lo[2], hi[2], args.step)

    print("\n--- solid skin (100% fill slices) ---")
    solid = [z for z in zs if slice_at(mesh, z)[0] > 0.98 * footprint]
    if solid:
        print(f"  z {min(solid):.2f} -> {max(solid):.2f} mm "
              f"({max(solid) - min(solid) + args.step:.2f} mm thick)")
    else:
        print("  none - open lattice top to bottom")

    print("\n--- enclosed cavities (magnet pouches) ---")
    tracks = {}   # (x, y) rounded -> list of (z, diameter)
    for z in zs:
        for r in slice_at(mesh, z)[1]:
            tracks.setdefault((round(r["x"], 1), round(r["y"], 1)), []).append(
                (z, r["d"], r["round"]))

    pouches = [(k, v) for k, v in tracks.items() if len(v) * args.step > 1.0]
    if not pouches:
        print("  none found")
        return
    pouches.sort(key=lambda kv: (kv[0][1], kv[0][0]))

    for i, ((x, y), samples) in enumerate(pouches, 1):
        diams = np.array([s[1] for s in samples])
        zvals = np.array([s[0] for s in samples])
        bore = diams.max()
        in_bore = zvals[diams > bore - 0.2]
        z0, z1 = in_bore.min(), in_bore.max() + args.step
        depth = z1 - z0
        name, err = match_magnet(bore, depth)
        print(f"\n[{i}] center ({x:.2f}, {y:.2f})   bore Ø{bore:.2f} mm   "
              f"z {z0:.2f} -> {z1:.2f} ({depth:.2f} mm deep)")
        necks = sorted({round(d, 2) for d in diams if d < bore - 0.2})
        if necks:
            nz = zvals[diams < bore - 0.2]
            print(f"    retention neck Ø{max(necks):.2f} mm, "
                  f"z {nz.min():.2f} -> {nz.max() + args.step:.2f}")
        print(f"    magnet: {name or 'no match in README list'}")
        if name:
            t, d = MAGNETS[name]
            print(f"    fit   : {bore - d:+.3f} mm diametral clearance, "
                  f"magnet sits {t - depth:+.3f} mm proud of the bore")
        print(f"    create_pouch.py -> [{bore:.3f}, {depth:.3f}, "
              f"[{x:.3f}, {y:.3f}, {(z0 + z1) / 2:.3f}]]")

    xs = sorted({p[0][0] for p in pouches})
    ys = sorted({p[0][1] for p in pouches})
    print(f"\nsummary: {len(pouches)} pouches")
    if len(xs) > 1:
        print(f"  x pitch {np.diff(xs)[0]:.2f} mm, columns at {xs}")
    if len(ys) > 1:
        print(f"  y pitch {np.diff(ys)[0]:.2f} mm, rows at {ys}")
    cx, cy = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    ax, ay = np.mean(xs), np.mean(ys)
    print(f"  array center ({ax:.2f}, {ay:.2f}) vs part center ({cx:.2f}, {cy:.2f})"
          f"  offset ({ax - cx:+.2f}, {ay - cy:+.2f}) mm")


if __name__ == "__main__":
    main()
