#!/usr/bin/env python3
"""Paper download orchestration for deep-report.

Coordinates downloading papers from multiple sources.
"""

import re
import json
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .sources import (
    PaperResult,
    ArxivSource,
    PMCSource,
    OpenAccessSource,
    DOISource,
    BiorxivSource,
)


class PaperDownloader:
    """Orchestrates paper downloads from multiple academic sources."""

    def __init__(self, output_dir: Path, max_workers: int = 3):
        """Initialize the downloader.

        Args:
            output_dir: Directory to save downloaded PDFs
            max_workers: Maximum concurrent downloads
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers

        # Initialize source handlers in priority order
        self.sources = [
            ArxivSource(),
            PMCSource(),
            BiorxivSource(),
            OpenAccessSource(),
            DOISource(),  # Last resort - tries to follow DOI
        ]

        # Results tracking
        self.results = {
            "downloaded": [],
            "failed": [],
            "skipped": [],
        }

    def extract_urls(self, text: str) -> list[str]:
        """Extract unique URLs from text content.

        Args:
            text: Text containing URLs (e.g., references markdown)

        Returns:
            List of unique URLs
        """
        # Match various URL patterns
        patterns = [
            r'https?://[^\s\)\]<>\"]+',
            r'doi\.org/10\.\d{4,}/[^\s\)\]<>\"]+',
        ]

        urls = []
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            urls.extend(matches)

        # Clean up URLs
        cleaned = []
        seen = set()
        for url in urls:
            # Remove trailing punctuation
            url = url.rstrip('.,;:)\'"')
            # Remove markdown formatting artifacts
            url = re.sub(r'\)$', '', url)

            # Skip duplicates
            if url.lower() in seen:
                continue
            seen.add(url.lower())

            # Skip obviously non-paper URLs
            skip_patterns = [
                'github.com',
                'twitter.com',
                'linkedin.com',
                'youtube.com',
                'wikipedia.org',
                'amazon.com',
            ]
            if any(p in url.lower() for p in skip_patterns):
                continue

            cleaned.append(url)

        return cleaned

    def download_one(self, url: str) -> PaperResult:
        """Download a single paper.

        Tries each source handler in order until one succeeds.

        Args:
            url: URL of the paper to download

        Returns:
            PaperResult with success status
        """
        for source in self.sources:
            if source.can_handle(url):
                result = source.download(url, self.output_dir)
                if result.success:
                    return result
                # If this source could handle it but failed, don't try others
                # (they likely won't work either)
                if result.error and "paywall" not in result.error.lower():
                    return result

        return PaperResult(
            success=False,
            error="No handler found for this URL type"
        )

    def download_all(self, refs_file: Path, parallel: bool = True) -> dict:
        """Download all papers referenced in a file.

        Args:
            refs_file: Path to references file (markdown or text)
            parallel: Whether to download in parallel

        Returns:
            Dict with 'downloaded', 'failed', 'skipped' lists
        """
        if not refs_file.exists():
            print(f"References file not found: {refs_file}")
            return self.results

        # Extract URLs
        content = refs_file.read_text()
        urls = self.extract_urls(content)
        print(f"Found {len(urls)} URLs in references")

        if not urls:
            return self.results

        if parallel:
            self._download_parallel(urls)
        else:
            self._download_sequential(urls)

        # Write download report
        self._write_report()

        return self.results

    def _download_sequential(self, urls: list[str]):
        """Download URLs one at a time."""
        for i, url in enumerate(urls, 1):
            print(f"  [{i}/{len(urls)}] {url[:60]}...")
            result = self.download_one(url)
            self._record_result(url, result)

    def _download_parallel(self, urls: list[str]):
        """Download URLs in parallel."""
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.download_one, url): url for url in urls}

            for i, future in enumerate(as_completed(futures), 1):
                url = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = PaperResult(success=False, error=str(e))

                self._record_result(url, result)
                status = "✓" if result.success else "✗"
                short_url = url[:50] + "..." if len(url) > 50 else url
                if result.success:
                    print(f"  [{i}/{len(urls)}] {status} {result.filepath.name}")
                else:
                    print(f"  [{i}/{len(urls)}] {status} {short_url}")

    def _record_result(self, url: str, result: PaperResult):
        """Record a download result."""
        if result.success:
            self.results["downloaded"].append({
                "url": url,
                "file": str(result.filepath),
                "source": result.source,
                "size_bytes": result.size_bytes,
            })
        elif result.error and "no handler" in result.error.lower():
            self.results["skipped"].append(url)
        else:
            self.results["failed"].append({
                "url": url,
                "error": result.error,
                "source": result.source,
            })

    def _write_report(self):
        """Write a download report to the output directory."""
        report_file = self.output_dir / "download_report.json"
        report = {
            "summary": {
                "downloaded": len(self.results["downloaded"]),
                "failed": len(self.results["failed"]),
                "skipped": len(self.results["skipped"]),
                "total_size_mb": sum(
                    d["size_bytes"] for d in self.results["downloaded"]
                ) / (1024 * 1024),
            },
            "details": self.results,
        }
        report_file.write_text(json.dumps(report, indent=2))

    def get_summary(self) -> str:
        """Get a human-readable summary of downloads."""
        downloaded = len(self.results["downloaded"])
        failed = len(self.results["failed"])
        skipped = len(self.results["skipped"])
        total_mb = sum(d["size_bytes"] for d in self.results["downloaded"]) / (1024 * 1024)

        return (
            f"Papers: {downloaded} downloaded ({total_mb:.1f} MB), "
            f"{failed} failed, {skipped} skipped"
        )


def download_papers(refs_file: Path, output_dir: Path,
                    progress_callback: Optional[callable] = None) -> dict:
    """Convenience function to download papers.

    Args:
        refs_file: Path to references file
        output_dir: Directory for PDFs
        progress_callback: Optional callback(current, total, url, success)

    Returns:
        Results dict
    """
    downloader = PaperDownloader(output_dir)
    return downloader.download_all(refs_file)
