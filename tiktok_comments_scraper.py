"""TikTok comments scraper.

Reads a list of TikTok video URLs (one per line) from an input file, fetches
all comments for each video using the public TikTok web API, and writes the
results to ``comments_output.csv``.

Usage:
    python tiktok_comments_scraper.py urls.txt

Configuration:
    A ``config.json`` file in the working directory must provide the
    ``sessionid`` cookie copied from a logged-in browser session. Optional
    fields are documented in ``config.example.json``.

The script is intentionally conservative: it sleeps a random 3-8 seconds
between paginated requests, retries failed requests with exponential backoff,
and prints progress for every video.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import httpx

LOGGER = logging.getLogger("tiktok_scraper")

DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_OUTPUT_PATH = Path("comments_output.csv")
COMMENT_API_URL = "https://www.tiktok.com/api/comment/list/"
PAGE_SIZE = 20
MAX_URLS = 80
MAX_RETRIES = 4
MIN_DELAY = 3.0
MAX_DELAY = 8.0

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

VIDEO_ID_PATTERNS = (
    re.compile(r"/video/(\d+)"),
    re.compile(r"/v/(\d+)"),
    re.compile(r"[?&]item_id=(\d+)"),
    re.compile(r"[?&]aweme_id=(\d+)"),
)


@dataclass(frozen=True)
class Config:
    sessionid: str
    msToken: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    proxy: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise SystemExit(
                f"Config file {path} not found. See config.example.json."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        sessionid = data.get("sessionid", "").strip()
        if not sessionid:
            raise SystemExit(
                "config.json must include a non-empty 'sessionid' value."
            )
        return cls(
            sessionid=sessionid,
            msToken=data.get("msToken") or None,
            user_agent=data.get("user_agent") or DEFAULT_USER_AGENT,
            proxy=data.get("proxy") or None,
        )


@dataclass(frozen=True)
class CommentRow:
    video_url: str
    username: str
    comment: str
    likes: int
    date: str


def extract_video_id(url: str) -> str | None:
    """Extract the numeric video id from a TikTok URL."""
    for pattern in VIDEO_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def resolve_short_url(client: httpx.Client, url: str) -> str:
    """Follow redirects for vm.tiktok.com / vt.tiktok.com short links."""
    try:
        response = client.get(url, follow_redirects=True, timeout=15.0)
        return str(response.url)
    except httpx.HTTPError as exc:
        LOGGER.warning("Failed to resolve short URL %s: %s", url, exc)
        return url


def read_urls(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit(f"Input file {path} not found.")
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    if not urls:
        raise SystemExit(f"No URLs found in {path}.")
    if len(urls) > MAX_URLS:
        raise SystemExit(
            f"Input file contains {len(urls)} URLs but the limit is {MAX_URLS}."
        )
    return urls


def build_client(config: Config) -> httpx.Client:
    headers = {
        "User-Agent": config.user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    }
    cookies = {"sessionid": config.sessionid}
    if config.msToken:
        cookies["msToken"] = config.msToken
    return httpx.Client(
        headers=headers,
        cookies=cookies,
        timeout=20.0,
        proxy=config.proxy,
        follow_redirects=True,
    )


def random_delay() -> None:
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    LOGGER.debug("Sleeping %.2fs", delay)
    time.sleep(delay)


def fetch_with_retries(
    client: httpx.Client, params: dict[str, str | int]
) -> dict:
    """GET the comment list endpoint with retry + exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.get(COMMENT_API_URL, params=params)
            if response.status_code == 200:
                # TikTok occasionally returns an empty body on rate limits.
                if not response.content:
                    raise httpx.HTTPError("Empty response body")
                return response.json()
            if response.status_code in (403, 429) or response.status_code >= 500:
                raise httpx.HTTPError(
                    f"HTTP {response.status_code} from TikTok"
                )
            response.raise_for_status()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_exc = exc
            backoff = min(60.0, (2 ** attempt) + random.uniform(0, 1.5))
            LOGGER.warning(
                "Request failed (attempt %d/%d): %s. Sleeping %.1fs",
                attempt,
                MAX_RETRIES,
                exc,
                backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(f"Giving up after {MAX_RETRIES} retries: {last_exc}")


def parse_comment(item: dict, video_url: str) -> CommentRow:
    user = item.get("user") or {}
    username = (
        user.get("unique_id")
        or user.get("nickname")
        or user.get("sec_uid")
        or ""
    )
    text = item.get("text") or ""
    likes = int(item.get("digg_count") or 0)
    create_time = item.get("create_time")
    if create_time:
        date = datetime.fromtimestamp(int(create_time), tz=timezone.utc).isoformat()
    else:
        date = ""
    return CommentRow(
        video_url=video_url,
        username=username,
        comment=text,
        likes=likes,
        date=date,
    )


def iter_comments(
    client: httpx.Client, video_id: str, video_url: str
) -> Iterator[CommentRow]:
    cursor = 0
    total_seen = 0
    while True:
        params = {
            "aweme_id": video_id,
            "count": PAGE_SIZE,
            "cursor": cursor,
            "aid": 1988,
            "app_language": "en",
            "device_platform": "web_pc",
        }
        payload = fetch_with_retries(client, params)
        comments = payload.get("comments") or []
        for item in comments:
            yield parse_comment(item, video_url)
        total_seen += len(comments)
        has_more = bool(payload.get("has_more"))
        next_cursor = payload.get("cursor")
        if not has_more or next_cursor in (None, cursor):
            LOGGER.info("Fetched %d comments for %s", total_seen, video_id)
            return
        cursor = int(next_cursor)
        random_delay()


def write_csv(rows: Iterable[CommentRow], path: Path) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["video_url", "username", "comment", "likes", "date"])
        for row in rows:
            writer.writerow(
                [row.video_url, row.username, row.comment, row.likes, row.date]
            )
            count += 1
    return count


def collect(
    urls: list[str], config: Config, output: Path
) -> tuple[int, list[str]]:
    failures: list[str] = []
    rows: list[CommentRow] = []
    with build_client(config) as client:
        for index, raw_url in enumerate(urls, start=1):
            url = raw_url
            if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
                url = resolve_short_url(client, url)
            video_id = extract_video_id(url)
            if not video_id:
                LOGGER.error("[%d/%d] Could not parse video id from %s",
                             index, len(urls), raw_url)
                failures.append(raw_url)
                continue
            print(
                f"[{index}/{len(urls)}] Fetching comments for {url}",
                flush=True,
            )
            try:
                video_rows = list(iter_comments(client, video_id, url))
            except Exception as exc:  # noqa: BLE001 - surface any error per video
                LOGGER.error("Failed to fetch %s: %s", url, exc)
                failures.append(raw_url)
                continue
            rows.extend(video_rows)
            print(f"    -> {len(video_rows)} comments", flush=True)
            if index < len(urls):
                random_delay()
    written = write_csv(rows, output)
    return written, failures


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        type=Path,
        help="Path to a text file with up to 80 TikTok URLs (one per line).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.json (default: ./config.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Path to output CSV (default: ./comments_output.csv).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.load(args.config)
    urls = read_urls(args.input)
    print(f"Loaded {len(urls)} URLs from {args.input}", flush=True)
    written, failures = collect(urls, config, args.output)
    print(f"Wrote {written} comments to {args.output}", flush=True)
    if failures:
        print(f"Failed videos ({len(failures)}):", flush=True)
        for url in failures:
            print(f"  - {url}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
