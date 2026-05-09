# TikTok Comments Scraper

A small Python script that reads a list of TikTok video URLs and writes every
comment (username, text, likes, timestamp) to a CSV file.

## Features

- Input: text file with up to **80** TikTok video URLs, one per line.
- Authenticates with TikTok via the `sessionid` cookie supplied in
  `config.json` (copy it from a logged-in browser session).
- Random **3–8 second** delays between paginated requests to avoid rate
  limiting.
- Retry with exponential backoff on HTTP errors / empty bodies (up to 4
  attempts per request).
- Prints progress for every video.
- Output CSV columns: `video_url, username, comment, likes, date`.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

1. Open `https://www.tiktok.com` in a browser, log in.
2. Open DevTools → Application → Cookies → copy the `sessionid` value.
3. Copy `config.example.json` to `config.json` and paste the value:

```json
{
  "sessionid": "YOUR_SESSIONID",
  "msToken": null,
  "user_agent": "Mozilla/5.0 ...",
  "proxy": null
}
```

`msToken` and `proxy` are optional. If you hit a lot of rate limits, set
`proxy` to something like `http://user:pass@host:port`.

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

## Notes / Limits

- Comments come from TikTok's public web API (`/api/comment/list/`). TikTok
  may change the endpoint or shape at any time; if scraping stops working,
  inspect the network tab on tiktok.com and update the request shape.
- Replies to comments (`reply_comment_total`) are **not** expanded by default —
  only top-level comments are exported. Adding a second pagination call for
  replies is straightforward but was kept out to keep behavior simple.
- Respect TikTok's Terms of Service and any applicable laws when scraping.
