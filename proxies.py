"""
Free proxy scraper + tester. No extra deps (requests + httpx already installed).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import requests

# ── Sources ────────────────────────────────────────────────────────────────
# ponytail: static list. If sources go down, update URLs or add new ones.
PROXY_SOURCES: list[tuple[str, str, str]] = [
    ("txt", "HTTP", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt"),
    ("txt", "SOCKS4", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks4.txt"),
    ("txt", "SOCKS5", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    ("txt", "HTTP", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all"),
    ("txt", "HTTP", "https://www.proxy-list.download/api/v1/get?type=http"),
    ("html", "HTTP", "https://free-proxy-list.net/"),
    ("html", "HTTP", "https://www.sslproxies.org/"),
    ("txt", "HTTP", "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt"),
]

TEST_URL = "https://api.ipify.org?format=json"
TEST_TIMEOUT = 8  # seconds per proxy


def _scrape_source(fmt: str, ptype: str, url: str) -> set[str]:
    """Fetch one source, return {ip:port} set."""
    proxies: set[str] = set()
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        text = r.text
    except Exception:
        return proxies

    if fmt == "txt":
        for line in text.splitlines():
            line = line.strip()
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", line):
                proxies.add(line)
    elif fmt == "html":
        # Tables: free-proxy-list.net style
        for m in re.finditer(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})</td>\s*<td>(\d+)", text):
            proxies.add(f"{m.group(1)}:{m.group(2)}")

    return proxies


def scrape_all() -> list[str]:
    """Scrape all sources, return deduplicated list of ip:port."""
    all_proxies: set[str] = set()
    for fmt, ptype, url in PROXY_SOURCES:
        found = _scrape_source(fmt, ptype, url)
        all_proxies.update(found)
    result = sorted(all_proxies)
    return result


async def test_one(proxy: str) -> str | None:
    """Test a single proxy. Returns proxy string if works, None if not."""
    try:
        proxy_url = f"http://{proxy}"
        async with httpx.AsyncClient(
            proxy=httpx.Proxy(url=proxy_url),
            timeout=TEST_TIMEOUT,
        ) as client:
            r = await client.get(TEST_URL)
            if r.status_code == 200:
                return proxy
    except Exception:
        pass
    return None


async def test_batch(proxies: list[str], batch_size: int = 50) -> list[str]:
    """Test proxies concurrently, return working ones."""
    working: list[str] = []
    for i in range(0, len(proxies), batch_size):
        batch = proxies[i : i + batch_size]
        results = await asyncio.gather(*[test_one(p) for p in batch])
        working.extend(p for p in results if p)
    return working
