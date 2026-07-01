# scroll_reels.py
import os
import random
import sys
import time

import uiautomator2 as u2
from dotenv import load_dotenv

from human_swipe import generate_human_swipe

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# WiFi-debugging address (IP:port) preferred; USB serial as fallback.
DEVICE_ID = os.environ.get("PHONE_ADDRESS", "R5CX235CF9A")
DURATION_MINUTES = 10

# Distraction (reel loops while phone is idle)
DISTRACTION_CHANCE = 0.03
DISTRACTION_DURATION = (60, 150)


def human_swipe_up(d):
    """Fast flick gesture sampled from the empirical model fit in
    `human_swipe.py` (see swipe_stats.json). The model was calibrated on
    a 1080x2340 Galaxy S23 trace; if the device geometry differs, the
    pixel coordinates won't be on-screen.
    """
    points, duration_seconds = generate_human_swipe()
    int_points = [(int(round(x)), int(round(y))) for x, y in points]
    d.swipe_points(int_points, duration=duration_seconds)

def pick_watch_time():
    """Watch time distribution based on real Instagram Reels data.
    
    Average user watch time per reel is ~3s, with most reels skipped fast
    and a small tail of longer watches. Distribution:
      - 50% skip          (1.0-3.0s)
      - 25% brief check   (3.0-5.0s)
      - 15% watch         (5.0-7.0s)  ← cap of typical "real watch"
      -  7% hooked        (8.0-20s)
      -  3% distracted    (60-150s)
    """
    r = random.random()
    if r < DISTRACTION_CHANCE:
        return random.uniform(*DISTRACTION_DURATION), "distracted"
    elif r < DISTRACTION_CHANCE + 0.07:
        return random.uniform(8, 20), "hooked"
    elif r < DISTRACTION_CHANCE + 0.07 + 0.15:
        return random.uniform(5.0, 7.0), "watch"
    elif r < DISTRACTION_CHANCE + 0.07 + 0.15 + 0.25:
        return random.uniform(3.0, 5.0), "brief"
    else:
        return random.uniform(1.0, 3.0), "skip"


def open_reels(d):
    print("Opening Instagram...")
    d.app_start("com.instagram.android", stop=False)
    time.sleep(random.uniform(2.5, 4.5))
    
    reels_tab = d(resourceId="com.instagram.android:id/clips_tab")
    
    if reels_tab.exists:
        print("Tapping Reels tab...")
        reels_tab.click()
    else:
        for sel in [d(description="Reels"), d(text="Reels")]:
            if sel.exists:
                print("Tapping Reels (fallback)...")
                sel.click()
                break
        else:
            print("⚠ Reels tab not found.")
            return False
    
    time.sleep(random.uniform(2, 4))
    return True


def main():
    print(f"Connecting to {DEVICE_ID}...")
    d = u2.connect(DEVICE_ID)
    print(f"Connected: {d.info.get('productName', 'unknown')}")
    print(f"Screen: {d.window_size()}")

    if not open_reels(d):
        return

    end_time = time.time() + DURATION_MINUTES * 60
    swipe_count = 0
    mode_counts = {}

    print(f"Scrolling for {DURATION_MINUTES} minutes...\n")

    while time.time() < end_time:
        watch_time, mode = pick_watch_time()
        mode_counts[mode] = mode_counts.get(mode, 0) + 1

        marker = "💤" if mode == "distracted" else " "
        print(f"  Reel {swipe_count + 1}: {marker} {watch_time:.1f}s ({mode})")

        remaining = end_time - time.time()
        if watch_time > remaining:
            time.sleep(max(0, remaining))
            break

        time.sleep(watch_time)

        human_swipe_up(d)
        swipe_count += 1

    print(f"\nDone. {swipe_count} reels in {DURATION_MINUTES} minutes.")
    print(f"Mode breakdown: {mode_counts}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)