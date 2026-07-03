import os
import sys

import uiautomator2 as u2

# Make the repo root importable so `from shared import config` resolves when
# run directly (`py modules/instagram/dump_ui.py`).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import config  # noqa: E402  (needs the sys.path bootstrap above)

DEVICE_ID = config.DEVICE_ID

d = u2.connect(DEVICE_ID)
d.app_start("com.instagram.android", stop=False)

import time
time.sleep(3)  # give IG time to load

# Save the full UI tree to a file
xml = d.dump_hierarchy()
with open("ui_dump.xml", "w", encoding="utf-8") as f:
    f.write(xml)

print(f"Dumped {len(xml)} chars to ui_dump.xml")
print("Open it in a text editor or browser and search for 'reel' or 'tab'")