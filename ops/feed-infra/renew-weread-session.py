#!/usr/bin/env python3
"""Refresh the WeRead session cookie before it expires.

we-mp-rss stores one cookie from the setup QR scan and never renews it. The
``wr_skey`` inside expires after a few days; when it does every account fails
with ``mp cover returned HTTP 499`` and the WeChat sources go quiet without a
single source reporting an error - the feeds keep serving the articles they
already hold, so ``probe-sources`` still says ``ok``.

That is exactly what happened on 2026-09-01: the credential was written at
setup on 08-27 12:22, expired around 05:00 on 09-01, and twelve accounts had
been failing for seven hours before anyone looked. The daily lost the
benchmark digest's lead story that morning because DeepSeek announces on
WeChat and nowhere else we watch.

WeRead's own web client refreshes the session with ``POST
/web/login/renewal``, which takes the ``wr_rt`` in the cookie and hands back a
fresh ``wr_skey``. Running that on a timer keeps the credential alive with no
new QR scan. A scan is only needed once ``wr_rt`` itself dies, and this exits
non-zero in that case so the failure is visible in the journal rather than in
a week of missing Chinese first-party news.

Exit codes: 0 renewed, 1 renewal refused (QR re-scan required), 2 usage error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

LIC_PATH = Path(
    os.environ.get("WEREAD_LIC_PATH", "/www/wwwroot/ai-daily/feed-infra/data/we-mp-rss/wx.lic")
)
RENEWAL_URL = "https://weread.qq.com/web/login/renewal"
COVER_URL = "https://weread.qq.com/api/mp/cover"
#: Any bookId works as a liveness probe; this is the benchmark digest's.
PROBE_BOOK_ID = "MP_WXS_3220940658"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://weread.qq.com",
    "Referer": "https://weread.qq.com/",
}


def _load() -> tuple[dict, dict]:
    document = json.loads(LIC_PATH.read_text(encoding="utf-8"))
    stored = document.get("weread_data", {})
    if isinstance(stored, str):
        stored = json.loads(stored)
    return document, stored


def main() -> int:
    if not LIC_PATH.exists():
        print(f"credential file missing: {LIC_PATH}", file=sys.stderr)
        return 2

    document, stored = _load()
    cookie = stored.get("cookie") or ""
    if "wr_rt=" not in cookie:
        print("stored cookie carries no wr_rt; a QR re-scan is required", file=sys.stderr)
        return 1

    session = requests.Session()
    for pair in cookie.split(";"):
        if "=" in pair:
            key, value = pair.strip().split("=", 1)
            session.cookies.set(key, value, domain=".weread.qq.com")

    try:
        reply = session.post(
            RENEWAL_URL,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"rq": "%2Fweb%2Fbook%2Fread"},
            timeout=(10, 30),
        )
    except requests.RequestException as error:
        print(f"renewal request failed: {error}", file=sys.stderr)
        return 1

    if reply.status_code != 200:
        print(f"renewal refused: HTTP {reply.status_code}", file=sys.stderr)
        return 1
    try:
        succeeded = reply.json().get("succ") == 1
    except ValueError:
        succeeded = False
    if not succeeded:
        print(f"renewal refused: {reply.text[:120]}", file=sys.stderr)
        return 1

    # Keep every key WeRead handed back, not only the ones we arrived with:
    # renewal rotates wr_rt and adds wr_pf, and dropping either costs us the
    # next renewal.
    renewed = "; ".join(f"{key}={value}" for key, value in session.cookies.get_dict().items())
    if "wr_skey=" not in renewed:
        print("renewal returned no wr_skey; a QR re-scan is required", file=sys.stderr)
        return 1

    stored["cookie"] = renewed
    document["weread_data"] = stored
    scratch = LIC_PATH.with_name(LIC_PATH.name + ".tmp")
    scratch.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.replace(scratch, LIC_PATH)

    # Report whether the renewed session can actually read, so a rate limit or
    # a dead account shows up here rather than as silence in the digest.
    try:
        probe = session.get(
            COVER_URL,
            params={"bookId": PROBE_BOOK_ID},
            headers=HEADERS,
            timeout=(10, 30),
        )
    except requests.RequestException as error:
        print(f"renewed, but the probe failed: {error}")
        return 0

    detail = ""
    if probe.status_code != 200:
        try:
            detail = f" {probe.json().get('data', {})}"
        except ValueError:
            detail = ""
    print(f"renewed; cover probe HTTP {probe.status_code}{detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
