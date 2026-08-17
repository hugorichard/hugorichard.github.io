#!/usr/bin/env python3
"""Fetch Google Scholar publications and write Jekyll collection files."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_PROFILE_URL = "https://scholar.google.fr/citations?user=5KaKAOgAAAAJ"
ROOT = Path(__file__).resolve().parents[1]
PUBLICATIONS_DIR = ROOT / "_publications"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class ScholarPublication:
    title: str
    authors: str
    venue: str
    year: str
    citation_url: str
    source_url: str = ""
    pdf: str = ""
    code: str = ""
    talk: str = ""
    scholar_id: str = ""


class ScholarListParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.rows: list[dict[str, str | list[str]]] = []
        self._row: dict[str, str | list[str]] | None = None
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if tag == "tr" and "gsc_a_tr" in classes:
            self._row = {"gray": []}
            return

        if self._row is None:
            return

        if tag == "a" and "gsc_a_at" in classes:
            self._row["citation_url"] = urljoin(self.base_url, html.unescape(attr.get("href") or ""))
            self._start_capture("title", tag)
        elif tag == "div" and "gs_gray" in classes:
            self._start_capture("gray", tag)
        elif tag == "span" and "gsc_a_h" in classes and self._capture != "gray":
            self._start_capture("year", tag)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return

        if self._capture is not None and tag == self._capture_tag:
            value = collapse_ws("".join(self._capture_text))
            if self._capture == "gray":
                self._row.setdefault("gray", [])
                assert isinstance(self._row["gray"], list)
                self._row["gray"].append(value)
            else:
                self._row[self._capture] = value
            self._capture = None
            self._capture_tag = None
            self._capture_text = []

        if tag == "tr":
            if self._row.get("title"):
                self.rows.append(self._row)
            self._row = None

    def _start_capture(self, name: str, tag: str) -> None:
        self._capture = name
        self._capture_tag = tag
        self._capture_text = []


class ScholarDetailParser(HTMLParser):
    WANTED_FIELDS = {
        "Authors",
        "Publication date",
        "Journal",
        "Conference",
        "Book",
        "Institution",
        "Volume",
        "Issue",
        "Pages",
        "Publisher",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.source_url = ""
        self.pdf = ""
        self.title = ""
        self._pending_field = ""
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._capture_text: list[str] = []
        self._in_pdf_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())

        if tag == "a" and "gsc_oci_title_link" in classes:
            self.source_url = html.unescape(attr.get("href") or "")
            self._start_capture("title", tag)
        elif tag == "div" and "gsc_oci_title_ggi" in classes:
            self._in_pdf_link = True
        elif self._in_pdf_link and tag == "a" and not self.pdf:
            self.pdf = html.unescape(attr.get("href") or "")
        elif tag == "div" and "gsc_oci_field" in classes:
            self._start_capture("field", tag)
        elif tag == "div" and "gsc_oci_value" in classes and self._pending_field in self.WANTED_FIELDS:
            self._start_capture("value", tag)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is not None and tag == self._capture_tag:
            value = collapse_ws("".join(self._capture_text))
            if self._capture == "field":
                self._pending_field = value
            elif self._capture == "value":
                self.fields[self._pending_field] = value
                self._pending_field = ""
            elif self._capture == "title":
                self.title = value
            self._capture = None
            self._capture_tag = None
            self._capture_text = []

        if tag == "div" and self._in_pdf_link:
            self._in_pdf_link = False

    def _start_capture(self, name: str, tag: str) -> None:
        self._capture = name
        self._capture_tag = tag
        self._capture_text = []


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_title(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return collapse_ws(value)


def slugify(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-") or "publication"


def fetch_text(url: str, attempts: int = 3) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except HTTPError as exc:
            if exc.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(30 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}")


def profile_page_url(profile_url: str, cstart: int) -> str:
    parsed = urlparse(profile_url)
    query = parse_qs(parsed.query)
    query["hl"] = ["en"]
    query["pagesize"] = ["100"]
    if cstart:
        query["cstart"] = [str(cstart)]
    else:
        query.pop("cstart", None)
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def parse_publication_rows(profile_url: str, max_pages: int) -> list[dict[str, str | list[str]]]:
    base = f"{urlparse(profile_url).scheme}://{urlparse(profile_url).netloc}"
    rows: list[dict[str, str | list[str]]] = []
    seen_titles: set[str] = set()
    cstart = 0

    for _ in range(max_pages):
        parser = ScholarListParser(base)
        parser.feed(fetch_text(profile_page_url(profile_url, cstart)))
        new_rows = []
        for row in parser.rows:
            key = normalize_title(str(row.get("title", "")))
            if key and key not in seen_titles:
                seen_titles.add(key)
                new_rows.append(row)
        rows.extend(new_rows)
        if len(parser.rows) < 100:
            break
        cstart += 100

    return rows


def parse_existing_extras(publications_dir: Path) -> dict[str, dict[str, str]]:
    extras: dict[str, dict[str, str]] = {}
    if not publications_dir.exists():
        return extras

    for path in publications_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
        title = read_frontmatter_value(frontmatter, "title")
        if not title:
            continue
        data = {}
        for key in ("code", "talk", "pdf"):
            value = read_frontmatter_value(frontmatter, key)
            if value:
                data[key] = value
        if data:
            extras[normalize_title(title)] = data
    return extras


def read_frontmatter_value(frontmatter: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.+?)\s*$", frontmatter)
    if not match:
        return ""
    value = match.group(1).strip()
    if value and value[0] in {"'", '"'} and value[-1:] == value[0]:
        value = value[1:-1]
    return html.unescape(value)


def publication_from_row(row: dict[str, str | list[str]], extras: dict[str, dict[str, str]]) -> ScholarPublication:
    citation_url = str(row.get("citation_url") or "")
    detail = ScholarDetailParser()
    if citation_url:
        try:
            detail.feed(fetch_text(citation_url))
        except (HTTPError, URLError, TimeoutError) as exc:
            print(
                f"Warning: could not fetch details for {row.get('title', 'publication')}: {exc}",
                file=sys.stderr,
            )

    gray = row.get("gray") or []
    assert isinstance(gray, list)
    title = detail.title or str(row.get("title") or "")
    venue = gray[1] if len(gray) > 1 else venue_from_fields(detail.fields)
    year = publication_year(detail.fields.get("Publication date", "") or str(row.get("year") or ""))
    authors = detail.fields.get("Authors") or (gray[0] if gray else "")
    key = normalize_title(title)
    existing = extras.get(key, {})

    scholar_id = ""
    if citation_url:
        scholar_id = parse_qs(urlparse(citation_url).query).get("citation_for_view", [""])[0]

    return ScholarPublication(
        title=title,
        authors=highlight_author(authors),
        venue=venue,
        year=year,
        citation_url=citation_url,
        source_url=detail.source_url,
        pdf=detail.pdf or existing.get("pdf", ""),
        code=existing.get("code", ""),
        talk=existing.get("talk", ""),
        scholar_id=scholar_id,
    )


def venue_from_fields(fields: dict[str, str]) -> str:
    venue = fields.get("Journal") or fields.get("Conference") or fields.get("Book") or fields.get("Institution") or ""
    parts = [venue]
    if fields.get("Volume"):
        volume = fields["Volume"]
        if fields.get("Issue"):
            volume = f"{volume} ({fields['Issue']})"
        parts.append(volume)
    if fields.get("Pages"):
        parts.append(fields["Pages"])
    year = publication_year(fields.get("Publication date", ""))
    if year:
        parts.append(year)
    return ", ".join(part for part in parts if part)


def publication_year(value: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", value)
    return match.group(0) if match else "1900"


def highlight_author(authors: str) -> str:
    authors = re.sub(r"\bHugo Richard\b", "<strong>Hugo Richard</strong>", authors)
    authors = re.sub(r"\bH Richard\b", "<strong>H. Richard</strong>", authors)
    return authors


def yaml_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def render_publication(publication: ScholarPublication, index: int) -> tuple[str, str]:
    year = publication.year if publication.year else "1900"
    filename = f"{year}-01-01-{index + 1:03d}-{slugify(publication.title)}.md"
    lines = [
        "---",
        f"title: {yaml_value(publication.title)}",
        "collection: publications",
        f"date: {year}-01-01",
        f"venue: {yaml_value(publication.venue)}",
        f"authors: {yaml_value(publication.authors)}",
    ]
    if publication.pdf:
        lines.append(f"pdf: {yaml_value(publication.pdf)}")
    if publication.source_url:
        lines.append(f"source: {yaml_value(publication.source_url)}")
    if publication.code:
        lines.append(f"code: {yaml_value(publication.code)}")
    if publication.talk:
        lines.append(f"talk: {yaml_value(publication.talk)}")
    if publication.citation_url:
        lines.append(f"scholar: {yaml_value(publication.citation_url)}")
    if publication.scholar_id:
        lines.append(f"scholar_id: {yaml_value(publication.scholar_id)}")
    lines.append("---")
    lines.append("")
    return filename, "\n".join(lines)


def write_publications(publications: Iterable[ScholarPublication], publications_dir: Path, dry_run: bool) -> list[Path]:
    publications_dir.mkdir(parents=True, exist_ok=True)
    rendered = [render_publication(publication, index) for index, publication in enumerate(publications)]
    paths = [publications_dir / filename for filename, _ in rendered]

    if dry_run:
        return paths

    for path in publications_dir.glob("*.md"):
        path.unlink()
    for path, (_, content) in zip(paths, rendered):
        path.write_text(content, encoding="utf-8")
    return paths


def fetch_publications(profile_url: str, max_pages: int, request_delay: float) -> list[ScholarPublication]:
    extras = parse_existing_extras(PUBLICATIONS_DIR)
    rows = parse_publication_rows(profile_url, max_pages=max_pages)
    publications = []
    for row in rows:
        publications.append(publication_from_row(row, extras))
        if request_delay:
            time.sleep(request_delay)
    return publications


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-url", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    publications = fetch_publications(args.profile_url, args.max_pages, args.request_delay)
    if not publications:
        raise SystemExit("No publications found. Google Scholar may have blocked the request.")

    paths = write_publications(publications, PUBLICATIONS_DIR, dry_run=args.dry_run)
    action = "Would write" if args.dry_run else "Wrote"
    print(f"{action} {len(paths)} publications to {PUBLICATIONS_DIR.relative_to(ROOT)}")
    for path in paths:
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
