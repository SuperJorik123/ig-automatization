"""Pure parsing of a uiautomator2 hierarchy dump (`d.dump_hierarchy()`).

Everything that reads a dump — the recorder's account guess now, the screen
classifier, the locator and the auto-labeller later — goes through here, so
there is one notion of "a node" and its bounds. No device, no I/O.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

PKG = "com.instagram.android"
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True)
class Node:
    attrs: dict = field(hash=False)
    bounds: tuple  # (left, top, right, bottom) in screen pixels
    depth: int = 0

    def get(self, key, default=""):
        return self.attrs.get(key, default)

    @property
    def rid(self):
        """resource-id without the `com.instagram.android:id/` prefix."""
        rid = self.attrs.get("resource-id", "")
        return rid.split(":id/", 1)[1] if ":id/" in rid else rid

    @property
    def text(self):
        return self.attrs.get("text", "")

    @property
    def desc(self):
        return self.attrs.get("content-desc", "")

    @property
    def width(self):
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self):
        return self.bounds[3] - self.bounds[1]

    @property
    def center(self):
        return ((self.bounds[0] + self.bounds[2]) // 2, (self.bounds[1] + self.bounds[3]) // 2)

    def flag(self, name):
        return self.attrs.get(name) == "true"


def parse_bounds(value):
    m = _BOUNDS_RE.fullmatch(value or "")
    return tuple(int(g) for g in m.groups()) if m else None


def nodes(xml):
    """Every node with readable bounds, in document order. A dump that isn't
    valid XML (truncated mid-write) gives []."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []

    def walk(el, depth):
        for child in el:
            if child.tag == "node":
                b = parse_bounds(child.get("bounds"))
                if b:
                    out.append(Node(dict(child.attrib), b, depth))
            walk(child, depth + 1)

    walk(root, 0)
    return out


def find(xml_or_nodes, rid=None, text=None, desc=None, desc_prefix=None, min_size=1):
    """Nodes matching every given criterion (exact rid / text / desc, or a
    content-desc prefix, case-insensitive)."""
    ns = nodes(xml_or_nodes) if isinstance(xml_or_nodes, str) else xml_or_nodes
    out = []
    for n in ns:
        if n.width < min_size or n.height < min_size:
            continue
        if rid is not None and n.rid != rid:
            continue
        if text is not None and n.text != text:
            continue
        if desc is not None and n.desc != desc:
            continue
        if desc_prefix is not None and not n.desc.lower().startswith(desc_prefix.lower()):
            continue
        out.append(n)
    return out


def packages(xml):
    """The set of app packages that own nodes in this dump."""
    return {n.get("package") for n in nodes(xml) if n.get("package")}


# Where IG shows the active username. Best guesses until Phase 2 confirms
# them on the phone: the Profile screen's action-bar title, and the
# account-switcher sheet's selected row.
_ACCOUNT_RIDS = ("action_bar_large_title", "action_bar_title", "action_bar_username",
                 "profile_header_username", "username")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def guess_account(xml):
    """The active IG username if the dump shows one in a known place, else
    None. Best-effort: only used to tag recordings; the driver's real
    pre-Share account check (Phase 3) won't rely on a guess."""
    ns = nodes(xml)
    for rid in _ACCOUNT_RIDS:
        for n in find(ns, rid=rid):
            value = (n.text or n.desc).strip().lstrip("@")
            if _USERNAME_RE.match(value):
                return value
    return None
