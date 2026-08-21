#!/usr/bin/env python3
"""Render every video frame of a cast in parallel."""
import json, os, sys
from multiprocessing import Pool
from render import Player, render

CAST = OUT = TITLE = None
FPS = 12.0


def worker(rng):
    cast = json.load(open(CAST))
    p = Player(cast)
    for k in range(*rng):
        p.advance_to(k / FPS)
        render(p.screen, p.cols, p.rows, TITLE).save(f"{OUT}/f{k:05d}.png")
    return rng


def main():
    global CAST, OUT, TITLE, FPS
    CAST, OUT, TITLE = sys.argv[1], sys.argv[2], sys.argv[3]
    FPS = float(sys.argv[4]) if len(sys.argv) > 4 else 12.0
    os.makedirs(OUT, exist_ok=True)
    cast = json.load(open(CAST))
    dur = Player(cast).duration + 1.5
    n = int(dur * FPS)
    nw = min(8, os.cpu_count() or 4)
    chunk = (n + nw - 1) // nw
    ranges = [(i, min(i + chunk, n)) for i in range(0, n, chunk)]
    with Pool(nw) as pool:
        pool.map(worker, ranges)
    print(f"{n} frames @ {FPS}fps ({dur:.1f}s)")


if __name__ == "__main__":
    main()
