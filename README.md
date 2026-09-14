# Overvågning af nye boliger på Kereby.dk

Dette sætter en helt gratis, automatisk overvågning op af
https://kereby.dk/bolig/ — du får en email, hver gang der kommer en ny
bolig på listen (ikke ved statusskift på eksisterende boliger).

## Sådan virker det

- Et Python-script (`check_kereby.py`) henter siden med en rigtig browser
  (Playwright), finder alle boliglinks, og sammenligner med den liste det
  gemte sidste gang.
- Nye links → du får en email med adresse og link.
- Et GitHub Actions-workflow (`check-kereby.yml`) kører scriptet
  automatisk hver time, helt gratis.

## Opsætning (ca. 10 minutter)

1. **Opret et GitHub-repo**
   Gå til github.com → "New repository". Offentligt eller privat er fint
   — det koster intet ekstra, og dine hemmeligheder (adgangskoder) er
   krypterede uanset hvad.

2. **Upload filerne**
   Læg `check_kereby.py` og `requirements.txt` i roden af repoet, og
   `check-kereby.yml` i en mappe der hedder `.github/workflows/`.

3. **Opret et Gmail App Password**
   - Sørg for at 2-trins bekræftelse er slået til på din Google-konto.
   - Gå til https://myaccount.google.com/apppasswords
   - Opret et nyt password (vælg "Mail" / brugerdefineret navn, fx
     "Kereby overvågning"). Kopier den 16-cifrede kode — du skal ikke
     bruge din normale Gmail-adgangskode nogen steder.

4. **Tilføj hemmeligheder i GitHub**
   I dit repo: Settings → Secrets and variables → Actions → "New
   repository secret". Opret disse tre:
   - `GMAIL_USER` – din Gmail-adresse
   - `GMAIL_APP_PASSWORD` – den 16-cifrede kode fra trin 3
   - `EMAIL_TO` – den email du vil modtage notifikationer på (kan være
     samme som `GMAIL_USER`)

5. **Lad den køre**
   Workflowet kører automatisk hvert 20. minut. Du kan også starte det
   manuelt med det samme under fanen "Actions" → "Overvåg Kereby
   boliger" → "Run workflow".

   **Vigtigt ved 20-minutters interval:** Det giver ca. 72 kørsler i
   døgnet. Et **privat** repo har kun 2.000 gratis Actions-minutter om
   måneden, og det kan denne frekvens godt overskride. Et **offentligt**
   repo har ubegrænset gratis kørsel, uanset frekvens. Så medmindre du
   har en god grund til at holde det privat, anbefaler jeg et offentligt
   repo her — det afslører intet følsomt, da alle hemmeligheder
   (adgangskoder) altid er krypterede uanset synlighed.

6. **Første kørsel = baseline**
   Den allerførste kørsel gemmer bare de boliger, der findes lige nu,
   og sender ingen mail. Fra kørsel nummer to får du besked om alt nyt.

## Tilpasning

- **Andet interval:** Rediger linjen `cron: "*/20 * * * *"` i
  workflow-filen, fx til `"0 * * * *"` for hver time i stedet.
- **Andet websted:** Ændr `URL` og regex-mønsteret `LISTING_HREF_RE`
  øverst i `check_kereby.py`.
