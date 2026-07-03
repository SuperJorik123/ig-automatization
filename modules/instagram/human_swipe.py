"""
human_swipe.py

A swipe simulator whose distributions are fit to the real getevent trace
analyzed by `analyze_swipes.py`. Reads `swipe_stats.json` produced by that
script.

Design goals:
  - Tight: synthetic samples are kept inside the observed range of every
    parameter (rejection-truncated multivariate normal).
  - Minimal randomness: only 5 quantities are sampled per swipe
    (x_start, y_start, dx, dy, duration_ms). Everything else is derived
    deterministically:
      - n_points     = round(duration_ms / sample_period_ms) + 1
      - arc_apex     = a * straight_length + b   (linear fit to data)
      - bow shape    = sin(s * pi) * arc_apex
      - point timing = inverse-CDF of empirical velocity profile
  - Faithful to correlations: the 5-D multivariate normal preserves the
    measured correlations (e.g., longer flicks have larger dy and dx).

Public API:
    generate_human_swipe(rng=None) -> (points, duration_seconds)
    visualize_comparison(n_synth=47, save_path="comparison.png")

Run:
    python human_swipe.py
"""

import json
import math
import os
import sys

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

HERE = os.path.dirname(os.path.abspath(__file__))
STATS_PATH = os.path.join(HERE, "swipe_stats.json")


# --------------------------------------------------------------------------- #
# Load empirical stats and fit the model                                      #
# --------------------------------------------------------------------------- #

def _load_stats():
    if not os.path.exists(STATS_PATH):
        sys.exit(
            f"Could not find {STATS_PATH}. "
            "Run `python analyze_swipes.py` first."
        )
    with open(STATS_PATH, "r") as f:
        return json.load(f)


_STATS = _load_stats()


def _fit_model(stats):
    xs = np.asarray(stats["x_start_px"])
    ys = np.asarray(stats["y_start_px"])
    xe = np.asarray(stats["x_end_px"])
    ye = np.asarray(stats["y_end_px"])
    dx = xe - xs
    dy = ye - ys
    dur = np.asarray(stats["duration_ms"])
    n = np.asarray(stats["n_points"], dtype=float)
    arc = np.asarray(stats["arc_apex_signed_px"])
    straight = np.asarray(stats["straight_length_px"])

    # 5-D joint multivariate normal: (x_start, y_start, dx, dy, duration_ms)
    data = np.stack([xs, ys, dx, dy, dur], axis=1)
    mean = data.mean(axis=0)
    cov = np.cov(data, rowvar=False)
    lo = data.min(axis=0)
    hi = data.max(axis=0)

    # Sampling rate inferred from (n, dur)
    sample_rate_hz = (n.mean() - 1) / (dur.mean() / 1000.0)
    sample_period_ms = 1000.0 / sample_rate_hz

    # Linear fit: arc_apex = a * straight + b
    a = float(np.cov(straight, arc, ddof=0)[0, 1] / np.var(straight))
    b = float(arc.mean() - a * straight.mean())

    # Use the median per-swipe jitter (a touch lower than the mean), so
    # synthetic curves don't look noisier than the real ones in the plot.
    jitter_x = float(np.median(stats["jitter_x_px"]))
    jitter_y = float(np.median(stats["jitter_y_px"]))

    return {
        "mean": mean,
        "cov": cov,
        "lo": lo,
        "hi": hi,
        "arc_a": a,
        "arc_b": b,
        "arc_min": float(arc.min()),
        "arc_max": float(arc.max()),
        "sample_period_ms": sample_period_ms,
        "n_min": int(n.min()),
        "n_max": int(n.max()),
        "velocity_profile": np.asarray(stats["avg_norm_velocity_profile"]),
        "jitter_x": jitter_x,
        "jitter_y": jitter_y,
        "screen_w": stats["screen"]["w"],
        "screen_h": stats["screen"]["h"],
    }


_MODEL = _fit_model(_STATS)


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #

def _sample_truncated_mvn(rng, mean, cov, lo, hi, max_tries=200):
    """Sample from a multivariate normal, rejecting any draw that falls
    outside the per-dimension [lo, hi] box. Falls back to clipping if we
    can't draw an in-range sample after `max_tries` attempts.
    """
    for _ in range(max_tries):
        s = rng.multivariate_normal(mean, cov)
        if np.all(s >= lo) and np.all(s <= hi):
            return s
    return np.clip(rng.multivariate_normal(mean, cov), lo, hi)


def generate_human_swipe(rng=None):
    """Generate one synthetic Reels-style upward swipe.

    Returns:
        points (list of (float, float)): screen-px (x, y) samples, ordered
            from finger-down to finger-up. Sample times are uniform in
            wall-clock time over `duration_seconds`.
        duration_seconds (float).
    """
    if rng is None:
        rng = np.random.default_rng()

    # 1) Sample the five free parameters jointly; truncate to observed box
    s = _sample_truncated_mvn(
        rng, _MODEL["mean"], _MODEL["cov"], _MODEL["lo"], _MODEL["hi"]
    )
    x_start, y_start, dx, dy, dur_ms = s

    # 2) Derive everything else deterministically
    x_end = x_start + dx
    y_end = y_start + dy
    L = math.hypot(dx, dy)
    if L < 1.0:
        L = 1.0
    arc_apex = _MODEL["arc_a"] * L + _MODEL["arc_b"]
    arc_apex = float(np.clip(arc_apex, _MODEL["arc_min"], _MODEL["arc_max"]))

    n_points = int(round(1 + dur_ms / _MODEL["sample_period_ms"]))
    n_points = int(np.clip(n_points, _MODEL["n_min"], _MODEL["n_max"]))

    # 3) Build the path
    ux, uy = dx / L, dy / L
    # Perpendicular pointing in the empirically observed bow direction.
    # In the trace every swipe bowed in the direction (uy, -ux) — up-and-left
    # of the straight line, consistent with a right thumb pivoting at the
    # bottom-right corner.
    nx, ny = uy, -ux

    profile = _MODEL["velocity_profile"]
    profile_t = np.linspace(0.0, 1.0, len(profile))
    cum = np.cumsum(profile)
    cum_norm = cum / cum[-1]

    points = []
    for i in range(n_points):
        t_norm = i / (n_points - 1) if n_points > 1 else 0.0
        s_along = float(np.interp(t_norm, profile_t, cum_norm))
        x = x_start + dx * s_along
        y = y_start + dy * s_along
        bow = math.sin(s_along * math.pi) * arc_apex
        x += bow * nx
        y += bow * ny
        x += rng.normal(0.0, _MODEL["jitter_x"])
        y += rng.normal(0.0, _MODEL["jitter_y"])
        points.append((x, y))

    return points, dur_ms / 1000.0


# --------------------------------------------------------------------------- #
# Visualization                                                               #
# --------------------------------------------------------------------------- #

def _draw_phone(ax, w, h):
    rect = mpatches.FancyBboxPatch(
        (0, 0), w, h,
        boxstyle="round,pad=0,rounding_size=40",
        linewidth=2, edgecolor="#444", facecolor="#fafafa", zorder=0,
    )
    ax.add_patch(rect)
    ax.set_xlim(-60, w + 60)
    ax.set_ylim(h + 60, -60)  # invert Y so origin is top-left
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.25)


def _velocity_profile_from_points(points, duration_s, n_bins=10):
    if len(points) < 3:
        return np.zeros(n_bins)
    n = len(points)
    t = np.linspace(0.0, duration_s, n)
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    seg = np.hypot(np.diff(x), np.diff(y))
    dt = np.diff(t)
    mid_t = (t[:-1] + t[1:]) / 2.0
    mid_speed = np.where(dt > 0, seg / dt, 0)
    target_t = np.linspace(t[0], t[-1], n_bins)
    return np.interp(target_t, mid_t, mid_speed)


def _arc_magnitude(points):
    if len(points) < 3:
        return 0.0
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    sx, sy = x[0], y[0]
    ex, ey = x[-1], y[-1]
    vx, vy = ex - sx, ey - sy
    L = math.hypot(vx, vy)
    if L == 0:
        return 0.0
    nx, ny = vy / L, -vx / L
    offs = (x - sx) * nx + (y - sy) * ny
    return float(np.max(np.abs(offs)))


def visualize_comparison(n_synth=None, save_path=None, seed=0):
    if save_path is None:
        save_path = os.path.join(HERE, "comparison.png")
    if n_synth is None:
        n_synth = len(_STATS["real_swipes"])  # 47 to match real

    rng = np.random.default_rng(seed)
    w = _MODEL["screen_w"]
    h = _MODEL["screen_h"]

    fig, axes = plt.subplots(1, 4, figsize=(20, 8.5))

    # --- 1. real swipes overlaid on phone ---
    ax = axes[0]
    _draw_phone(ax, w, h)
    real_arc_mags = []
    real_profiles = []
    for s in _STATS["real_swipes"]:
        x = np.array(s["x_px"])
        y = np.array(s["y_px"])
        ax.plot(x, y, color="#1565c0", alpha=0.4, linewidth=1.4)
        ax.scatter(x[0], y[0], color="#1565c0", s=22, alpha=0.7, zorder=3)
        ax.scatter(x[-1], y[-1], color="#1565c0",
                   marker="x", s=30, alpha=0.7, zorder=3)
        real_arc_mags.append(_arc_magnitude(list(zip(x, y))))
        t = np.array(s["t_s"])
        if len(t) >= 3:
            seg = np.hypot(np.diff(x), np.diff(y))
            dt = np.diff(t)
            mid_t = (t[:-1] + t[1:]) / 2.0
            mid_speed = np.where(dt > 0, seg / dt, 0)
            target_t = np.linspace(t[0], t[-1], 10)
            real_profiles.append(np.interp(target_t, mid_t, mid_speed))
    ax.set_title(f"Real upward swipes (n={len(_STATS['real_swipes'])})")
    ax.set_xlabel("x (px)")
    ax.set_ylabel("y (px)")

    # --- 2. synthetic swipes overlaid on the same phone ---
    ax = axes[1]
    _draw_phone(ax, w, h)
    synth_arc_mags = []
    synth_profiles = []
    for _ in range(n_synth):
        pts, dur = generate_human_swipe(rng=rng)
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts])
        ax.plot(x, y, color="#c62828", alpha=0.4, linewidth=1.4)
        ax.scatter(x[0], y[0], color="#c62828", s=22, alpha=0.7, zorder=3)
        ax.scatter(x[-1], y[-1], color="#c62828",
                   marker="x", s=30, alpha=0.7, zorder=3)
        synth_arc_mags.append(_arc_magnitude(pts))
        synth_profiles.append(_velocity_profile_from_points(pts, dur))
    # Use the same axis limits as the real plot for true visual comparison
    axes[1].set_xlim(axes[0].get_xlim())
    axes[1].set_ylim(axes[0].get_ylim())
    ax.set_title(f"Synthetic swipes from human_swipe (n={n_synth})")
    ax.set_xlabel("x (px)")

    # --- 3. velocity profile comparison ---
    ax = axes[2]
    real_profiles = np.array(real_profiles)
    synth_profiles = np.array(synth_profiles)
    real_norm = real_profiles / np.maximum(
        real_profiles.max(axis=1, keepdims=True), 1e-9
    )
    synth_norm = synth_profiles / np.maximum(
        synth_profiles.max(axis=1, keepdims=True), 1e-9
    )
    bins_t = np.linspace(0, 1, real_norm.shape[1])
    ax.plot(bins_t, real_norm.mean(axis=0), marker="o",
            color="#1565c0", linewidth=2, label="real (mean)")
    ax.fill_between(bins_t,
                    real_norm.mean(axis=0) - real_norm.std(axis=0),
                    real_norm.mean(axis=0) + real_norm.std(axis=0),
                    color="#1565c0", alpha=0.15)
    ax.plot(bins_t, synth_norm.mean(axis=0), marker="s",
            color="#c62828", linewidth=2, label="synthetic (mean)")
    ax.fill_between(bins_t,
                    synth_norm.mean(axis=0) - synth_norm.std(axis=0),
                    synth_norm.mean(axis=0) + synth_norm.std(axis=0),
                    color="#c62828", alpha=0.15)
    ax.set_title("Normalized velocity profile")
    ax.set_xlabel("normalized time")
    ax.set_ylabel("speed / peak speed")
    ax.set_ylim(0, 1.15)
    ax.legend(loc="upper left")
    ax.grid(True, alpha=0.3)

    # --- 4. arc magnitude histogram ---
    ax = axes[3]
    bins = np.linspace(0, max(max(real_arc_mags), max(synth_arc_mags)) + 5, 18)
    ax.hist(real_arc_mags, bins=bins, color="#1565c0", alpha=0.55,
            label=f"real (n={len(real_arc_mags)})", edgecolor="black")
    ax.hist(synth_arc_mags, bins=bins, color="#c62828", alpha=0.55,
            label=f"synthetic (n={len(synth_arc_mags)})", edgecolor="black")
    ax.set_title("Arc apex magnitude")
    ax.set_xlabel("|perpendicular offset| (px)")
    ax.set_ylabel("count")
    ax.legend()
    ax.grid(True, alpha=0.3)

    fig.suptitle(
        "human_swipe.py — synthetic swipes vs. real getevent trace",
        fontsize=14,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(save_path, dpi=120)
    print(f"Saved {save_path}")
    return save_path


def _print_one_example():
    rng = np.random.default_rng(42)
    pts, dur = generate_human_swipe(rng=rng)
    print(f"Example synthetic swipe: {len(pts)} points, "
          f"duration {dur*1000:.1f} ms")
    print(f"  start: ({pts[0][0]:.1f}, {pts[0][1]:.1f})")
    print(f"  end:   ({pts[-1][0]:.1f}, {pts[-1][1]:.1f})")


if __name__ == "__main__":
    _print_one_example()
    visualize_comparison()
    if not os.environ.get("NO_DISPLAY"):
        try:
            plt.show()
        except Exception:
            pass
