# TikTok Comments Scraper

A small Python script that reads a list of TikTok video URLs and writes every
comment (username, text, likes, timestamp) to a CSV file.

## How it works

TikTok's web API signs every comment request from JavaScript inside the
browser. A plain `httpx` client therefore cannot fetch comments. This script
uses the [`TikTokApi`](https://github.com/davidteather/TikTok-Api) library,
which drives a headless Chromium via Playwright to perform that signing for
us, then iterates over every comment of every video.

## Features

- Input: text file with up to **80** TikTok video URLs, one per line.
- Authenticates via the `sessionid` (or `sid_guard`) and `msToken` cookies
  from a logged-in browser session, supplied via `config.json`.
- Random **3–8 second** delays between videos to avoid rate limiting.
- Retry with exponential backoff on errors (up to 3 attempts per video).
- Prints progress for every video.
- Output CSV columns: `video_url, username, comment, likes, date`.

## Install

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Windows cmd:        .venv\Scripts\activate.bat
# macOS/Linux:        source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

The last line downloads a managed Chromium build (~150 MB). It only has to
run once.

## Configure

1. Open `https://www.tiktok.com` in a browser, log in.
2. Open DevTools → Application → Cookies → `https://www.tiktok.com`.
3. Copy two cookie values:
   - `sessionid` (or `sid_guard` — see below if you only see this one).
   - `msToken` (use the one whose **Domain** column shows `.tiktok.com`).
4. Copy `config.example.json` to `config.json` and paste the values:

```json
{
  "sessionid": "YOUR_SESSIONID",
  "msToken": "YOUR_MSTOKEN",
  "headless": true,
  "proxy": null
}
```

`proxy` is optional. Set `headless` to `false` to watch the browser if you
need to debug.

### If you only see `sid_guard`, not `sessionid`

Some browsers / extensions only expose the `sid_guard` cookie. Its value
looks like:

```
3406cf0ff78ba0b0a805099101cc074f%7C1776664361%7C15552000%7C...
```

You can either:

- Paste the **whole** value as the `sid_guard` field in `config.json` — the
  script will automatically strip the trailing metadata; or
- Manually take only the part **before the first `%7C`** and paste it as
  `sessionid`.

Both forms work.

## Run

```bash
python tiktok_comments_scraper.py urls.txt
```

By default the script writes `comments_output.csv` in the current directory.
Use `--output some_path.csv` to override and `--verbose` for debug logs.

## Troubleshooting

- **`Wrote 0 comments`** — almost always means `msToken` is missing/expired
  or `sessionid` is invalid. Refresh both cookies and try again.
- **`playwright not installed` / browser missing** — run
  `playwright install chromium` again from the activated virtualenv.
- **`InvalidResponseException` / scraping fails on every video** — TikTok
  may have updated their internals. Update the library:
  `pip install --upgrade TikTokApi`.
- **Slow** — random 3–8s delays between videos are intentional to avoid
  rate limits.

## Notes / Limits

- Replies to comments are not expanded by default; only top-level comments
  are exported.
- Respect TikTok's Terms of Service and any applicable laws when scraping.
