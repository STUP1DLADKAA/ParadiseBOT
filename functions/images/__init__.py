"""Web image search package for Paradise.

Real images, found on the real web — not AI-generated pictures.
"""

from .models import ImageResponse, ImageResult
from .provider import ImageSearchError, ImageSearchService, is_safe_image_url
from .ui import MAX_EMBEDS, NO_IMAGE_LINE, build_image_embeds

__all__ = [
    "ImageResponse",
    "ImageResult",
    "ImageSearchError",
    "ImageSearchService",
    "MAX_EMBEDS",
    "NO_IMAGE_LINE",
    "build_image_embeds",
    "is_safe_image_url",
]
