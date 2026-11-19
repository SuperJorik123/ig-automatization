"""The one shape every radar source produces."""
from dataclasses import dataclass, field


@dataclass
class Item:
    source: str          # "tg:@bnonews", "reddit:PublicFreakout", "usgs", "nws", …
    ext_id: str          # unique within the source
    time: float          # unix seconds when it was posted
    text: str            # the post's text / title (any language)
    link: str = ""       # where a human can open it
    kind: str = "post"   # "post" (may alert with video) | "hazard" (alerts on its own)
    has_video: bool = False
    media_size: int = 0  # bytes; the same video reposted keeps its size
    duration: float = 0.0
    place: str = ""      # known up front (hazard feeds); posts get it from the label
    country: str = ""
    lat: float | None = None
    lon: float | None = None
    metrics: dict = field(default_factory=dict)  # reddit ups/velocity, quake mag…
    ref: dict = field(default_factory=dict)      # how to fetch the media again
    thumb: bytes | str | None = None             # bytes or an https URL, for the label

    @property
    def family(self) -> str:
        """`tg`, `reddit`, `usgs`… — what "different sources" counts by."""
        return self.source.split(":", 1)[0]
