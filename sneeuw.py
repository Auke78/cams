"""Haalt de actuele sneeuwhoogte op van sneeuwhoogte.nl en schrijft sneeuw.json.

Gebruik: python sneeuw.py
Leest sneeuw-bronnen.json (skigebied -> pagina op sneeuwhoogte.nl).
Lukt een gebied niet, dan blijven de vorige gegevens van dat gebied staan.
"""
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
BRONNEN = HIER / "sneeuw-bronnen.json"
UITVOER = HIER / "sneeuw.json"
USER_AGENT = "Pistecams/1.0 (persoonlijk webcamoverzicht; 1 verzoek per gebied per run)"


def tekst(fragment):
    """Verwijder tags en overbodige witruimte."""
    zonder_tags = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(zonder_tags)).strip()


def cm(waarde):
    m = re.search(r"(\d+)\s*cm", waarde or "")
    return int(m.group(1)) if m else None


def lees_pagina(bron):
    """Haal berg, dal en de tabelregels uit de samenvattingskaart."""
    start = bron.find('class="summary-card')
    if start < 0:
        raise ValueError("samenvattingskaart niet gevonden")
    eind = bron.find("</article>", start)
    kaart = bron[start:eind if eind > 0 else None]

    hoogtes = {}
    for label, waarde in re.findall(
        r'<span class="summary-card__label">\s*([^<]+?)\s*</span>\s*<span[^>]*>([^<]*)</span>', kaart
    ):
        hoogtes[label.strip().lower()] = waarde.strip()

    rijen = {}
    for th, td in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", kaart, re.S):
        rijen[tekst(th).lower()] = tekst(td)

    if "berg" not in hoogtes and "laatste update" not in rijen:
        raise ValueError("geen sneeuwgegevens op de pagina")

    def rij(naam):
        w = rijen.get(naam)
        return None if w in (None, "", "-") else w

    return {
        "berg": cm(hoogtes.get("berg")),
        "dal": cm(hoogtes.get("dal")),
        "laatste_update": rij("laatste update"),
        "laatste_sneeuwval": rij("laatste sneeuwval"),
        "laatste_24_uur": rij("laatste 24 uur"),
        "pistes_open": rij("pistes open"),
        "liften_open": rij("liften open"),
        "pistekwaliteit": rij("pistekwaliteit"),
    }


def haal_op(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main(ophalen=haal_op, pauze=2.0):
    bronnen = json.loads(BRONNEN.read_text(encoding="utf-8"))
    try:
        vorig = json.loads(UITVOER.read_text(encoding="utf-8")).get("gebieden", {})
    except (FileNotFoundError, ValueError):
        vorig = {}

    gebieden, fouten = {}, 0
    for i, (naam, url) in enumerate(bronnen.items()):
        if i:
            time.sleep(pauze)
        try:
            data = lees_pagina(ophalen(url))
            data["url"] = url
            gebieden[naam] = data
            print(f"OK    {naam}: berg {data['berg']} cm, dal {data['dal']} cm")
        except Exception as fout:  # één gebied mag de rest niet tegenhouden
            fouten += 1
            print(f"FOUT  {naam}: {fout}", file=sys.stderr)
            if naam in vorig:
                gebieden[naam] = vorig[naam]

    if not gebieden:
        print("Niets opgehaald, sneeuw.json blijft ongewijzigd.", file=sys.stderr)
        return 1

    nieuw = {"bron": "sneeuwhoogte.nl", "gebieden": gebieden}
    oud = {"bron": "sneeuwhoogte.nl", "gebieden": vorig}
    if nieuw == oud:
        print("Geen wijzigingen.")
        return 0
    nieuw["opgehaald"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    UITVOER.write_text(json.dumps(nieuw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"sneeuw.json bijgewerkt ({len(gebieden)} gebieden, {fouten} fouten).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
