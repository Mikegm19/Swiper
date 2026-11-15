import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}"
)

OUTPUT_FILE = Path("data.json")
TARGETS_FILE = Path("targets.json")


def get_target_urls() -> list[str]:
    """Use the URL submitted from the frontend if present; otherwise fall back to targets.json."""
    env_url = os.getenv("TARGET_URL", "").strip()
    if env_url:
        return [env_url]
    if TARGETS_FILE.exists():
        return json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    return []


def fetch_html_playwright(url: str) -> str | None:
    """Renders JS-heavy pages with human-like timing and basic anti-detection."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )

        # Mask the webdriver flag
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()
        try:
            # Random pre-request jitter so requests aren't robotic
            time.sleep(random.uniform(2.0, 5.0))

            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Wait for JS/network to settle without crashing on streaming sites
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass

            # Scroll slowly to trigger lazy-loaded profiles/contacts
            for _ in range(3):
                page.mouse.wheel(0, random.randint(400, 900))
                time.sleep(random.uniform(1.5, 3.5))

            html = page.content()
            return html
        except Exception as err:
            print(f"[ERROR] Failed to scrape {url}: {err}")
            return None
        finally:
            browser.close()


def extract_contacts(html: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    contacts = []
    seen_keys = set()

    containers = soup.select(
        ".contact, .profile, .card, .member, .person, article, li, tr, section"
    )

    for block in containers:
        text = block.get_text(" ", strip=True)
        emails = EMAIL_REGEX.findall(text)
        phones = [
            p.strip()
            for p in PHONE_REGEX.findall(text)
            if 7 <= len(re.sub(r"\D", "", p)) <= 15
        ]

        if not emails and not phones:
            continue

        name_el = block.select_one("h1, h2, h3, h4, .name, .full-name, strong, td")
        name = name_el.get_text(" ", strip=True) if name_el else "Unknown"
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""

        key = (name.lower(), email.lower(), phone)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        contacts.append(
            {
                "name": name[:100],
                "email": email,
                "phone": phone,
                "source": source_url,
            }
        )

    return contacts


def main() -> None:
    urls = get_target_urls()
    all_contacts = []
    global_seen = set()

    for idx, url in enumerate(urls):
        if idx > 0:
            # Cooldown between multiple URLs to avoid rate-limit bans
            time.sleep(random.uniform(5.0, 12.0))

        html = fetch_html_playwright(url)
        if not html:
            continue

        for record in extract_contacts(html, url):
            key = (record["name"].lower(), record["email"].lower(), record["phone"])
            if key not in global_seen:
                global_seen.add(key)
                all_contacts.append(record)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "scraped_urls": urls,
        "total_records": len(all_contacts),
        "contacts": all_contacts,
    }

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[INFO] Saved {len(all_contacts)} contacts to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
