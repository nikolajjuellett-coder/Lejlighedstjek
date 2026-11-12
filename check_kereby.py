"""
Overvåger https://kereby.dk/bolig/ og sender en email, når der dukker en
ny bolig op på listen (ikke ved statusskift på eksisterende boliger).

Første kørsel gemmer blot en "baseline" over de boliger der findes lige nu
og sender ingen mail. Alle efterfølgende kørsler sammenligner med sidste
kendte tilstand og mailer kun om reelt nye annoncer.

Hvis siden ikke kan indlæses korrekt, afslutter scriptet med en fejl, så
kørslen bliver rød i GitHub Actions i stedet for at se grøn og "OK" ud.
"""

import asyncio
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

from playwright.async_api import async_playwright

URL = "https://kereby.dk/bolig/"
STATE_FILE = Path("seen_listings.json")

# Matcher links af typen https://kereby.dk/bolig/<slug>/ men IKKE selve
# forsiden https://kereby.dk/bolig/
LISTING_HREF_RE = re.compile(r"^https://kereby\.dk/bolig/[^/?#]+/?$")

# Siden skriver fx "15 boliger fundet" når listen er indlæst korrekt.
PAGE_LOADED_RE = re.compile(r"(\d+)\s+bolig(?:er)?\s+fundet", re.IGNORECASE)


async def fetch_page() -> tuple[dict[str, str], str | None]:
    """
    Henter siden med en rigtig browser.
    Returnerer ({url: kort beskrivelse}, tekst-om-antal-boliger eller None).
    Det andet felt er None, hvis siden ikke ser ud til at være indlæst korrekt.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(URL, wait_until="networkidle", timeout=60000)
            # Giv JS-drevet indhold lidt ekstra tid til at rendere
            await page.wait_for_timeout(3000)

            anchors = await page.eval_on_selector_all(
                "a[href]",
                "els => els.map(el => ({href: el.href, text: el.innerText.trim()}))",
            )
            body_text = await page.inner_text("body")
        finally:
            await browser.close()

    listings: dict[str, str] = {}
    for a in anchors:
        href = a["href"].split("?")[0].rstrip("/") + "/"
        if LISTING_HREF_RE.match(href) and href != URL:
            text = (a["text"] or href).replace("\n", " ").strip()
            listings[href] = text[:300]

    match = PAGE_LOADED_RE.search(body_text)
    return listings, (match.group(0) if match else None)


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
    current: dict[str, str] = {}
    page_text: str | None = None

    # Prøv op til to gange, så et enkelt hængende sidebesøg ikke vælter kørslen
    for attempt in (1, 2):
        try:
            current, page_text = await fetch_page()
        except Exception as exc:  # noqa: BLE001
            print(f"Forsøg {attempt} fejlede: {exc}")
            page_text = None
        if page_text is not None:
            break
        if attempt == 1:
            await asyncio.sleep(10)

    if page_text is None:
        print(
            "FEJL: Siden blev ikke indlæst korrekt (teksten om antal boliger "
            "mangler). Kereby kan have ændret siden eller blokere besøget."
        )
        sys.exit(1)

    print(f"Siden indlæst: '{page_text}'. Boliger med link fundet: {len(current)}.")

    if not current:
        # Reserverede og udlejede boliger har intet link på siden, så det kan
        # i sig selv være normalt. Vi rører ikke ved den gemte status.
        print("Ingen boliger med link lige nu. Ingen ændringer.")
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
