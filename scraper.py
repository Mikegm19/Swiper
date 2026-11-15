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
MAX_PAGES = 20          # Total pages to crawl per run
MAX_WORKERS = 5         # Concurrent threads for fast HTTP crawling
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(
    r"(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}"
)

SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".zip", ".tar", ".gz", ".css", ".js", ".xml", ".json",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

PRIORITY_KEYWORDS = (
    "contact", "team", "about", "staff", "people", "directory",
    "number", "numbers", "phone", "sms", "receive", "country", "free"
)

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
    # Allow both root domain and www subdomain
    if parsed.netloc.lower().lstrip("www.") != allowed_domain.lower().lstrip("www."):
        return None
    if parsed.path.lower().endswith(SKIP_EXTENSIONS):
        return None
    return clean.rstrip("/")


def looks_like_js_or_blocked(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").lower() if soup.title else ""
    if any(w in title for w in ("just a moment", "attention required", "access denied", "cloudflare")):
        return True
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)
    return len(visible_text) < 250


def fetch_fast(url: str) -> tuple[str, str | None, bool]:
    time.sleep(random.uniform(0.25, 0.8))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        content_type = resp.headers.get("Content-Type", "").lower()
        print(f"[HTTP] {resp.status_code} {url}")
        if resp.status_code in (403, 429, 503):
            return (url, None, True)
        if resp.status_code != 200 or "text/html" not in content_type:
            return (url, None, False)
        html = resp.text
        if looks_like_js_or_blocked(html):
            return (url, html, True)
        return (url, html, False)
    except requests.RequestException as err:
        print(f"[HTTP ERR] {url}: {err}")
        return (url, None, True)


def extract_phone_candidates(text: str) -> list[str]:
    found = []
    for match in PHONE_REGEX.findall(text):
        cleaned = match.strip()
        digits = re.sub(r"\D", "", cleaned)
        # Filter out years/dates (e.g., 2024-2026) and keep realistic phone lengths
        if 8 <= len(digits) <= 15 and not re.fullmatch(r"(19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}", cleaned):
            found.append(cleaned)
    return found


def parse_page(html: str, source_url: str, allowed_domain: str) -> tuple[list[dict], list[str]]:
    soup = BeautifulSoup(html, "lxml")

    title = (soup.title.string or "").strip() if soup.title else "No Title"
    if any(w in title.lower() for w in ("just a moment", "attention required", "cloudflare")):
        print(f"[BLOCKED] Cloudflare/WAF challenge detected on {source_url} (Title: {title})")
        return [], []

    # 1. Discover internal links
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

    # 2. Container-scoped extraction (includes div cards & anchor blocks)
    contacts = []
    seen = set()

    containers = soup.select(
        ".contact, .profile, .card, .member, .person, .number, .phone, article, li, tr, section, footer"
    )
    for block in containers:
        text = block.get_text(" ", strip=True)
        emails = EMAIL_REGEX.findall(text)
        phones = extract_phone_candidates(text)

        for a in block.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                emails.append(href[7:].split("?")[0].strip())
            elif href.lower().startswith("tel:"):
                raw_tel = href[4:].strip()
                if 8 <= len(re.sub(r"\D", "", raw_tel)) <= 15:
                    phones.append(raw_tel)

        if not emails and not phones:
            continue

        name_el = block.select_one("h1, h2, h3, h4, .name, .full-name, .country, strong, td")
        name = name_el.get_text(" ", strip=True) if name_el else "Unknown"
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""

        key = (name.lower(), email.lower(), phone)
        if key in seen:
            continue
        seen.add(key)

        contacts.append(
            {"name": name[:100], "email": email, "phone": phone, "source": source_url}
        )

    # 3. Page-wide fallback if container selectors missed everything (e.g., plain div grids)
    if not contacts:
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        full_text = soup.get_text(" ", strip=True)
        fallback_emails = list(dict.fromkeys(EMAIL_REGEX.findall(full_text)))
        fallback_phones = list(dict.fromkeys(extract_phone_candidates(full_text)))

        for phone in fallback_phones[:50]:
            contacts.append({"name": title[:80], "email": "", "phone": phone, "source": source_url})
        for email in fallback_emails[:50]:
            contacts.append({"name": title[:80], "email": email, "phone": "", "source": source_url})

    return contacts, priority_links + regular_links


def spider_with_playwright(seed_urls: list[str], allowed_domain: str, already_visited: set, max_pw_pages: int = 10) -> tuple[list[dict], int]:
    """Playwright spider fallback that ALSO follows discovered links."""
    contacts = []
    crawled = 0
    queue = deque(seed_urls)
    pw_visited = set()
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

        while queue and crawled < max_pw_pages:
            url = queue.popleft()
            if url in pw_visited:
                continue
            pw_visited.add(url)
            crawled += 1

            try:
                time.sleep(random.uniform(1.0, 2.2))
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except PlaywrightTimeout:
                    pass
                page.mouse.wheel(0, random.randint(500, 900))
                time.sleep(random.uniform(0.6, 1.2))

                html = page.content()
                found_contacts, links = parse_page(html, url, allowed_domain)
                contacts.extend(found_contacts)
                print(f"[PW] {url} -> {len(found_contacts)} contacts, {len(links)} links")

                for link in links:
                    if link not in pw_visited and link not in already_visited and link not in queue:
                        if any(k in link.lower() for k in PRIORITY_KEYWORDS):
                            queue.appendleft(link)
                        else:
                            queue.append(link)
            except Exception as err:
                print(f"[PW WARN] Failed on {url}: {err}")

        browser.close()

    return contacts, crawled


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
                            if any(k in link.lower() for k in PRIORITY_KEYWORDS):
                                queue.appendleft(link)
                            else:
                                queue.append(link)
                if needs_js and url not in js_fallback_urls:
                    js_fallback_urls.append(url)

    total_crawled = len(visited)

    # Stage 2: Playwright spider fallback (also follows links if Stage 1 got stuck)
    if js_fallback_urls:
        print(f"[INFO] Running Playwright spider fallback starting from {len(js_fallback_urls)} URL(s)...")
        pw_contacts, pw_crawled = spider_with_playwright(
            js_fallback_urls[:5], allowed_domain, visited, max_pw_pages=10
        )
        add_contacts(pw_contacts)
        total_crawled = max(total_crawled, pw_crawled)

    return all_contacts, total_crawled


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
