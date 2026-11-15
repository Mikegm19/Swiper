import json
import re
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# Add your target test URLs here
TARGET_URLS = [
    "https://your-username.github.io/your-test-site/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
}

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}"
)

OUTPUT_FILE = Path("data.json")


def fetch_html(url: str) -> str | None:
    """Fetch raw HTML. Swap this function for Playwright on heavy JS/social sites."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        return response.text
    except requests.RequestException as err:
        print(f"[ERROR] Failed to fetch {url}: {err}")
        return None


def extract_contacts(html: str, source_url: str) -> list[dict]:
    """
    Extracts name, email, and phone by scanning container blocks first
    (so contact info stays linked to the right person), falling back to page-wide regex.
    """
    soup = BeautifulSoup(html, "html.parser")
    contacts = []

    # Target common profile/card containers; adjust selectors for specific sites
    containers = soup.select(
        ".contact, .profile, .card, .member, .person, article, li, tr"
    )

    seen_keys = set()

    for block in containers:
        text = block.get_text(" ", strip=True)
        emails = EMAIL_REGEX.findall(text)
        phones = [p.strip() for p in PHONE_REGEX.findall(text) if len(re.sub(r"\D", "", p)) >= 7]

        if not emails and not phones:
            continue

        # Attempt to pull the personal name from heading/name tags inside the block
        name_el = block.select_one("h1, h2, h3, h4, .name, .full-name, strong, td")
        name = name_el.get_text(" ", strip=True) if name_el else "Unknown"

        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""

        dedupe_key = (name.lower(), email.lower(), phone)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        contacts.append(
            {
                "name": name,
                "email": email,
                "phone": phone,
                "source": source_url,
            }
        )

    return contacts


def main() -> None:
    all_contacts = []
    global_seen = set()

    for url in TARGET_URLS:
        html = fetch_html(url)
        if not html:
            continue
        for record in extract_contacts(html, url):
            key = (record["name"].lower(), record["email"].lower(), record["phone"])
            if key not in global_seen:
                global_seen.add(key)
                all_contacts.append(record)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_records": len(all_contacts),
        "contacts": all_contacts,
    }

    OUTPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[INFO] Saved {len(all_contacts)} contacts to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
