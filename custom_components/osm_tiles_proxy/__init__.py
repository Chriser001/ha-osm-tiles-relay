"""Override the map_tiles integration's hardcoded OSMF upstream URLs.

HA 2026.9's ``map_tiles`` component (homeassistant/components/map_tiles/)
proxies the frontend base map from the OpenStreetMap Foundation tile servers
(``vector.openstreetmap.org`` / ``tile.openstreetmap.org``, both behind Fastly).
On networks where those hostnames' TLS SNI is blocked (mainland China), every
upstream fetch fails and HA answers the map endpoints with 502.

This component repoints the upstream URLs at a Cloudflare Worker relay
(configured via YAML) which forwards 1:1 to the real OSMF origins from CF's
edge. It only changes where map_tiles *fetches* — every other layer (the
/api/map_tiles/* endpoints, tokens, caching, TileJSON rewrite) is untouched.

Patch points (HA 2026.9.x source, components/map_tiles/views.py):
  - module globals resolved at request time:
      VECTOR_URL   (glyphs + sprite index/sheet URL builders)
      TILEJSON_URL (tilejson view)
  - class attributes baked as f-strings at class definition time:
      MapTilesVectorView.upstream
      MapTilesRasterView.upstream

If a future HA version renames or moves these, the patch fails loudly in the
log and the untouched module keeps its built-in upstream (the map simply stays
as it would have been without this component) — an update can never brick
startup over this.
"""

import importlib
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "osm_tiles_proxy"

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(DOMAIN): vol.Schema(
            {vol.Required("url"): cv.url}, extra=vol.PREVENT_EXTRA
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _patch_upstream(base_url: str) -> list[str]:
    """Point map_tiles.views upstream sources at ``base_url``.

    Returns a list of applied changes for logging. Raises AttributeError when
    the current HA's map_tiles no longer has the expected structure.
    """
    views = importlib.import_module("homeassistant.components.map_tiles.views")
    root = base_url.rstrip("/")
    applied: list[str] = []

    for name, value in {
        # Glyph/sprite URL builders and the TileJSON view read these globals
        # from the views module namespace on every request.
        "VECTOR_URL": root,
        "TILEJSON_URL": f"{root}/shortbread_v1/tilejson.json",
    }.items():
        if not hasattr(views, name):
            raise AttributeError(f"map_tiles.views has no module global '{name}'")
        setattr(views, name, value)
        applied.append(f"views.{name} = {value}")

    for cls_name, upstream in {
        # These f-strings were evaluated at class definition time.
        "MapTilesVectorView": f"{root}/shortbread_v1/{{z}}/{{x}}/{{y}}.mvt",
        "MapTilesRasterView": f"{root}/{{z}}/{{x}}/{{y}}.png",
    }.items():
        cls = getattr(views, cls_name, None)
        if cls is None or not hasattr(cls, "upstream"):
            raise AttributeError(f"map_tiles.views.{cls_name} has no 'upstream'")
        cls.upstream = upstream
        applied.append(f"views.{cls_name}.upstream = {upstream}")

    # The zone WAF (UA whitelist) on the relay hostname would 403 HA core's
    # stock UA, which the map_tiles component also sends upstream via
    # UPSTREAM_HEADERS. Append a whitelisted token so the HA -> Cloudflare leg
    # passes; the Worker replaces the UA before reaching OSMF, so the OSMF
    # tile policy (identifying UA with contact) is still honoured end to end.
    if not hasattr(views, "UPSTREAM_HEADERS"):
        raise AttributeError("map_tiles.views has no module global 'UPSTREAM_HEADERS'")
    upstream_headers = dict(views.UPSTREAM_HEADERS)
    ua = upstream_headers.get("User-Agent", "")
    if "Firefox/1" not in ua:
        upstream_headers["User-Agent"] = f"{ua} Firefox/1"
    views.UPSTREAM_HEADERS = upstream_headers
    applied.append("views.UPSTREAM_HEADERS User-Agent += ' Firefox/1'")

    return applied


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Repoint map_tiles' upstream once (dependency on map_tiles guarantees
    the views module is already imported and its views registered)."""
    base_url: str | None = (config.get(DOMAIN) or {}).get("url")
    if not base_url:
        _LOGGER.error("%s: missing required 'url' in configuration.yaml", DOMAIN)
        return False

    try:
        applied = _patch_upstream(base_url)
    except AttributeError as err:
        _LOGGER.error(
            "%s: cannot patch map_tiles — HA version drift? %s. "
            "Leaving the built-in upstream in place.",
            DOMAIN,
            err,
        )
        return True

    for line in applied:
        _LOGGER.warning("%s: %s", DOMAIN, line)
    return True
