import json
import os
import random
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# --- Spider & Concurrency Tuning ---
MAX_PAGES = 25          # Safety cap per run (keeps GitHub Actions fast & polite)
MAX_WORKERS = 5         # Concurrent threads for fast HTTP crawling
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}"
)

# Skip non-HTML files when spidering
SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".zip", ".tar", ".gz", ".css", ".js", ".xml", ".json",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

# High-value paths are crawled first
PRIORITY_KEYWORDS = ("contact", "team", "about", "staff", "people", "directory", "leadership", "support")

# Analytics & tracker domains blocked when Playwright runs
BLOCKED_DOMAINS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "connect.facebook.net", "hotjar.com", "segment.io",
    "mixpanel.com", "clarity.ms", "ads-twitter.com", "tiktok.com",
)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

OUTPUT_FILE = Path("data.json")
TARGETS_FILE = Path("targets.json")


def get_seed_urls() -> list[str]:
    env_url = os.getenv("TARGET_URL", "").strip()
    if env_url:
        return [env_url]
    if TARGETS_FILE.exists():
        return json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    return []


def get_random_proxy() -> dict | None:
    raw_list = os.getenv("PROXY_LIST", "").strip()
    if not raw_list:
        return None
    proxies = [p.strip() for p in raw_list.split(",") if p.strip()]
    if not proxies:
        return None
    parsed = urlparse(random.choice(proxies))
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def normalize_link(base_url: str, href: str, allowed_domain: str) -> str | None:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    joined = urljoin(base_url, href.strip())
    clean, _ = urldefrag(joined)
    parsed = urlparse(clean)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() != allowed_domain.lower():
        return None
    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
        return None
    return clean.rstrip("/")


def looks_like_js_spa(html: str) -> bool:
    """Detects empty React/Vue/Next shells that require Playwright rendering."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)
    if len(visible_text) < 250:
        return True
    lower = html.lower()
    spa_markers = ('id="__next"', 'id="root"', 'id="app"', "enable javascript")
    return any(m in lower for m in spa_markers) and len(visible_text) < 600


def fetch_fast(url: str) -> tuple[str, str | None, bool]:
    """
    Fast thread-safe HTTP fetch.
    Returns (url, html_or_none, needs_playwright_fallback).
    """
    time.sleep(random.uniform(0.25, 0.9))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        content_type = resp.headers.get("Content-Type", "").lower()
        if resp.status_code in (403, 429, 503):
            return (url, None, True)
        if resp.status_code != 200 or "text/html" not in content_type:
            return (url, None, False)
        html = resp.text
        if looks_like_js_spa(html):
            return (url, html, True)
        return (url, html, False)
    except requests.RequestException:
        return (url, None, True)


def fetch_batch_playwright(urls: list[str]) -> dict[str, str]:
    """
    Renders JS-only fallback URLs using ONE shared Chromium instance,
    blocking images, CSS, fonts, media, and analytics scripts.
    """
    results: dict[str, str] = {}
    if not urls:
        return results

    proxy_config = get_random_proxy()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, proxy=proxy_config)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        def block_heavy_assets(route):
            req = route.request
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                return route.abort()
            if any(domain in req.url for domain in BLOCKED_DOMAINS):
                return route.abort()
            return route.continue_()

        page.route("**/*", block_heavy_assets)

        for url in urls:
            try:
                time.sleep(random.uniform(1.2, 2.8))
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=7000)
                except PlaywrightTimeout:
                    pass
                for _ in range(2):
                    page.mouse.wheel(0, random.randint(500, 900))
                    time.sleep(random.uniform(0.8, 1.5))
                results[url] = page.content()
            except Exception as err:
                print(f"[WARN] Playwright failed on {url}: {err}")

        browser.close()

    return results


def parse_page(html: str, source_url: str, allowed_domain: str) -> tuple[list[dict], list[str]]:
    """Extracts both contacts and internal spider links using fast lxml parsing."""
    soup = BeautifulSoup(html, "lxml")

    # 1. Discover internal links for the spider
    priority_links = []
    regular_links = []
    for a in soup.find_all("a", href=True):
        link = normalize_link(source_url, a["href"], allowed_domain)
        if not link:
            continue
        if any(k in link.lower() for k in PRIORITY_KEYWORDS):
            priority_links.append(link)
        else:
            regular_links.append(link)

    # 2. Extract contacts scoped to DOM containers
    contacts = []
    seen = set()

    containers = soup.select(
        ".contact, .profile, .card, .member, .person, article, li, tr, section, footer"
    )
    for block in containers:
        text = block.get_text(" ", strip=True)
        emails = EMAIL_REGEX.findall(text)

        # Also catch mailto: and tel: hrefs hidden behind icons/buttons
        for a in block.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                emails.append(href[7:].split("?")[0].strip())

        phones = [
            p.strip()
            for p in PHONE_REGEX.findall(text)
            if 7 <= len(re.sub(r"\D", "", p)) <= 15
        ]
        for a in block.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("tel:"):
                raw_tel = href[4:].strip()
                if 7 <= len(re.sub(r"\D", "", raw_tel)) <= 15:
                    phones.append(raw_tel)

        if not emails and not phones:
            continue

        name_el = block.select_one("h1, h2, h3, h4, .name, .full-name, strong, td")
        name = name_el.get_text(" ", strip=True) if name_el else "Unknown"
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""

        key = (name.lower(), email.lower(), phone)
        if key in seen:
            continue
        seen.add(key)

        contacts.append(
            {
                "name": name[:100],
                "email": email,
                "phone": phone,
                "source": source_url,
            }
        )

    return contacts, priority_links + regular_links


def spider_site(seed_url: str) -> tuple[list[dict], int]:
    allowed_domain = urlparse(seed_url).netloc
    queue = deque([seed_url.rstrip("/")])
    visited = set()
    js_fallback_urls = []
    all_contacts = []
    global_seen = set()

    def add_contacts(records: list[dict]):
        for rec in records:
            key = (rec["name"].lower(), rec["email"].lower(), rec["phone"])
            if key not in global_seen:
                global_seen.add(key)
                all_contacts.append(rec)

    # Stage 1: Fast concurrent HTTP spidering
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while queue and len(visited) < MAX_PAGES:
            batch = []
            while queue and len(batch) < MAX_WORKERS and (len(visited) + len(batch)) < MAX_PAGES:
                nxt = queue.popleft()
                if nxt not in visited:
                    visited.add(nxt)
                    batch.append(nxt)

            if not batch:
                break

            futures = [executor.submit(fetch_fast, u) for u in batch]
            for fut in as_completed(futures):
                url, html, needs_js = fut.result()
                if html:
                    contacts, links = parse_page(html, url, allowed_domain)
                    add_contacts(contacts)
                    for link in links:
                        if link not in visited and link not in queue:
                            # Push priority links to the front of the queue
                            if any(k in link.lower() for k in PRIORITY_KEYWORDS):
                                queue.appendleft(link)
                            else:
                                queue.append(link)
                if needs_js and len(js_fallback_urls) < 10:
                    js_fallback_urls.append(url)

    # Stage 2: Stripped-down Playwright pass only for JS-heavy pages
    if js_fallback_urls:
        print(f"[INFO] Running fast Playwright fallback on {len(js_fallback_urls)} JS pages...")
        rendered = fetch_batch_playwright(js_fallback_urls)
        for url, html in rendered.items():
            contacts, _ = parse_page(html, url, allowed_domain)
            add_contacts(contacts)

    return all_contacts, len(visited)


def main() -> None:
    seeds = get_seed_urls()
    combined_contacts = []
    global_seen = set()
    total_pages_crawled = 0

    for seed in seeds:
        contacts, crawled = spider_site(seed)
        total_pages_crawled += crawled
        for rec in contacts:
            key = (rec["name"].lower(), rec["email"].lower(), rec["phone"])
            if key not in global_seen:
                global_seen.add(key)
                combined_contacts.append(rec)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "scraped_urls": seeds,
        "pages_crawled": total_pages_crawled,
        "total_records": len(combined_contacts),
        "contacts": combined_contacts,
    }

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[INFO] Crawled {total_pages_crawled} pages, saved {len(combined_contacts)} contacts.")


if __name__ == "__main__":
    main()
