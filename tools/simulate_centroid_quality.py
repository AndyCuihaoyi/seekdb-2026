#!/usr/bin/env python3
"""Simulate the two ways of choosing the centroids, and their effect.

The approximate query with a filter is much slower than it was in the
version that passed the admission. The scan did not change, so the
change is in the **centroids**.

Two ways of choosing them are compared on the same data:

* the way of the version that passed the admission: the farthest point
  sampling runs over **every row** of the table;
* the way of the current version: the sampling runs over a **sample of
  8192 rows**, so that the build does not grow with the size of the
  table.

For each of them the simulation reports the numbers that decide the
cost of the approximate scan:

* the radius of every cell, and the largest of them: the scan prunes a
  cell by its radius, so a large radius means that the cell cannot be
  pruned;
* the number of cells the scan visits and the number of rows it
  measures, for a query with a filter that keeps one row in eight.

The third way is the fix that is proposed: the farthest point sampling
is followed by a few rounds of Lloyd's refinement over the sample. It
costs the same as the sampling itself, because it also runs over the
sample, and it makes the cells much tighter.

Usage:
    tools/simulate_centroid_quality.py [--rows N] [--dim D] [--nlist N]
                                       [--sample N] [--iters N]
"""

import argparse
import json
import numpy as np


def make_data(rows, dim):
    """The data of the benchmark: the same shape the tools write."""
    row = np.arange(rows, dtype=np.float64)
    out = np.empty((rows, dim), dtype=np.float64)
    for i in range(dim):
        out[:, i] = (row * (i + 1)) % 100
    return out


def fps_centroids(data, nlist, rng):
    """The farthest point sampling, as the database runs it."""
    n = data.shape[0]
    first = 0
    centroids = [data[first].copy()]
    best = ((data - centroids[0]) ** 2).sum(axis=1)
    while len(centroids) < nlist:
        pick = int(np.argmax(best))
        if best[pick] <= 0:
            centroids.extend(
                [np.zeros(data.shape[1]) for _ in range(nlist - len(centroids))]
            )
            break
        centroid = data[pick].copy()
        centroids.append(centroid)
        d = ((data - centroid) ** 2).sum(axis=1)
        best = np.minimum(best, d)
    return np.array(centroids)


def lloyd_refine(data, centroids, iters):
    """A few rounds of Lloyd's algorithm over the sample."""
    for _ in range(iters):
        # The assignment of every row of the sample to its closest
        # centroid, and the sum of the distances.
        assign = np.argmin(
            ((data[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2),
            axis=1,
        )
        new = np.zeros_like(centroids)
        counts = np.zeros(centroids.shape[0], dtype=np.int64)
        np.add.at(new, assign, data)
        np.add.at(counts, assign, 1)
        # A cell that took no row of the sample keeps its centroid: a
        # sample is a sample, and a cell can be empty in it and not in
        # the table.
        keep = counts > 0
        new[keep] = new[keep] / counts[keep][:, None]
        new[~keep] = centroids[~keep]
        centroids = new
    return centroids


def assign_all(data, centroids):
    """The cell of every row of the table, and the radius of every cell."""
    assign = np.argmin(
        ((data[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2),
        axis=1,
    )
    radii = np.zeros(centroids.shape[0])
    for cell in range(centroids.shape[0]):
        rows = data[assign == cell]
        if rows.shape[0]:
            radii[cell] = np.sqrt(((rows - centroids[cell]) ** 2).sum(axis=1)).max()
    return assign, radii


def scan_cost(data, assign, radii, centroids, query, k, keep_every):
    """The number of cells a filtered scan visits and the rows it measures.

    The scan is the one of the database: the cells are visited in the
    order of their distance to the query, a cell is skipped when its
    radius proves that it holds nothing better than the K-th accepted
    row, and the filter is applied inside the cell.
    """
    centroid_dists = np.sqrt(((centroids - query) ** 2).sum(axis=1))
    order = np.argsort(centroid_dists)
    max_radius = radii.max()

    best = []
    visited = 0
    measured = 0
    for cell in order:
        cd = centroid_dists[cell]
        if len(best) >= k and cd - max_radius > best[-1][0]:
            break
        if len(best) >= k and cd - radii[cell] > best[-1][0]:
            continue
        visited += 1
        rows = np.where(assign == cell)[0]
        d = np.sqrt(((data[rows] - query) ** 2).sum(axis=1))
        keep = (rows % keep_every) == 3
        for dist in d[keep]:
            measured += 1
            best.append((dist, rows[keep][0]))
            best.sort()
            best = best[:k]
    return visited, measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--nlist", type=int, default=64)
    parser.add_argument("--sample", type=int, default=8192)
    parser.add_argument("--iters", type=int, default=5)
    parser.add_argument("--keep-every", type=int, default=8)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    data = make_data(args.rows, args.dim)
    rng = np.random.default_rng(0)
    query = np.array([i % 100 for i in range(args.dim)], dtype=np.float64)

    # The sample the database takes: a reservoir over the rows.
    sample_idx = rng.choice(args.rows, size=min(args.sample, args.rows), replace=False)
    sample_idx.sort()
    sample = data[sample_idx]

    report = {"args": vars(args), "ways": []}

    ways = [
        ("scoring: fps over every row", fps_centroids(data, args.nlist, rng)),
        ("current: fps over the sample", fps_centroids(sample, args.nlist, rng)),
    ]
    refined = lloyd_refine(sample, fps_centroids(sample, args.nlist, rng), args.iters)
    ways.append(("fix: fps over the sample + lloyd", refined))

    for name, centroids in ways:
        assign, radii = assign_all(data, centroids)
        visited, measured = scan_cost(
            data, assign, radii, centroids, query, 10, args.keep_every
        )
        entry = {
            "way": name,
            "max_radius": round(float(radii.max()), 3),
            "radius_sum": round(float(radii.sum()), 3),
            "radius_mean": round(float(radii.mean()), 3),
            "largest_cell": int(np.bincount(assign, minlength=args.nlist).max()),
            "cells_visited": int(visited),
            "rows_measured": int(measured),
        }
        report["ways"].append(entry)
        print(
            "%-38s max_radius %9.3f  radius_sum %11.3f  largest_cell %7d  "
            "cells_visited %3d  rows_measured %9d"
            % (
                name,
                entry["max_radius"],
                entry["radius_sum"],
                entry["largest_cell"],
                entry["cells_visited"],
                entry["rows_measured"],
            )
        )

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
