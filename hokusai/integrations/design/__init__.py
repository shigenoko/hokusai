"""Design integration package (Figma / Miro)."""

from .cache import DesignCache
from .context import (
    DesignContextResolver,
    DesignResolution,
    ResolutionStatus,
    extract_figma_urls,
    extract_miro_urls,
)
from .figma import FigmaAPIError, FigmaClient, FigmaRateLimitError
from .miro import MiroAPIError, MiroClient, MiroRateLimitError
from .url_parser import (
    ParsedFigmaUrl,
    ParsedMiroUrl,
    parse_figma_url,
    parse_miro_url,
)

__all__ = [
    "DesignCache",
    "DesignContextResolver",
    "DesignResolution",
    "ResolutionStatus",
    "FigmaAPIError",
    "FigmaClient",
    "FigmaRateLimitError",
    "MiroAPIError",
    "MiroClient",
    "MiroRateLimitError",
    "ParsedFigmaUrl",
    "ParsedMiroUrl",
    "extract_figma_urls",
    "extract_miro_urls",
    "parse_figma_url",
    "parse_miro_url",
]
