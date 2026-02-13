#!/usr/bin/env python3
"""Paper source handlers for different academic repositories.

Each source class handles downloading from a specific repository type.
"""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


@dataclass
class PaperResult:
    """Result from a paper download attempt."""
    success: bool
    filepath: Optional[Path] = None
    error: Optional[str] = None
    source: Optional[str] = None
    size_bytes: int = 0


class BaseSource:
    """Base class for paper sources."""

    name: str = "base"
    timeout: int = 60

    def can_handle(self, url: str) -> bool:
        """Check if this source can handle the given URL."""
        raise NotImplementedError

    def download(self, url: str, output_dir: Path) -> PaperResult:
        """Download the paper to the output directory."""
        raise NotImplementedError

    def _safe_filename(self, name: str, max_length: int = 100) -> str:
        """Convert string to safe filename."""
        # Remove/replace problematic characters
        name = re.sub(r'[<>:"/\\|?*]', '_', name)
        name = re.sub(r'\s+', '_', name)
        name = name.strip('._')
        if len(name) > max_length:
            name = name[:max_length]
        return name

    def _get_with_retry(self, url: str, max_retries: int = 3,
                        **kwargs) -> Optional[requests.Response]:
        """GET request with retry logic."""
        if not HAS_REQUESTS:
            return None

        # Add User-Agent header to avoid blocks
        headers = kwargs.pop('headers', {})
        if 'User-Agent' not in headers:
            headers['User-Agent'] = 'deep-report/1.0 (Academic Research Tool)'

        for attempt in range(max_retries):
            resp = None
            try:
                resp = requests.get(url, timeout=self.timeout, headers=headers, **kwargs)
                if resp.status_code == 200:
                    return resp  # Caller must close the response after use
                if resp.status_code == 429:  # Rate limit
                    time.sleep(5 * (attempt + 1))
                    continue
                if resp.status_code >= 500:  # Server error
                    time.sleep(2 * (attempt + 1))
                    continue
                return resp  # Client error, don't retry
            except requests.exceptions.Timeout:
                time.sleep(2 * (attempt + 1))
            except requests.exceptions.RequestException:
                return None
            finally:
                if resp is not None and resp.status_code != 200:
                    try:
                        resp.close()
                    except Exception:
                        pass
        return None


class ArxivSource(BaseSource):
    """Handler for arXiv papers."""

    name = "arxiv"
    PATTERN = re.compile(r'arxiv\.org/(?:abs|pdf)/(\d+\.\d+)')
    PATTERN_OLD = re.compile(r'arxiv\.org/(?:abs|pdf)/([a-z\-]+/\d+)')

    def can_handle(self, url: str) -> bool:
        return 'arxiv.org' in url

    def download(self, url: str, output_dir: Path) -> PaperResult:
        if not HAS_REQUESTS:
            return PaperResult(False, error="requests library not installed", source=self.name)

        # Extract arXiv ID
        match = self.PATTERN.search(url) or self.PATTERN_OLD.search(url)
        if not match:
            return PaperResult(False, error="Could not parse arXiv ID", source=self.name)

        arxiv_id = match.group(1)
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

        # Create safe filename
        safe_id = arxiv_id.replace('/', '_').replace('.', '_')
        output_path = output_dir / f"arxiv_{safe_id}.pdf"

        try:
            resp = self._get_with_retry(pdf_url, allow_redirects=True)
            if resp is None:
                return PaperResult(False, error="Request failed", source=self.name)

            try:
                if resp.status_code != 200:
                    return PaperResult(False, error=f"HTTP {resp.status_code}", source=self.name)

                content_type = resp.headers.get('content-type', '')
                if 'pdf' not in content_type.lower() and len(resp.content) < 1000:
                    return PaperResult(False, error="Response not a PDF", source=self.name)

                output_path.write_bytes(resp.content)
                return PaperResult(
                    True,
                    filepath=output_path,
                    source=self.name,
                    size_bytes=len(resp.content)
                )
            finally:
                resp.close()
        except Exception as e:
            return PaperResult(False, error=str(e), source=self.name)


class PMCSource(BaseSource):
    """Handler for PubMed Central papers."""

    name = "pmc"
    PATTERN = re.compile(r'ncbi\.nlm\.nih\.gov/pmc/articles/(PMC\d+)')
    PATTERN_ID = re.compile(r'PMC(\d+)')

    def can_handle(self, url: str) -> bool:
        return 'pmc/articles/' in url or 'PMC' in url.upper()

    def download(self, url: str, output_dir: Path) -> PaperResult:
        if not HAS_REQUESTS:
            return PaperResult(False, error="requests library not installed", source=self.name)

        # Extract PMC ID
        match = self.PATTERN.search(url)
        if not match:
            match = self.PATTERN_ID.search(url.upper())
            if match:
                pmc_id = f"PMC{match.group(1)}"
            else:
                return PaperResult(False, error="Could not parse PMC ID", source=self.name)
        else:
            pmc_id = match.group(1)

        # Try multiple PDF URL patterns
        pdf_urls = [
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/",
            f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf",
        ]

        output_path = output_dir / f"{pmc_id}.pdf"

        for pdf_url in pdf_urls:
            try:
                resp = self._get_with_retry(pdf_url, allow_redirects=True)
                if resp is None:
                    continue

                try:
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        output_path.write_bytes(resp.content)
                        return PaperResult(
                            True,
                            filepath=output_path,
                            source=self.name,
                            size_bytes=len(resp.content)
                        )
                finally:
                    resp.close()
            except Exception:
                continue

        return PaperResult(False, error="Could not download PDF", source=self.name)


class OpenAccessSource(BaseSource):
    """Handler for open-access journals (Frontiers, eLife, PLOS, MDPI, Nature OA)."""

    name = "open_access"
    DOMAINS = {
        'frontiersin.org': 'Frontiers',
        'elifesciences.org': 'eLife',
        'plos.org': 'PLOS',
        'plosone.org': 'PLOS ONE',
        'mdpi.com': 'MDPI',
        'nature.com': 'Nature',
        'biomedcentral.com': 'BMC',
        'springeropen.com': 'SpringerOpen',
    }

    def _is_allowed_domain(self, url: str) -> bool:
        """Check if PDF URL is from an allowed domain."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            # Allow subdomains of known publishers
            return any(d in domain for d in self.DOMAINS.keys())
        except Exception:
            return False

    def can_handle(self, url: str) -> bool:
        return any(d in url.lower() for d in self.DOMAINS)

    def download(self, url: str, output_dir: Path) -> PaperResult:
        if not HAS_REQUESTS:
            return PaperResult(False, error="requests library not installed", source=self.name)

        try:
            # First, fetch the article page
            resp = self._get_with_retry(url)
            if resp is None or resp.status_code != 200:
                if resp is not None:
                    resp.close()
                return PaperResult(False, error="Could not fetch article page", source=self.name)

            try:
                # Look for PDF link patterns
                pdf_patterns = [
                    r'href="([^"]+\.pdf[^"]*)"',
                    r'href="([^"]+/pdf/[^"]*)"',
                    r'data-pdf-url="([^"]+)"',
                    r'"pdfUrl"\s*:\s*"([^"]+)"',
                ]

                pdf_url = None
                for pattern in pdf_patterns:
                    match = re.search(pattern, resp.text)
                    if match:
                        pdf_url = match.group(1)
                        break
            finally:
                resp.close()

            if not pdf_url:
                return PaperResult(False, error="No PDF link found on page", source=self.name)

            # Handle relative URLs
            if not pdf_url.startswith('http'):
                pdf_url = urljoin(url, pdf_url)

            # Validate PDF URL is from expected domain
            if not self._is_allowed_domain(pdf_url):
                return PaperResult(False, error="PDF URL from untrusted domain", source=self.name)

            # Validate urljoin result
            if not pdf_url.startswith('http'):
                return PaperResult(False, error="Invalid PDF URL after join", source=self.name)

            # Download PDF
            pdf_resp = self._get_with_retry(pdf_url, allow_redirects=True)
            if pdf_resp is None or pdf_resp.status_code != 200:
                if pdf_resp is not None:
                    pdf_resp.close()
                return PaperResult(False, error="PDF download failed", source=self.name)

            try:
                # Validate final URL after following redirects (SSRF protection)
                if hasattr(pdf_resp, 'url') and not self._is_allowed_domain(pdf_resp.url):
                    return PaperResult(False, error="PDF redirect to untrusted domain", source=self.name)

                # Generate filename from URL
                from urllib.parse import unquote
                parsed = urlparse(pdf_url)
                filename = unquote(Path(parsed.path).name)
                if not filename or filename == 'pdf':
                    # Generate from article URL
                    filename = self._safe_filename(Path(urlparse(url).path).stem) + '.pdf'
                if not filename.endswith('.pdf'):
                    filename += '.pdf'

                output_path = output_dir / filename

                output_path.write_bytes(pdf_resp.content)
                return PaperResult(
                    True,
                    filepath=output_path,
                    source=self.name,
                    size_bytes=len(pdf_resp.content)
                )
            finally:
                pdf_resp.close()

        except Exception as e:
            return PaperResult(False, error=str(e), source=self.name)


class DOISource(BaseSource):
    """Handler for DOI links - attempts to resolve and download."""

    name = "doi"
    PATTERN = re.compile(r'(?:doi\.org/|doi:\s*)(10\.\d{4,}/[^\s]+)')

    # Allowlist of known academic publisher domains
    ALLOWED_DOMAINS = {
        'arxiv.org', 'doi.org', 'ncbi.nlm.nih.gov', 'biorxiv.org', 'medrxiv.org',
        'frontiersin.org', 'elifesciences.org', 'plos.org', 'plosone.org',
        'mdpi.com', 'nature.com', 'biomedcentral.com', 'springeropen.com',
        'springer.com', 'wiley.com', 'elsevier.com', 'sciencedirect.com',
        'ieee.org', 'acm.org', 'aaai.org', 'neurips.cc', 'mlr.press',
        'openreview.net', 'cell.com', 'thelancet.com', 'nejm.org',
        'acs.org', 'rsc.org', 'iop.org', 'aps.org',
    }

    def _is_allowed_domain(self, url: str) -> bool:
        """Check if URL is from an allowed academic domain."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            return any(allowed in domain for allowed in self.ALLOWED_DOMAINS)
        except Exception:
            return False

    def can_handle(self, url: str) -> bool:
        return 'doi.org/' in url or 'doi:' in url.lower()

    def download(self, url: str, output_dir: Path) -> PaperResult:
        if not HAS_REQUESTS:
            return PaperResult(False, error="requests library not installed", source=self.name)

        # Extract DOI
        match = self.PATTERN.search(url)
        if not match:
            return PaperResult(False, error="Could not parse DOI", source=self.name)

        doi = match.group(1).rstrip('.,;:)')
        doi_url = f"https://doi.org/{doi}"

        try:
            # Follow redirect to actual article
            resp = self._get_with_retry(doi_url, allow_redirects=True)
            if resp is None or resp.status_code != 200:
                if resp is not None:
                    resp.close()
                return PaperResult(False, error="DOI redirect failed", source=self.name)

            try:
                # Check if redirected to a known open-access source
                final_url = resp.url
                oa_source = OpenAccessSource()
                if oa_source.can_handle(final_url):
                    resp.close()
                    return oa_source.download(final_url, output_dir)

                # Try to find PDF on the page
                pdf_patterns = [
                    r'href="([^"]+\.pdf[^"]*)"',
                    r'href="([^"]+/pdf/[^"]*)"',
                ]

                for pattern in pdf_patterns:
                    match = re.search(pattern, resp.text)
                    if match:
                        pdf_url = match.group(1)
                        if not pdf_url.startswith('http'):
                            pdf_url = urljoin(final_url, pdf_url)

                        # Validate URL and domain
                        if not pdf_url.startswith('http') or not self._is_allowed_domain(pdf_url):
                            continue

                        pdf_resp = self._get_with_retry(pdf_url, allow_redirects=True)
                        if pdf_resp is None:
                            continue
                        try:
                            # Validate final URL after redirects (SSRF protection)
                            if hasattr(pdf_resp, 'url') and not self._is_allowed_domain(pdf_resp.url):
                                continue
                            if pdf_resp.status_code == 200 and len(pdf_resp.content) > 1000:
                                safe_doi = self._safe_filename(doi)
                                output_path = output_dir / f"doi_{safe_doi}.pdf"
                                output_path.write_bytes(pdf_resp.content)
                                return PaperResult(
                                    True,
                                    filepath=output_path,
                                    source=self.name,
                                    size_bytes=len(pdf_resp.content)
                                )
                        finally:
                            pdf_resp.close()
            finally:
                resp.close()

            return PaperResult(False, error="Could not find open-access PDF", source=self.name)

        except Exception as e:
            return PaperResult(False, error=str(e), source=self.name)


class BiorxivSource(BaseSource):
    """Handler for bioRxiv and medRxiv preprints."""

    name = "biorxiv"
    PATTERN = re.compile(r'(?:biorxiv|medrxiv)\.org/content/(10\.\d+/[\d.]+)')

    def can_handle(self, url: str) -> bool:
        return 'biorxiv.org' in url or 'medrxiv.org' in url

    def download(self, url: str, output_dir: Path) -> PaperResult:
        if not HAS_REQUESTS:
            return PaperResult(False, error="requests library not installed", source=self.name)

        match = self.PATTERN.search(url)
        if not match:
            # Try to extract from full URL
            if '/content/' in url:
                # Construct PDF URL by appending .full.pdf
                pdf_url = url.rstrip('/') + '.full.pdf'
                # Validate the constructed URL
                if not pdf_url.startswith('http'):
                    return PaperResult(False, error="Invalid PDF URL", source=self.name)
            else:
                return PaperResult(False, error="Could not parse biorxiv ID", source=self.name)
        else:
            doi = match.group(1)
            base = 'biorxiv' if 'biorxiv' in url else 'medrxiv'
            pdf_url = f"https://www.{base}.org/content/{doi}.full.pdf"

        try:
            resp = self._get_with_retry(pdf_url, allow_redirects=True)
            if resp is None or resp.status_code != 200:
                if resp is not None:
                    resp.close()
                return PaperResult(False, error=f"HTTP {resp.status_code if resp else 'None'}", source=self.name)

            try:
                # Generate filename
                safe_id = self._safe_filename(url.split('/content/')[-1].split('v')[0])
                output_path = output_dir / f"biorxiv_{safe_id}.pdf"

                output_path.write_bytes(resp.content)
                return PaperResult(
                    True,
                    filepath=output_path,
                    source=self.name,
                    size_bytes=len(resp.content)
                )
            finally:
                resp.close()
        except Exception as e:
            return PaperResult(False, error=str(e), source=self.name)
