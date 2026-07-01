"""
analyze_swipes.py

Parse `swipes.txt` (output of `adb shell getevent -lt /dev/input/eventN`),
classify each touch session, compute statistics for the upward swipes,
save histograms, and write a JSON of empirical parameters so that
`human_swipe.py` can replay the same distribution.

Run:
    python analyze_swipes.py

Outputs:
    classification.txt     -- one line per gesture, plus totals
    swipe_stats.json       -- empirical distributions for the simulator
    hist_*.png             -- histograms of the various swipe attributes
"""

import json
import math
import os
import re

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

INPUT_FILE = os.path.join(os.path.dirname(__file__), "swipes.txt")
OUT_DIR = os.path.dirname(__file__)

SCREEN_W = 1080
SCREEN_H = 2340

# Empirical: the sec_touchscreen on the Galaxy S23 reports raw values whose
# observed maxima (X up to 3909, Y up to 3312 in this trace) are consistent
# with a 12-bit (0..4095) range on both axes. We use 4096 as the divisor.
RAW_MAX = 4096
X_PX_PER_RAW = SCREEN_W / RAW_MAX
Y_PX_PER_RAW = SCREEN_H / RAW_MAX


# --------------------------------------------------------------------------- #
# 1. Parsing                                                                  #
# --------------------------------------------------------------------------- #

LINE_RE = re.compile(
    r"\[\s*([\d.]+)\]\s*[^:]*:\s*(EV_\w+)\s+(\w+)\s+([0-9a-fA-F]+)"
)


def parse_sessions(path):
    """Parse getevent -lt output into a list of touch sessions.

    Each session is a list of (t_seconds, x_raw, y_raw) tuples. We commit
    one sample per SYN_REPORT frame ONLY IF that frame reported a position
    update (ABS_MT_POSITION_X or ABS_MT_POSITION_Y). Frames that only
    update TOUCH_MAJOR/MINOR (touch-area changes with no movement) and
    the finger-lift frame are skipped, since they would otherwise show up
    as duplicate-position samples with 0 velocity.

    The trace contains no ABS_MT_SLOT events, so all touches are slot 0
    (single-finger).
    """
    sessions = []
    current = None       # in-progress list of points
    last_x = None        # latest known X for the in-progress touch
    last_y = None
    frame_x = None       # X reported in the current SYN frame, if any
    frame_y = None
    frame_t = None
    frame_lift = False
    frame_down = False

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            m = LINE_RE.search(raw_line)
            if not m:
                continue
            ts_s, ev_type, ev_code, val_hex = m.groups()
            ts = float(ts_s)

            if ev_type == "EV_ABS":
                if ev_code == "ABS_MT_TRACKING_ID":
                    if val_hex.lower() == "ffffffff":
                        frame_lift = True
                    else:
                        frame_down = True
                        if current is None:
                            current = []
                elif ev_code == "ABS_MT_POSITION_X":
                    frame_x = int(val_hex, 16)
                    frame_t = ts
                elif ev_code == "ABS_MT_POSITION_Y":
                    frame_y = int(val_hex, 16)
                    frame_t = ts
            elif ev_type == "EV_SYN" and ev_code == "SYN_REPORT":
                frame_has_position = (frame_x is not None
                                      or frame_y is not None)
                if frame_x is not None:
                    last_x = frame_x
                if frame_y is not None:
                    last_y = frame_y
                if (
                    frame_has_position
                    and last_x is not None
                    and last_y is not None
                    and current is not None
                ):
                    current.append((frame_t, last_x, last_y))
                if frame_lift:
                    if current and len(current) > 0:
                        sessions.append(current)
                    current = None
                    last_x = None
                    last_y = None
                frame_x = frame_y = None
                frame_t = None
                frame_down = False
                frame_lift = False

    return sessions


# --------------------------------------------------------------------------- #
# 2. Classification                                                           #
# --------------------------------------------------------------------------- #

def to_px(session):
    """Return a numpy array shape (N, 3) with columns (t_s, x_px, y_px)."""
    arr = np.array(session, dtype=float)
    arr[:, 1] *= X_PX_PER_RAW
    arr[:, 2] *= Y_PX_PER_RAW
    return arr


def classify(session_px):
    """Return (label, info_dict) for a single touch session in screen px.

    Strict rules (all must hold to qualify as a swipe):
      - vertical distance > 200 px
      - vertical distance > 2 x horizontal distance
      - 30ms < duration < 800ms
      - >= 5 raw points
      - Y trends monotonically (allow some wobble but no major reversal)

    Returns one of:
        ("swipe_up", info)
        ("swipe_down", info)
        ("tap", info)
        ("drag", info)              # vertical-ish but too long in time
        ("rejected", info)          # info["reason"] explains why
    """
    n = len(session_px)
    t = session_px[:, 0]
    x = session_px[:, 1]
    y = session_px[:, 2]

    duration_ms = (t[-1] - t[0]) * 1000.0
    dx = x[-1] - x[0]
    dy = y[-1] - y[0]
    abs_dx = abs(dx)
    abs_dy = abs(dy)
    info = {
        "n": n,
        "duration_ms": duration_ms,
        "dx_px": float(dx),
        "dy_px": float(dy),
        "x_start": float(x[0]),
        "y_start": float(y[0]),
        "x_end": float(x[-1]),
        "y_end": float(y[-1]),
    }

    # tap: very small movement
    if abs_dx < 30 and abs_dy < 30:
        return "tap", info

    if n < 5:
        info["reason"] = f"only {n} points (need >=5)"
        return "rejected", info

    if duration_ms < 30:
        info["reason"] = f"duration {duration_ms:.1f}ms < 30ms"
        return "rejected", info

    if duration_ms > 800:
        info["reason"] = f"duration {duration_ms:.1f}ms > 800ms"
        return "drag", info

    if abs_dy <= 200:
        info["reason"] = f"vertical {abs_dy:.0f}px <= 200px"
        return "rejected", info

    if abs_dy <= 2 * abs_dx:
        info["reason"] = (
            f"horizontal too large: |dx|={abs_dx:.0f} |dy|={abs_dy:.0f}"
        )
        return "rejected", info

    # monotonicity check: count signs of consecutive Y deltas. Allow up to
    # ~20% reversals (small wobble), reject if there's a large direction
    # reversal mid-gesture (compare cumulative against straight-line dy).
    diffs = np.diff(y)
    if dy < 0:
        wrong_way = np.sum(diffs > 0)
    else:
        wrong_way = np.sum(diffs < 0)
    frac_wrong = wrong_way / max(1, len(diffs))

    # perpendicular-monotonicity: check that the cumulative back-and-forth
    # movement isn't huge compared with the net distance
    abs_total = float(np.sum(np.abs(diffs)))
    if abs_total > 0 and abs_dy / abs_total < 0.7:
        info["reason"] = (
            f"too much Y wobble (net/total={abs_dy/abs_total:.2f})"
        )
        return "rejected", info

    if frac_wrong > 0.30:
        info["reason"] = f"{frac_wrong:.0%} of samples reverse direction"
        return "rejected", info

    label = "swipe_up" if dy < 0 else "swipe_down"
    return label, info


# --------------------------------------------------------------------------- #
# 3. Statistics for upward swipes                                             #
# --------------------------------------------------------------------------- #

def perpendicular_offsets(x, y):
    """For a path (x, y), return signed perpendicular offsets from the
    straight start->end line. Positive = right of the start->end vector
    (using +X = right convention)."""
    sx, sy = x[0], y[0]
    ex, ey = x[-1], y[-1]
    vx, vy = ex - sx, ey - sy
    L = math.hypot(vx, vy)
    if L == 0:
        return np.zeros_like(x)
    ux, uy = vx / L, vy / L
    # right-perpendicular to (ux, uy) using +X right, +Y down convention is
    # (uy, -ux); points to the geometric "right" of motion
    nx, ny = uy, -ux
    return (x - sx) * nx + (y - sy) * ny


def velocity_profile(t, x, y, n_samples=10):
    """Sample 10 normalized-time positions along the gesture and return the
    instantaneous speed at each (in px/s). Speed is computed by 3-point
    finite difference on the raw samples then linearly interpolated.
    """
    if len(t) < 3:
        return np.zeros(n_samples)
    # cumulative distance
    seg = np.hypot(np.diff(x), np.diff(y))
    cumdist = np.concatenate([[0], np.cumsum(seg)])
    dt = np.diff(t)
    # midpoint speed of each segment
    mid_t = (t[:-1] + t[1:]) / 2.0
    mid_speed = np.where(dt > 0, seg / dt, 0)
    # interpolate speed onto normalized time samples
    t_norm = (t - t[0]) / (t[-1] - t[0])
    target_t = np.linspace(0, 1, n_samples)
    target_real_t = t[0] + target_t * (t[-1] - t[0])
    return np.interp(target_real_t, mid_t, mid_speed)


def jitter_per_swipe(x, y):
    """Estimate residual jitter as the std of (point - smoothed_point)
    where the smoothed point is a simple 3-sample moving average.
    Returns (jitter_x, jitter_y) in px.
    """
    if len(x) < 5:
        return 0.0, 0.0
    sx = np.convolve(x, np.ones(3) / 3, mode="valid")
    sy = np.convolve(y, np.ones(3) / 3, mode="valid")
    rx = x[1:-1] - sx
    ry = y[1:-1] - sy
    return float(np.std(rx)), float(np.std(ry))


# --------------------------------------------------------------------------- #
# 4. Reporting                                                                #
# --------------------------------------------------------------------------- #

def percent(values, q):
    return float(np.percentile(values, q))


def summarize(values, label, unit=""):
    a = np.asarray(values, dtype=float)
    return (
        f"  {label:<24} n={len(a):2d}  "
        f"min={a.min():7.2f}  max={a.max():7.2f}  "
        f"mean={a.mean():7.2f}  median={np.median(a):7.2f}  "
        f"std={a.std():6.2f} {unit}"
    )


def save_hist(values, title, xlabel, fname, bins=15):
    fig, ax = plt.subplots(figsize=(6, 3.5))
    ax.hist(values, bins=bins, color="#3676c0", edgecolor="black", alpha=0.85)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("count")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, fname), dpi=120)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    sessions = parse_sessions(INPUT_FILE)
    if not sessions:
        print(f"No touch sessions found in {INPUT_FILE}.")
        return

    # Convert all to screen px and classify
    classified = []  # list of (idx, label, info, session_px)
    counts = {}
    lines = []
    for i, s in enumerate(sessions):
        s_px = to_px(s)
        label, info = classify(s_px)
        counts[label] = counts.get(label, 0) + 1
        classified.append((i, label, info, s_px))
        if label == "rejected":
            extra = f"rejected: {info['reason']}"
        else:
            extra = label
        lines.append(
            f"  S{i:02d} n={info['n']:2d} dt={info['duration_ms']:6.1f}ms "
            f"start=({info['x_start']:4.0f},{info['y_start']:4.0f}) "
            f"end=({info['x_end']:4.0f},{info['y_end']:4.0f}) "
            f"dx={info['dx_px']:+5.0f} dy={info['dy_px']:+5.0f}  -> {extra}"
        )
    summary = "\n".join(lines)
    summary += "\n\nTotals:\n"
    for k, v in sorted(counts.items()):
        summary += f"  {k}: {v}\n"
    print(summary)
    with open(os.path.join(OUT_DIR, "classification.txt"), "w") as f:
        f.write(summary)

    # Pull out swipe_up sessions for analysis
    swipes = [(i, info, sp) for (i, lab, info, sp) in classified
              if lab == "swipe_up"]
    if not swipes:
        print("\nNo upward swipes to analyze.")
        return

    print(f"\n{'-' * 60}\nUpward swipes: {len(swipes)}\n{'-' * 60}")

    # --- collect raw stats ---
    x_starts, y_starts, x_ends, y_ends = [], [], [], []
    durations_ms = []
    n_points_list = []
    path_lengths = []
    straight_lengths = []
    curvatures = []
    arc_apex_signed = []   # signed perpendicular offset at the apex
    arc_apex_magnitude = []
    velocity_profiles = []  # 10 samples each
    jitter_x_list, jitter_y_list = [], []

    for _, info, sp in swipes:
        t = sp[:, 0]
        x = sp[:, 1]
        y = sp[:, 2]

        x_starts.append(x[0])
        y_starts.append(y[0])
        x_ends.append(x[-1])
        y_ends.append(y[-1])
        durations_ms.append((t[-1] - t[0]) * 1000.0)
        n_points_list.append(len(sp))

        seg = np.hypot(np.diff(x), np.diff(y))
        path = float(np.sum(seg))
        straight = math.hypot(x[-1] - x[0], y[-1] - y[0])
        path_lengths.append(path)
        straight_lengths.append(straight)
        curvatures.append(path / straight if straight > 0 else 1.0)

        offs = perpendicular_offsets(x, y)
        # apex = the point of largest |offset|
        apex_idx = int(np.argmax(np.abs(offs)))
        arc_apex_signed.append(float(offs[apex_idx]))
        arc_apex_magnitude.append(float(abs(offs[apex_idx])))

        velocity_profiles.append(velocity_profile(t, x, y, n_samples=10))

        jx, jy = jitter_per_swipe(x, y)
        jitter_x_list.append(jx)
        jitter_y_list.append(jy)

    velocity_profiles = np.array(velocity_profiles)
    # normalize each profile by its own peak so we can average shape
    norm_profiles = velocity_profiles / np.maximum(
        velocity_profiles.max(axis=1, keepdims=True), 1e-9
    )
    avg_norm_profile = norm_profiles.mean(axis=0)

    print(summarize(x_starts, "x_start (px)"))
    print(summarize(y_starts, "y_start (px)"))
    print(summarize(x_ends, "x_end (px)"))
    print(summarize(y_ends, "y_end (px)"))
    print(summarize(durations_ms, "duration (ms)"))
    print(summarize(path_lengths, "path length (px)"))
    print(summarize(straight_lengths, "straight length (px)"))
    print(summarize(curvatures, "curvature ratio"))
    print(summarize(arc_apex_signed, "arc apex signed (px)"))
    print(summarize(arc_apex_magnitude, "arc apex |offset| (px)"))
    print(summarize(n_points_list, "raw points/swipe"))
    print(summarize(jitter_x_list, "per-swipe jitter X"))
    print(summarize(jitter_y_list, "per-swipe jitter Y"))
    print()
    print("Average normalized velocity profile (10 bins, 0..1 of swipe time):")
    for i, v in enumerate(avg_norm_profile):
        bar = "#" * int(round(v * 50))
        print(f"  t={i/9:.2f}: {v:.3f}  {bar}")
    print()
    pct_w_start = np.mean(x_starts) / SCREEN_W * 100
    pct_h_start = np.mean(y_starts) / SCREEN_H * 100
    pct_w_end = np.mean(x_ends) / SCREEN_W * 100
    pct_h_end = np.mean(y_ends) / SCREEN_H * 100
    print(f"Mean start: x={pct_w_start:.1f}% of width, y={pct_h_start:.1f}% of height")
    print(f"Mean end:   x={pct_w_end:.1f}% of width, y={pct_h_end:.1f}% of height")

    # --- save histograms ---
    save_hist(x_starts, "Swipe start X", "px", "hist_x_start.png")
    save_hist(y_starts, "Swipe start Y", "px", "hist_y_start.png")
    save_hist(x_ends, "Swipe end X", "px", "hist_x_end.png")
    save_hist(y_ends, "Swipe end Y", "px", "hist_y_end.png")
    save_hist(durations_ms, "Swipe duration", "ms", "hist_duration.png")
    save_hist(path_lengths, "Path length", "px", "hist_path_length.png")
    save_hist(curvatures, "Curvature (path / straight)", "ratio",
              "hist_curvature.png")
    save_hist(arc_apex_signed, "Arc apex (signed offset)", "px",
              "hist_arc_signed.png")
    save_hist(arc_apex_magnitude, "Arc apex |offset|", "px",
              "hist_arc_magnitude.png")
    save_hist(n_points_list, "Raw points per swipe", "count",
              "hist_n_points.png", bins=range(min(n_points_list),
                                              max(n_points_list) + 2))

    # average velocity profile plot
    fig, ax = plt.subplots(figsize=(6, 3.5))
    bins_t = np.linspace(0, 1, 10)
    ax.plot(bins_t, avg_norm_profile, marker="o", color="#c0464a")
    ax.fill_between(bins_t, 0, avg_norm_profile, color="#c0464a", alpha=0.15)
    ax.set_title("Average normalized velocity profile")
    ax.set_xlabel("normalized time")
    ax.set_ylabel("speed / peak speed")
    ax.set_ylim(0, 1.05)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "velocity_profile.png"), dpi=120)
    plt.close(fig)

    # --- write the JSON for the simulator ---
    stats = {
        "screen": {"w": SCREEN_W, "h": SCREEN_H},
        "n_swipes": len(swipes),
        "x_start_px": list(map(float, x_starts)),
        "y_start_px": list(map(float, y_starts)),
        "x_end_px": list(map(float, x_ends)),
        "y_end_px": list(map(float, y_ends)),
        "duration_ms": list(map(float, durations_ms)),
        "n_points": list(map(int, n_points_list)),
        "path_length_px": list(map(float, path_lengths)),
        "straight_length_px": list(map(float, straight_lengths)),
        "curvature": list(map(float, curvatures)),
        "arc_apex_signed_px": list(map(float, arc_apex_signed)),
        "arc_apex_magnitude_px": list(map(float, arc_apex_magnitude)),
        "jitter_x_px": list(map(float, jitter_x_list)),
        "jitter_y_px": list(map(float, jitter_y_list)),
        "avg_norm_velocity_profile": list(map(float, avg_norm_profile)),
        # also store the raw px traces of the upward swipes so the simulator
        # can plot the "real" overlay in visualize_comparison()
        "real_swipes": [
            {
                "t_s": [float(p) for p in sp[:, 0].tolist()],
                "x_px": [float(p) for p in sp[:, 1].tolist()],
                "y_px": [float(p) for p in sp[:, 2].tolist()],
            }
            for _, _, sp in swipes
        ],
    }
    out_path = os.path.join(OUT_DIR, "swipe_stats.json")
    with open(out_path, "w") as f:
        json.dump(stats, f, indent=2)
    print(f"\nSaved {out_path}")
    print("Saved histograms (hist_*.png) and velocity_profile.png to disk.")


if __name__ == "__main__":
    main()
