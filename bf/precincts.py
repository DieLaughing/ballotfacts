"""Which ward and precinct an address is in, for Massachusetts, from the official map: MassGIS's
2022 Wards and Precincts (maintained by the Secretary of the Commonwealth's office).

The map is downloaded from MassGIS once (about 6 MB) and kept in the data folder. Lookups happen
on this server: the voter's coordinates never go anywhere else.
"""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass

import httpx
import shapefile
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import Point, shape

from . import config

log = logging.getLogger("bf.precincts")
SOURCE = "https://s3.us-east-1.amazonaws.com/download.massgis.digital.mass.gov/shapefiles/state/wardsprecincts2022_poly.zip"
ABOUT = "https://www.mass.gov/info-details/massgis-data-2022-wards-and-precincts"
FILE = config.DATA / "ma-precincts-2022.zip"
_to_state_plane = Transformer.from_crs("EPSG:4326", "EPSG:26986", always_xy=True)  # the map's projection


@dataclass
class Precinct:
    town: str       # "FALMOUTH"
    ward: str       # "" in towns, which have precincts only
    precinct: str   # "4"
    name: str       # "Falmouth Precinct 4"

    @property
    def key(self) -> str:
        """Matches the 'TOWN|WARD|PCT' lists in bf/data/ma-2026.json."""
        return f"{self.town}|{self.ward}|{self.precinct}"


_index: tuple[STRtree, list[Precinct]] | None = None


async def download(client: httpx.AsyncClient) -> None:
    if FILE.exists():
        return
    r = await client.get(SOURCE, timeout=120)
    r.raise_for_status()
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(FILE)
    log.warning("downloaded the MassGIS precinct map")


def _load() -> tuple[STRtree, list[Precinct]] | None:
    global _index
    if _index is None and FILE.exists():
        with zipfile.ZipFile(FILE) as z:
            base = next(n[:-4] for n in z.namelist() if n.upper().endswith("POLY.SHP"))
            reader = shapefile.Reader(shp=io.BytesIO(z.read(base + ".shp")), shx=io.BytesIO(z.read(base + ".shx")),
                                      dbf=io.BytesIO(z.read(base + ".dbf")))
            fields = [f[0] for f in reader.fields[1:]]
            shapes, info = [], []
            for sr in reader.iterShapeRecords():
                a = dict(zip(fields, sr.record))
                shapes.append(shape(sr.shape.__geo_interface__))
                info.append(Precinct(a["TOWN"].strip().upper(), str(a["WARD"] or "").strip(), str(a["PRECINCT"]).strip(),
                                     a["WP_NAME"].strip()))
        _index = (STRtree(shapes), info)
    return _index


def find(lat: float, lng: float) -> Precinct | None:
    loaded = _load()
    if not loaded or not lat:
        return None
    tree, info = loaded
    point = Point(*_to_state_plane.transform(lng, lat))
    hits = tree.query(point, predicate="within")
    return info[hits[0]] if len(hits) else None
