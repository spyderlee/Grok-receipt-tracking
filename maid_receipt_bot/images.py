"""Image format support shared by OCR and the vision fallback."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_HEIF = None


def register_heif() -> bool:
    """Teach Pillow to open HEIC/HEIF (iPhone photos sent as a *file*, audit L1)."""
    global _HEIF
    if _HEIF is None:
        try:
            import pillow_heif  # type: ignore

            pillow_heif.register_heif_opener()
            _HEIF = True
        except Exception as exc:  # pragma: no cover - only if the wheel is missing
            log.warning("pillow-heif unavailable, HEIC receipts can't be read: %s", exc)
            _HEIF = False
    return _HEIF


# Telegram document mime type -> file extension we save under.
MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/heic": ".heic", "image/heif": ".heif", "image/heic-sequence": ".heic",
    "image/heif-sequence": ".heif", "image/tiff": ".tif", "image/bmp": ".bmp",
}
ALLOWED_EXT = set(MIME_EXT.values()) | {".jpeg", ".tiff"}
