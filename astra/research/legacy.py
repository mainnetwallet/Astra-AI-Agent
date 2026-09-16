"""Optional web research helper for Astra.

Fetches a URL (project page / X / GitHub) and pulls the page <title> and meta
description so the user gets a quick "what is this project" summary. Uses only
stdlib urllib, no API keys. Offline-graceful: reports honestly when it can't
reach the network.
"""
from __future__ import annotations

import html
import re
import urllib.request

TIMEOUT = 8
UA = "Mozilla/5.0 (X11; Linux x86_64) AstraAI/1.0 (+research helper)"


class ResearchReply:
    def __init__(self, text: str, data: dict | None = None):
        self.text = text
        self.data = data or {}


def _fetch(url: str) -> tuple[str, str]:
    """Fetch a URL and strip it to (title, meta_description).

    SSRF-guarded: only http/https to public addresses; loopback, link-local
    and private ranges are refused unless the operator explicitly opens them
    with ASTRA_ALLOW_PRIVATE_URLS=1.
    """
    import os

    import astra.security as sec
    if not sec.allow_url(url,
                         allow_private=os.environ.get("ASTRA_ALLOW_PRIVATE_URLS") == "1"):
        raise ValueError("refused URL (private/blocked network)")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read(200_000)
        if resp.status >= 400:
            return "", ""
        try:
            page = raw.decode("utf-8", errors="replace")
        except Exception:
            page = raw.decode("latin-1", errors="replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                     page, re.I | re.S)
    if not desc:
        desc = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description',
                         page, re.I | re.S)
    t = html.unescape(title.group(1)).strip() if title else ""
    d = html.unescape(desc.group(1)).strip() if desc else ""
    return re.sub(r"\s+", " ", t)[:200], re.sub(r"\s+", " ", d)[:400]


def extract_url(text: str) -> str | None:
    m = re.search(r"(https?://\S+)", text)
    if m:
        return m.group(1).rstrip(".,;)")
    m = re.search(r"\b(\w+\.(com|io|xyz|net|org|app|finance)[\w./-]*)", text, re.I)
    if m:
        return "https://" + m.group(1).rstrip(".,;)")
    return None


def quick_lookup(message: str) -> ResearchReply:
    url = extract_url(message)
    if not url:
        return ResearchReply(
            "Research er jonno kono link/URL pailam na message e. Halka ekta "
            "project name thakle official site e verify korte parben — link dio "
            "amio page pushbo (request: 'report <link>').")
    try:
        title, desc = _fetch(url)
        if not title:
            return ResearchReply(
                f"🚫 Page fetch korte parlam na: {url}\n\nPossible: site block "
                f"korche, bot-detect, na offline. Internet check korun.")
        lines = [f"🔎 Research: {url}", "", f"**{title}**"]
        if desc:
            lines.append(desc)
        lines.append("")
        lines.append("⚠️ Safety: verify korte shobar theke bhalo — official "
                     "Telegram (username check ✓ bot), official X (blue tick + "
                     "followers), audit report. Kono wallet signature kokhono "
                     "diben na. 'Free token' er lalach e dhonkhay porben na.")
        return ResearchReply("\n".join(lines),
                             {"url": url, "title": title, "desc": desc})
    except Exception as e:
        return ResearchReply(f"🚫 {url} fetch e problem: {e.__class__.__name__}. "
                             f"Internet connection check korun.")