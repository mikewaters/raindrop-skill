#!/usr/bin/env python3
"""Fetch and extract readable text from a Raindrop.io bookmark.

Retrieves content from either Raindrop's cached/permanent copy or the live URL
behind a bookmark, extracts readable text from HTML, and outputs it to stdout
with YAML frontmatter metadata.

Usage:
    uv run scripts/fetch_content.py <raindrop_id> --source {cache,live} [--max-chars N]

Requires RAINDROP_TOKEN environment variable.
Zero external dependencies -- stdlib only.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from http.client import HTTPResponse

BASE_URL = "https://api.raindrop.io/rest/v1"
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 60
DEFAULT_MAX_CHARS = 100_000

SKIP_TAGS = frozenset({
    "script", "style", "nav", "footer", "header", "aside",
    "iframe", "noscript", "svg", "form",
})

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# HTML text extraction (stdlib only)
# ---------------------------------------------------------------------------

class TextExtractor(HTMLParser):
    """Extract readable text from HTML, skipping non-content elements."""

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        attr_dict = dict(attrs)
        if attr_dict.get("role") in ("navigation", "banner", "complementary"):
            self._skip_depth += 1
            return
        if attr_dict.get("aria-hidden") == "true":
            self._skip_depth += 1
            return

    def handle_endtag(self, tag: str):
        if self._skip_depth > 0:
            # Decrement on any end tag while skipping. HTMLParser doesn't
            # guarantee perfect nesting, but depth tracking is good enough
            # for stripping boilerplate.
            self._skip_depth -= 1

    def handle_data(self, data: str):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.chunks.append(text)


def extract_text(html: str) -> str:
    """Parse HTML and return cleaned readable text."""
    parser = TextExtractor()
    parser.feed(html)
    raw = "\n".join(parser.chunks)
    # Collapse 3+ consecutive newlines to 2
    cleaned = re.sub(r"\n{3,}", "\n\n", raw)
    # Strip trailing whitespace per line
    cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
    return cleaned.strip()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Capture redirect responses instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectCaptured(newurl, code)


class RedirectCaptured(Exception):
    def __init__(self, url: str, code: int):
        self.url = url
        self.code = code
        super().__init__(f"{code} -> {url}")


def api_get(endpoint: str, token: str, follow_redirects: bool = True) -> HTTPResponse:
    """Make an authenticated GET to the Raindrop API."""
    url = f"{BASE_URL}{endpoint}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    if follow_redirects:
        return urllib.request.urlopen(req, timeout=API_TIMEOUT)
    else:
        opener = urllib.request.build_opener(NoRedirectHandler)
        return opener.open(req, timeout=API_TIMEOUT)


def fetch_url(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    """Fetch a URL with browser-like headers. Returns raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp.read()


# ---------------------------------------------------------------------------
# Raindrop fetch logic
# ---------------------------------------------------------------------------

def get_raindrop_metadata(raindrop_id: int, token: str) -> dict:
    """Fetch raindrop metadata. Returns the item dict."""
    try:
        resp = api_get(f"/raindrop/{raindrop_id}", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            die(f"Raindrop {raindrop_id} not found")
        raise
    data = json.loads(resp.read())
    if not data.get("result"):
        die(f"API error fetching raindrop {raindrop_id}")
    return data["item"]


def fetch_cache_html(raindrop_id: int, token: str) -> str:
    """Fetch the cached/permanent copy HTML from Raindrop."""
    # The cache endpoint returns a 307 redirect to a presigned S3 URL.
    # We must NOT forward the Bearer token to S3 (it rejects unknown auth).
    try:
        api_get(f"/raindrop/{raindrop_id}/cache", token, follow_redirects=False)
        # If we get here without redirect, something unexpected happened
        die("Cache endpoint did not redirect as expected")
    except RedirectCaptured as rc:
        s3_url = rc.url
    except urllib.error.HTTPError as e:
        if e.code == 403:
            die("Cache requires Raindrop PRO subscription")
        if e.code == 404:
            die("No cached copy available for this raindrop")
        raise

    try:
        raw = fetch_url(s3_url)
    except urllib.error.HTTPError as e:
        die(f"Failed to download cached copy: HTTP {e.code}")
    except Exception as e:
        die(f"Failed to download cached copy: {e}")

    return raw.decode("utf-8", errors="replace")


def fetch_live_html(url: str) -> str:
    """Fetch the live URL content."""
    try:
        raw = fetch_url(url)
    except urllib.error.HTTPError as e:
        die(f"Failed to fetch live URL {url}: HTTP {e.code}")
    except urllib.error.URLError as e:
        die(f"Failed to fetch live URL {url}: {e.reason}")
    except Exception as e:
        die(f"Failed to fetch live URL {url}: {e}")

    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_output(text: str, metadata: dict, source: str,
                  raindrop_id: int, max_chars: int) -> str:
    """Build frontmatter + text output, with optional truncation."""
    title = metadata.get("title", "")
    url = metadata.get("link", "")

    header = (
        f"---\n"
        f"title: {title}\n"
        f"url: {url}\n"
        f"source: {source}\n"
        f"raindrop_id: {raindrop_id}\n"
        f"---\n\n"
    )

    if not text:
        return header + "[No readable content extracted]"

    total = len(text)
    if max_chars > 0 and total > max_chars:
        text = text[:max_chars]
        text += f"\n\n[TRUNCATED at {max_chars} chars, {total} total]"

    return header + text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch and extract readable text from a Raindrop.io bookmark."
    )
    parser.add_argument("raindrop_id", type=int, help="Numeric raindrop ID")
    parser.add_argument(
        "--source", required=True, choices=["cache", "live"],
        help="Content source: 'cache' for Raindrop's permanent copy, "
             "'live' for the current live URL"
    )
    parser.add_argument(
        "--max-chars", type=int, default=DEFAULT_MAX_CHARS,
        help=f"Maximum output characters (default: {DEFAULT_MAX_CHARS}, 0 = no limit)"
    )
    args = parser.parse_args()

    token = os.environ.get("RAINDROP_TOKEN", "")
    if not token:
        die(
            "RAINDROP_TOKEN not set\n"
            "Get token from: https://app.raindrop.io/settings/integrations\n"
            "Then: export RAINDROP_TOKEN='your_token'"
        )

    # Fetch metadata first (needed for both paths)
    metadata = get_raindrop_metadata(args.raindrop_id, token)

    if args.source == "cache":
        cache_info = metadata.get("cache", {})
        status = cache_info.get("status", "unknown")
        if status != "ready":
            die(f"Cache not ready (status: {status}). Try again later.")
        html = fetch_cache_html(args.raindrop_id, token)
    else:
        url = metadata.get("link", "")
        if not url:
            die(f"Raindrop {args.raindrop_id} has no URL")
        html = fetch_live_html(url)

    text = extract_text(html)
    output = format_output(text, metadata, args.source, args.raindrop_id, args.max_chars)
    print(output)


if __name__ == "__main__":
    main()
