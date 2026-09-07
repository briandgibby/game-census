"""Fixed source registry: no arbitrary URL fetching or model-mediated interfaces."""
from . import players, store, catalog
from .base import Capture, SourceError
from .discovery import ADAPTERS
from .details import ADAPTERS as DETAIL_ADAPTERS

REGISTRY = {players.SOURCE: players, store.SOURCE: store, catalog.SOURCE: catalog, **ADAPTERS, **DETAIL_ADAPTERS}
__all__ = ["Capture", "SourceError", "REGISTRY", "players", "store"]
