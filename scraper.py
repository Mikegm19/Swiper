import csv
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

MAX_PAGES = 20
MAX_WORKERS = 5
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
# Matches numbers like +234 801 234 5678, (555) 123-4567, 08012345678, or 123-4567
PHONE_REGEX = re.compile(r"\+?\d[\d\s().-]{6,18}\d")

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

OUTPUT_JSON = Path("data.json")
OUTPUT_CSV = Path("contacts_export.csv")
TARGETS_CSV = Path("targets.csv")


def get_seed_urls() -> list[str]:
    env_url = os.getenv("TARGET_URL", "").strip()
    if env_url:
        return [env_url]

    seeds = []
    seen = set()
    if TARGETS_CSV.exists():
        print("[INFO] Reading bulk URLs from targets.csv...")
        with open(TARGETS_CSV, mode="r", encoding="utf-8", errors="ignore") as f:
            for row in csv.reader(f):
                for cell in row:
                    cell = cell.strip()
                    if cell.startswith(("http://", "https://")) and cell not in seen:
                        seen.add(cell)
                        seeds.append(cell)
    return seeds


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
    return len(soup.get_text(" ", strip=True)) < 250


def fetch_fast(url: str) -> tuple[str, str | None, bool]:
    time.sleep(random.uniform(0.2, 0.6))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        content_type = resp.headers.get("Content-Type", "").lower()
        if resp.status_code in (403, 429, 503):
            return (url, None, True)
        if resp.status_code != 200 or "text/html" not in content_type:
            return (url, None, False)
        html = resp.text
        if looks_like_js_or_blocked(html):
            return (url, html, True)
        return (url, html, False)
    except requests.RequestException:
        return (url, None, True)


def extract_phones_from_text(text: str) -> list[str]:
    phones = []
    seen = set()
    for match in PHONE_REGEX.findall(text):
        cleaned = re.sub(r"\s+", " ", match).strip()
        digits = re.sub(r"\D", "", cleaned)
        if not (7 <= len(digits) <= 15):
            continue
        # Skip dates like 2026-11-21 or 11/21/2026
        if re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", cleaned):
            continue
        # Skip all-identical digits
        if len(set(digits)) <= 1:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            phones.append(cleaned)
    return phones


def parse_page(html: str, source_url: str, allowed_domain: str) -> tuple[list[dict], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title else "No Title"

    if any(w in title.lower() for w in ("just a moment", "attention required", "cloudflare")):
        print(f"[BLOCKED] Cloudflare/WAF challenge on {source_url}")
        return [], []

    # Strip code/hidden tags so only visible page content is read
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()

    links = []
    seen_links = set()
    for a in soup.find_all("a", href=True):
        link = normalize_link(source_url, a["href"], allowed_domain)
        if link and link not in seen_links:
            seen_links.add(link)
            if any(k in link.lower() for k in PRIORITY_KEYWORDS):
                links.insert(0, link)
            else:
                links.append(link)

    contacts = []
    seen_contacts = set()

    def add_record(name: str, email: str, phone: str):
        key = (name.strip().lower(), email.strip().lower(), phone.strip())
        if key not in seen_contacts and (email or phone):
            seen_contacts.add(key)
            contacts.append({
                "name": (name.strip() or title)[:100],
                "email": email.strip(),
                "phone": phone.strip(),
                "source": source_url,
            })

    # 1. Direct tel: and mailto: links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        label = a.get_text(" ", strip=True) or title
        if href.lower().startswith("tel:"):
            tel_num = re.sub(r"\s+", " ", href[4:].strip())
            if 7 <= len(re.sub(r"\D", "", tel_num)) <= 15:
                add_record(label, "", tel_num)
        elif href.lower().startswith("mailto:"):
            mail = href[7:].split("?")[0].strip()
            if mail:
                add_record(label, mail, "")

    # 2. Visible text across the page
    visible_text = soup.get_text(" ", strip=True)
    for phone in extract_phones_from_text(visible_text):
        add_record(title, "", phone)

    for email in dict.fromkeys(EMAIL_REGEX.findall(visible_text)):
        add_record(title, email, "")

    return contacts, links


def spider_with_playwright(seed_urls: list[str], allowed_domain: str, already_visited: set, max_pw_pages: int = 10) -> tuple[list[dict], int]:
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
            if req.resource_type in BLOCKED_RESOURCE_TYPES or any(d in req.url for d in BLOCKED_DOMAINS):
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
                time.sleep(random.uniform(0.8, 1.8))
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except PlaywrightTimeout:
                    pass
                page.mouse.wheel(0, random.randint(400, 900))
                time.sleep(random.uniform(0.5, 1.0))

                html = page.content()
                found_contacts, links = parse_page(html, url, allowed_domain)
                contacts.extend(found_contacts)
                print(f"[PW] {url} -> {len(found_contacts)} items, {len(links)} links")

                for link in links:
                    if link not in pw_visited and link not in already_visited and link not in queue:
                        queue.append(link)
            except Exception as err:
                print(f"[PW WARN] Failed on {url}: {err}")

        browser.close()

    return contacts, crawled


def spider_site(seed_url: str) -> tuple[list[dict], int]:
    print(f"\n[INFO] Crawling: {seed_url}")
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
                    print(f"[HTTP] {url} -> {len(contacts)} items, {len(links)} links")
                    for link in links:
                        if link not in visited and link not in queue:
                            queue.append(link)
                if needs_js and url not in js_fallback_urls:
                    js_fallback_urls.append(url)

    total_crawled = len(visited)

    if js_fallback_urls:
        print(f"[INFO] Running Playwright fallback on {len(js_fallback_urls)} URL(s)...")
        pw_contacts, pw_crawled = spider_with_playwright(
            js_fallback_urls[:5], allowed_domain, visited, max_pw_pages=10
        )
        add_contacts(pw_contacts)
        total_crawled = max(total_crawled, pw_crawled)

    return all_contacts, total_crawled


def main() -> None:
    seeds = get_seed_urls()
    if not seeds:
        print("[INFO] No target URLs found via Vercel or targets.csv.")
        return

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
    OUTPUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with open(OUTPUT_CSV, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Name", "Email", "Phone", "Source"])
        for c in combined_contacts:
            writer.writerow([c["name"], c["email"], c["phone"], c["source"]])

    print(f"\n[SUCCESS] Crawled {total_pages_crawled} pages across {len(seeds)} site(s).")
    print(f"[SUCCESS] Saved {len(combined_contacts)} unique contacts to data.json and contacts_export.csv.")


if __name__ == "__main__":
    main()
