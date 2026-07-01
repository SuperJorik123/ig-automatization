import os

import uiautomator2 as u2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
DEVICE_ID = os.environ.get("PHONE_ADDRESS", "R5CX235CF9A")

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