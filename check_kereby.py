"""
Overvåger https://kereby.dk/bolig/ og sender en email, når der dukker en
ny bolig op på listen (ikke ved statusskift på eksisterende boliger).

Første kørsel gemmer blot en "baseline" over de boliger der findes lige nu
og sender ingen mail. Alle efterfølgende kørsler sammenligner med sidste
kendte tilstand og mailer kun om reelt nye annoncer.
"""

import asyncio
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://kereby.dk/bolig/"
STATE_FILE = Path("seen_listings.json")

# Matcher links af typen https://kereby.dk/bolig/<slug>/ men IKKE selve
# forsiden https://kereby.dk/bolig/
LISTING_HREF_RE = re.compile(r"^https://kereby\.dk/bolig/[^/?#]+/?$")


async def fetch_listings() -> dict[str, str]:
    """Henter siden med en rigtig browser og returnerer {url: kort beskrivelse}."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        # Giv eventuelt JS-drevet indhold lidt ekstra tid til at rendere
        await page.wait_for_timeout(3000)

        anchors = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(el => ({href: el.href, text: el.innerText.trim()}))",
        )
        await browser.close()

    listings: dict[str, str] = {}
    for a in anchors:
        href = a["href"].split("?")[0].rstrip("/") + "/"
        if LISTING_HREF_RE.match(href) and href != URL:
            text = (a["text"] or href).replace("\n", " ").strip()
            listings[href] = text[:300]
    return listings


def load_state() -> dict[str, str] | None:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(listings: dict[str, str]) -> None:
    STATE_FILE.write_text(
        json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_email(subject: str, body: str) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    email_to = os.environ.get("EMAIL_TO", gmail_user)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = email_to

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [email_to], msg.as_string())


async def main() -> None:
    current = await fetch_listings()

    if not current:
        print(
            "Fandt ingen boliglinks på siden. Kereby kan have ændret "
            "struktur på hjemmesiden — scriptet bør tjekkes/opdateres."
        )
        return

    previous = load_state()

    if previous is None:
        # Første kørsel: gem baseline, send ingen mail om eksisterende boliger
        save_state(current)
        print(f"Baseline gemt med {len(current)} boliger. Ingen mail sendt.")
        return

    new_urls = [url for url in current if url not in previous]

    if new_urls:
        blocks = [f"- {current[url]}\n  {url}" for url in new_urls]
        body = "Nye boliger fundet på Kereby:\n\n" + "\n\n".join(blocks)
        subject = f"Ny bolig på Kereby ({len(new_urls)} stk.)"
        send_email(subject, body)
        print(f"Sendte mail om {len(new_urls)} ny(e) bolig(er).")
    else:
        print("Ingen nye boliger siden sidste tjek.")

    save_state(current)


if __name__ == "__main__":
    asyncio.run(main())
