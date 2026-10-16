"""Site Grabber: crawl a website and collect links to downloadable files."""
from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlsplit

import requests

_LINK_ATTRS = {
    "a": "href", "area": "href", "img": "src", "video": "src", "audio": "src",
    "source": "src", "embed": "src", "iframe": "src", "frame": "src", "link": "href",
    "track": "src", "object": "data",
}


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "base" and a.get("href"):
            self.base = a["href"]
        attr = _LINK_ATTRS.get(tag)
        if attr and a.get(attr):
            self.links.append(a[attr])  # type: ignore[arg-type]
        if tag in ("img", "source") and a.get("srcset"):
            for part in a["srcset"].split(","):  # type: ignore[union-attr]
                url = part.strip().split(" ")[0]
                if url:
                    self.links.append(url)


def extract_links(html: str, page_url: str) -> list[str]:
    p = _LinkParser()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML
        pass
    base = urljoin(page_url, p.base) if p.base else page_url
    out = []
    for link in p.links:
        link = link.strip()
        if link.startswith(("javascript:", "mailto:", "data:", "tel:", "#")):
            continue
        out.append(urldefrag(urljoin(base, link))[0])
    return out


@dataclass
class GrabOptions:
    start_url: str
    depth: int = 1
    same_domain: bool = True
    extensions: tuple[str, ...] = ("jpg", "jpeg", "png", "gif", "webp", "mp4", "mp3", "zip", "pdf")
    include_regex: str = ""
    exclude_regex: str = ""
    max_pages: int = 300


def _ext(url: str) -> str:
    path = urlsplit(url).path
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1].lower() if "." in last else ""


def grab(opts: GrabOptions, session: requests.Session, stop: threading.Event | None = None,
         on_progress: Callable[[int, int, str], None] | None = None) -> list[tuple[str, str]]:
    """BFS crawl. Returns [(file_url, found_on_page)]."""
    host = urlsplit(opts.start_url).hostname
    exts = {e.lower().lstrip(".") for e in opts.extensions}
    inc = re.compile(opts.include_regex) if opts.include_regex else None
    exc = re.compile(opts.exclude_regex) if opts.exclude_regex else None
    seen_pages: set[str] = set()
    found: dict[str, str] = {}
    todo: deque[tuple[str, int]] = deque([(opts.start_url, 0)])
    pages = 0
    while todo and pages < opts.max_pages:
        if stop is not None and stop.is_set():
            break
        url, depth = todo.popleft()
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException:
            continue
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype:
            continue
        pages += 1
        if on_progress:
            on_progress(pages, len(found), url)
        for link in extract_links(r.text, r.url):
            if urlsplit(link).scheme not in ("http", "https"):
                continue
            if exc and exc.search(link):
                continue
            e = _ext(link)
            if e in exts and (inc is None or inc.search(link)):
                found.setdefault(link, url)
                continue
            if depth + 1 <= opts.depth and link not in seen_pages:
                if opts.same_domain and urlsplit(link).hostname != host:
                    continue
                if e in ("", "html", "htm", "php", "asp", "aspx", "jsp", "shtml"):
                    todo.append((link, depth + 1))
    return list(found.items())
