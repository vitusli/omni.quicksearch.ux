"""Path and omni.client helper utilities shared across the extension."""

import os
from urllib.parse import unquote, urlparse

PREVIEW_STAGE_FILENAMES = {"main.usd", "main.usda"}
PREVIEW_IMAGE_FILENAME = "preview.png"


def normalize_path(path: str) -> str:
    """Normalize a path/URL to use forward slashes and no surrounding whitespace."""
    return str(path or "").strip().replace("\\", "/")


def omni_result_ok(result) -> bool:
    """Return True if an omni.client result represents success (OK/ALREADY)."""
    if isinstance(result, tuple):
        result = result[0]
    name = str(getattr(result, "name", result)).upper()
    return "OK" in name or "ALREADY" in name


def to_local_filesystem_path(path_or_url: str) -> str | None:
    """Convert a local path or file:// URL to an absolute filesystem path.

    Returns None for anonymous stages or non-file remote URLs.
    """
    value = normalize_path(path_or_url)
    if not value or value.startswith("anon:"):
        return None

    if value.lower().startswith("file:"):
        parsed = urlparse(value)
        decoded_path = unquote(parsed.path or "")

        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            unc_path = f"//{parsed.netloc}{decoded_path}"
            return os.path.abspath(unc_path)

        if len(decoded_path) >= 3 and decoded_path[0] == "/" and decoded_path[2] == ":":
            decoded_path = decoded_path[1:]

        decoded_path = decoded_path.replace("/", os.sep)
        return os.path.abspath(decoded_path)

    if "://" in value:
        return None

    return os.path.abspath(value)


def is_saved_stage(stage_url: str) -> bool:
    """Return True if the stage URL points to a persisted (non-anonymous) stage."""
    normalized = normalize_path(stage_url)
    return bool(normalized) and not normalized.startswith("anon:")
