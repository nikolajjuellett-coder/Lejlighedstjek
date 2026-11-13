"""
Overvåger https://kereby.dk/bolig/ og sender en email, når der dukker en
ny bolig op på listen (ikke ved statusskift på eksisterende boliger).

Scriptet kører som en LØBENDE overvågning: det tjekker siden hvert 5. minut
mellem kl. 08 og 17 dansk tid, og bliver i op til MAX_RUN_MIN minutter.
Derefter slutter det, og GitHub-workflowet starter selv den næste kørsel.
Det er nødvendigt, fordi GitHubs skemalagte kørsler er "best effort" og i
praksis kun blev startet 1-2 gange i døgnet.

Status (hvilke boliger der er set) gemmes i seen_listings.json og skrives
tilbage til repoet, så en ny kørsel kan fortsætte, hvor den forrige slap.
Hvis siden ikke kan hentes 3 gange i træk, sender scriptet én fejlmail og
afslutter med fejl (rød kørsel).
"""

import asyncio
import json
import os
import re
import smtplib
import subprocess
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

URL = "https://kereby.dk/bolig/"
STATE_FILE = Path("seen_listings.json")

TZ = ZoneInfo("Europe/Copenhagen")
WINDOW_START_HOUR = 8   # der tjekkes fra og med kl. 08:00
WINDOW_END_HOUR = 17    # ... og til (ikke med) kl. 17:00

CHECK_INTERVAL_SEC = float(os.environ.get("CHECK_INTERVAL_SEC", "300"))
MAX_RUN_MIN = float(os.environ.get("MAX_RUN_MIN", "300"))
MAX_CONSECUTIVE_FAILURES = 3

# Matcher links af typen https://kereby.dk/bolig/<slug>/ men IKKE selve
# forsiden https://kereby.dk/bolig/
LISTING_HREF_RE = re.compile(r"^https://kereby\.dk/bolig/[^/?#]+/?$")

# Siden skriver fx "15 boliger fundet" når listen er indlæst korrekt.
PAGE_LOADED_RE = re.compile(r"(\d+)\s+bolig(?:er)?\s+fundet", re.IGNORECASE)


# --------------------------------------------------------------------------
# Hentning af siden
# --------------------------------------------------------------------------

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


async def fetch_with_retry() -> tuple[dict[str, str], str] | None:
    """Prøver op til to gange. Returnerer None, hvis siden ikke kan læses."""
    for attempt in (1, 2):
        try:
            current, page_text = await fetch_page()
            if page_text is not None:
                return current, page_text
            print(f"Forsøg {attempt}: siden ser ikke indlæst ud (teksten om antal boliger mangler).")
        except Exception as exc:  # noqa: BLE001
            print(f"Forsøg {attempt} fejlede: {exc}")
        if attempt == 1:
            await asyncio.sleep(10)
    return None


# --------------------------------------------------------------------------
# Status, mail og git
# --------------------------------------------------------------------------

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

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as server:
        server.starttls()
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [email_to], msg.as_string())


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def commit_state(message: str = "Opdater liste over sete boliger") -> None:
    """Skriver status tilbage til repoet. Gør kun noget i GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    _git("config", "user.name", "kereby-overvaagning")
    _git("config", "user.email", "actions@github.com")
    _git("add", str(STATE_FILE))
    if _git("diff", "--cached", "--quiet").returncode == 0:
        return  # ingen ændringer
    _git("commit", "-m", message)
    result = _git("push")
    if result.returncode != 0:
        print(f"Advarsel: kunne ikke gemme status i repoet: {result.stderr.strip()}")


def set_output(key: str, value: str) -> None:
    """Giver workflowet besked (bruges til at afgøre, om næste kørsel skal startes)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


# --------------------------------------------------------------------------
# Ét tjek og selve løkken
# --------------------------------------------------------------------------

def in_window(now: datetime) -> bool:
    return WINDOW_START_HOUR <= now.hour < WINDOW_END_HOUR


async def check_once(
    previous: dict[str, str] | None,
) -> tuple[bool, dict[str, str] | None]:
    """
    Ét tjek af siden. Returnerer (lykkedes, opdateret status).
    'Lykkedes' er False, hvis siden ikke kunne læses, eller mailen fejlede.
    """
    result = await fetch_with_retry()
    if result is None:
        print("FEJL: siden kunne ikke læses.")
        return False, previous

    current, page_text = result
    stamp = datetime.now(TZ).strftime("%H:%M")
    print(f"[{stamp}] Siden indlæst: '{page_text}'. Boliger med link fundet: {len(current)}.")

    if not current:
        # Reserverede og udlejede boliger har intet link, så det kan være
        # normalt. Den gemte status røres ikke.
        print("Ingen boliger med link lige nu. Status uændret.")
        return True, previous

    if previous is None:
        # Første kørsel nogensinde: gem baseline, send ingen mail
        save_state(current)
        commit_state("Gem baseline over boliger")
        print(f"Baseline gemt med {len(current)} boliger. Ingen mail sendt.")
        return True, current

    new_urls = [url for url in current if url not in previous]

    if new_urls:
        blocks = [f"- {current[url]}\n  {url}" for url in new_urls]
        body = "Nye boliger fundet på Kereby:\n\n" + "\n\n".join(blocks)
        subject = f"Ny bolig på Kereby ({len(new_urls)} stk.)"
        try:
            send_email(subject, body)
        except Exception as exc:  # noqa: BLE001
            # Status gemmes IKKE, så mailen prøves igen ved næste tjek
            print(f"FEJL: kunne ikke sende mail: {exc}")
            return False, previous
        print(f"Sendte mail om {len(new_urls)} ny(e) bolig(er).")

    if set(current) != set(previous):
        save_state(current)
        commit_state()

    return True, current


async def run_loop() -> None:
    started = time.monotonic()
    deadline = started + MAX_RUN_MIN * 60
    previous = load_state()
    failures = 0
    waiting_logged = False

    print(
        f"Starter overvågning: tjekker hvert {CHECK_INTERVAL_SEC:.0f}. sekund "
        f"mellem kl. {WINDOW_START_HOUR:02d} og {WINDOW_END_HOUR:02d} dansk tid, "
        f"i op til {MAX_RUN_MIN:.0f} minutter."
    )

    while True:
        tick = time.monotonic()
        now = datetime.now(TZ)

        if in_window(now):
            waiting_logged = False
            ok, previous = await check_once(previous)
            failures = 0 if ok else failures + 1

            if failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"FEJL: {failures} mislykkede tjek i træk. Stopper.")
                try:
                    send_email(
                        "Kereby-overvågning: tjek fejler",
                        f"Overvågningen af {URL} har fejlet {failures} gange i "
                        "træk og er stoppet. Se loggen under Actions i dit "
                        "GitHub-repo. Den prøver igen senere via sikkerhedsnettet "
                        "(skemaet), eller du kan starte den manuelt med "
                        "'Run workflow'.",
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"Kunne heller ikke sende fejlmail: {exc}")
                set_output("genstart", "false")
                sys.exit(1)
        elif not waiting_logged:
            print(f"[{now:%H:%M}] Uden for tidsvinduet - venter til kl. {WINDOW_START_HOUR:02d}.")
            waiting_logged = True

        wait = max(0.0, CHECK_INTERVAL_SEC - (time.monotonic() - tick))
        if time.monotonic() + wait >= deadline:
            break
        await asyncio.sleep(wait)

    print("Tidsgrænsen for denne kørsel er nået. Workflowet starter den næste.")
    set_output("genstart", "true")


if __name__ == "__main__":
    asyncio.run(run_loop())
