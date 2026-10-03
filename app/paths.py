"""Helpers for folder names and playlist classification."""
import re

_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_folder_name(title: str, playlist_id: str, max_bytes: int = 150) -> str:
    """Build a safe folder name: '<sanitized title> [<playlist_id>]'."""
    name = _BAD_CHARS.sub(" ", title or "")
    name = name.replace("..", " ")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "playlist"
    encoded = name.encode("utf-8")
    if len(encoded) > max_bytes:
        name = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    safe_id = _ID_BAD.sub("_", playlist_id or "unknown")
    return f"{name} [{safe_id}]"


def is_oneshot(title: str, keyword: str) -> bool:
    """True if the keyword appears in the title (case-insensitive)."""
    kw = (keyword or "").strip().lower()
    return bool(kw) and kw in (title or "").lower()
