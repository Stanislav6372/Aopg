"""TikTok comments scraper.

Reads a list of TikTok video URLs (one per line) from an input file, fetches
all comments for each video, and writes the results to ``comments_output.csv``.

Authentication is performed via cookies provided in ``config.json``. The
script uses the `TikTokApi <https://github.com/davidteather/TikTok-Api>`_
library, which drives a headless Chromium via Playwright. This is required
because TikTok signs every web API request from JS in the browser, so a
plain ``httpx`` client cannot fetch comments without that signing.

Usage::

    python tiktok_comments_scraper.py urls.txt

See ``README.md`` for the full setup procedure (Playwright install, cookies,
etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from TikTokApi import TikTokApi

LOGGER = logging.getLogger("tiktok_scraper")

DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_OUTPUT_PATH = Path("comments_output.csv")
MAX_URLS = 80
MAX_COMMENTS_PER_VIDEO = 10_000
MAX_RETRIES = 3
MIN_DELAY = 3.0
MAX_DELAY = 8.0

VIDEO_ID_PATTERNS = (
    re.compile(r"/video/(\d+)"),
    re.compile(r"/v/(\d+)"),
    re.compile(r"[?&]item_id=(\d+)"),
    re.compile(r"[?&]aweme_id=(\d+)"),
)


@dataclass(frozen=True)
class Config:
    sessionid: str
    msToken: str
    headless: bool = True
    proxy: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise SystemExit(
                f"Config file {path} not found. See config.example.json."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_sessionid = (
            data.get("sessionid")
            or data.get("sid_guard")
            or ""
        ).strip()
        if not raw_sessionid:
            raise SystemExit(
                "config.json must include a non-empty 'sessionid' (or "
                "'sid_guard') value."
            )
        sessionid = normalize_sessionid(raw_sessionid)
        ms_token = (data.get("msToken") or "").strip()
        if not ms_token:
            raise SystemExit(
                "config.json must include a non-empty 'msToken' value. "
                "Copy it from the .tiktok.com cookies in your browser."
            )
        headless_flag = data.get("headless")
        return cls(
            sessionid=sessionid,
            msToken=ms_token,
            headless=True if headless_flag is None else bool(headless_flag),
            proxy=data.get("proxy") or None,
        )


def normalize_sessionid(value: str) -> str:
    """Accept either a plain ``sessionid`` or a ``sid_guard`` cookie value."""
    decoded = value.replace("%7C", "|").replace("%7c", "|")
    head = decoded.split("|", 1)[0].strip()
    return head or value.strip()


@dataclass(frozen=True)
class CommentRow:
    video_url: str
    username: str
    comment: str
    likes: int
    date: str


def extract_video_id(url: str) -> str | None:
    for pattern in VIDEO_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


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


def build_cookie_payload(config: Config) -> list[dict[str, object]]:
    """Build the cookies list passed to ``TikTokApi.create_sessions``."""
    return [
        {
            "name": "sessionid",
            "value": config.sessionid,
            "domain": ".tiktok.com",
            "path": "/",
        }
    ]


async def random_delay() -> None:
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    LOGGER.debug("Sleeping %.2fs", delay)
    await asyncio.sleep(delay)


def parse_create_time(raw: object) -> str:
    if not raw:
        return ""
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return ""


def comment_to_row(comment: object, video_url: str) -> CommentRow:
    data = getattr(comment, "as_dict", {}) or {}
    user = data.get("user") or {}
    username = (
        user.get("unique_id")
        or user.get("nickname")
        or getattr(getattr(comment, "author", None), "username", "")
        or ""
    )
    text = getattr(comment, "text", None) or data.get("text") or ""
    likes = int(
        getattr(comment, "likes_count", None) or data.get("digg_count") or 0
    )
    date = parse_create_time(data.get("create_time"))
    return CommentRow(
        video_url=video_url,
        username=username,
        comment=text,
        likes=likes,
        date=date,
    )


async def fetch_video_comments(
    api: TikTokApi, raw_url: str
) -> tuple[list[CommentRow], str | None]:
    """Return (rows, error). ``error`` is None on success."""
    video_id = extract_video_id(raw_url)
    if not video_id:
        return [], f"Could not parse video id from {raw_url}"
    last_error: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            video = api.video(id=video_id, url=raw_url)
            rows: list[CommentRow] = []
            async for comment in video.comments(count=MAX_COMMENTS_PER_VIDEO):
                rows.append(comment_to_row(comment, raw_url))
            return rows, None
        except Exception as exc:  # noqa: BLE001 - we want to retry on anything
            last_error = f"{type(exc).__name__}: {exc}"
            backoff = min(60.0, (2 ** attempt) + random.uniform(0, 1.5))
            LOGGER.warning(
                "Attempt %d/%d for %s failed: %s. Sleeping %.1fs",
                attempt,
                MAX_RETRIES,
                raw_url,
                last_error,
                backoff,
            )
            await asyncio.sleep(backoff)
    return [], last_error


async def collect(
    urls: list[str], config: Config
) -> tuple[list[CommentRow], list[str]]:
    rows: list[CommentRow] = []
    failures: list[str] = []
    proxies = [config.proxy] if config.proxy else None
    async with TikTokApi() as api:
        await api.create_sessions(
            ms_tokens=[config.msToken],
            num_sessions=1,
            sleep_after=3,
            headless=config.headless,
            cookies=build_cookie_payload(config),
            proxies=proxies,
        )
        for index, raw_url in enumerate(urls, start=1):
            print(
                f"[{index}/{len(urls)}] Fetching comments for {raw_url}",
                flush=True,
            )
            video_rows, error = await fetch_video_comments(api, raw_url)
            if error:
                LOGGER.error("Failed %s: %s", raw_url, error)
                failures.append(raw_url)
            else:
                print(f"    -> {len(video_rows)} comments", flush=True)
                rows.extend(video_rows)
            if index < len(urls):
                await random_delay()
    return rows, failures


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


async def amain(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    urls = read_urls(args.input)
    print(f"Loaded {len(urls)} URLs from {args.input}", flush=True)
    rows, failures = await collect(urls, config)
    written = write_csv(rows, args.output)
    print(f"Wrote {written} comments to {args.output}", flush=True)
    if failures:
        print(f"Failed videos ({len(failures)}):", flush=True)
        for url in failures:
            print(f"  - {url}", flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
