#!/usr/bin/env python3
"""Paper download module for deep-report orchestrator."""

from .downloader import PaperDownloader
from .sources import PaperResult, ArxivSource, PMCSource, OpenAccessSource, DOISource, BiorxivSource

__all__ = [
    "PaperDownloader",
    "PaperResult",
    "ArxivSource",
    "PMCSource",
    "OpenAccessSource",
    "DOISource",
    "BiorxivSource",
]
