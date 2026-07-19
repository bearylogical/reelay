"""Reelay — the household media-request relay for Telegram."""

from pathlib import Path

try:
    __version__ = (Path(__file__).parent / "VERSION").read_text().strip()
except Exception:
    __version__ = "0.0.0+dev"
