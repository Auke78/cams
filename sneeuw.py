"""Haalt de actuele sneeuwhoogte op van sneeuwhoogte.nl en schrijft sneeuw.json.

Gebruik: python sneeuw.py

1. Haalt één keer de overzichtspagina op met berg, dal en update van alle plaatsen.
   Zo vindt de website ook sneeuwhoogtes bij webcams die je zelf toevoegt.
2. Voor de skigebieden van de vaste webcams (index.html) en een eventuele
   pistecams.json haalt het ook de detailpagina op, voor laatste sneeuwval,
   open pistes en liften.
3. Een vaste koppeling kan in sneeuw-bronnen.json: {"Skigebied": "https://..."}.
Lukt een stap niet, dan blijven de vorige gegevens staan.
"""
import html
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
BRONNEN = HIER / "sneeuw-bronnen.json"
UITVOER = HIER / "sneeuw.json"
SITE = HIER / "index.html"
EXPORT = HIER / "pistecams.json"
OVERZICHT = "https://www.sneeuwhoogte.nl/overzicht"
USER_AGENT = "Pistecams/1.0 (persoonlijk webcamoverzicht; enkele verzoeken per run)"


# ---------- hulpfuncties ----------

def tekst(fragment):
    zonder_tags = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(zonder_tags)).strip()


def cm(waarde):
    m = re.search(r"(\d+)\s*cm", waarde or "")
    return int(m.group(1)) if m else None


def schoon_url(url):
    """Eén vaste schrijfwijze, zodat 'sölden' en 's%C3%B6lden' gelijk zijn."""
    u = urllib.parse.unquote(html.unescape(url.strip()))
    return unicodedata.normalize("NFC", u).rstrip("/")


def norm(naam):
    """'Serfaus - Fiss - Ladis' -> 'serfaus-fiss-ladis', 'Sölden' -> 'solden'."""
    t = unicodedata.normalize("NFC", naam.strip().lower())
    t = t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    t = "".join(ch for ch in unicodedata.normalize("NFD", t) if not unicodedata.combining(ch))
    t = t.replace("ae", "a").replace("oe", "o").replace("ue", "u")  # 'Soelden' = 'Sölden' = 'Solden'
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def zoek_plaats(naam, plaatsen):
    """Zelfde regels als op de website: dorp exact, skigebied exact, dan deelwoorden.
    Bij een skigebied telt de plaats met de meeste sneeuw op de berg."""
    doel = norm(naam)
    if not doel:
        return None
    woorden = set(doel.split("-"))
    beste = None
    for p in plaatsen:
        dorp, gebied = norm(p["naam"]), norm(p["skigebied"])
        slug = norm(p["url"].rsplit("/", 1)[-1])
        if doel in (dorp, slug):
            score = 1
        elif doel == gebied:
            score = 2
        elif woorden <= set(dorp.split("-")) | set(slug.split("-")):
            score = 3
        elif woorden <= set(gebied.split("-")):
            score = 4
        elif all(len(w) >= 4 and any(x.startswith(w) for x in alle) for w in woorden
                 for alle in [dorp.split("-") + slug.split("-") + gebied.split("-")]):
            score = 5
        else:
            continue
        in_gebied = 0 if dorp and dorp in gebied.split("-") else 1  # 'Saalbach' in 'Skicircus Saalbach Hinterglemm'
        sleutel = (score, in_gebied, -(p["berg"] if p["berg"] is not None else -1), len(p["naam"]))
        if beste is None or sleutel < beste[0]:
            beste = (sleutel, p)
    return beste[1] if beste else None


# ---------- pagina's lezen ----------

def lees_overzicht(bron):
    plaatsen = []
    for rij in re.findall(r"<tr>(.*?)</tr>", bron, re.S):
        if 'data-label="Sneeuwhoogte berg"' not in rij:
            continue
        m = re.search(r'<th[^>]*>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', rij, re.S)
        if not m:
            continue
        cel = {lbl: tekst(v) for lbl, v in re.findall(r'<td data-label="([^"]*)"[^>]*>(.*?)</td>', rij, re.S)}
        upd = cel.get("Laatste update")
        plaatsen.append({
            "naam": tekst(m.group(2)),
            "skigebied": cel.get("Skigebied", ""),
            "berg": cm(cel.get("Sneeuwhoogte berg")),
            "dal": cm(cel.get("Sneeuwhoogte dal")),
            "laatste_update": None if upd in (None, "", "-") else upd,
            "url": schoon_url(m.group(1)),
        })
    if len(plaatsen) < 50:
        raise ValueError(f"overzicht bevat maar {len(plaatsen)} plaatsen")
    return plaatsen


def lees_pagina(bron):
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
    rijen = {tekst(th).lower(): tekst(td)
             for th, td in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", kaart, re.S)}
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
    if not url.startswith("https://www.sneeuwhoogte.nl/"):
        raise ValueError("alleen links van sneeuwhoogte.nl")
    pad = urllib.parse.quote(urllib.parse.urlparse(url).path)
    req = urllib.request.Request("https://www.sneeuwhoogte.nl" + pad, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------- welke gebieden krijgen details ----------

def gebieden_uit_bestanden():
    namen = []

    def voeg_toe(cams):
        for c in cams or []:
            naam = str((c or {}).get("area") or "").strip()
            if naam and naam.lower() not in {n.lower() for n in namen}:
                namen.append(naam)

    if SITE.exists():
        m = re.search(r"var DEFAULTS=(\[.*?\]);", SITE.read_text(encoding="utf-8"), re.S)
        if m:
            voeg_toe(json.loads(m.group(1)))
    if EXPORT.exists():
        try:
            data = json.loads(EXPORT.read_text(encoding="utf-8"))
            voeg_toe(data.get("cams") if isinstance(data, dict) else data)
        except ValueError:
            print("FOUT  pistecams.json is geen geldig bestand", file=sys.stderr)
    return namen


# ---------- hoofdprogramma ----------

def main(ophalen=haal_op, pauze=2.0):
    try:
        vaste = {k: schoon_url(v) for k, v in json.loads(BRONNEN.read_text(encoding="utf-8")).items()}
    except FileNotFoundError:
        vaste = {}
    try:
        vorig = json.loads(UITVOER.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        vorig = {}

    # 1. Overzicht van alle plaatsen.
    try:
        plaatsen = lees_overzicht(ophalen(OVERZICHT))
        print(f"OK    overzicht: {len(plaatsen)} plaatsen")
    except Exception as fout:
        print(f"FOUT  overzicht: {fout}", file=sys.stderr)
        plaatsen = vorig.get("plaatsen", [])
    if not plaatsen:
        print("Geen gegevens, sneeuw.json blijft ongewijzigd.", file=sys.stderr)
        return 1

    # 2. Details voor de bekende skigebieden.
    namen = gebieden_uit_bestanden()
    for n in vaste:
        if n.lower() not in {x.lower() for x in namen}:
            namen.append(n)
    vaste_lc = {k.lower(): v for k, v in vaste.items()}
    urls = []
    for n in namen:
        url = vaste_lc.get(n.lower())
        if not url:
            p = zoek_plaats(n, plaatsen)
            url = p["url"] if p else None
        if url:
            print(f"KOPPELING  {n} -> {url}")
            if url not in urls:
                urls.append(url)
        else:
            print(f"NIET GEVONDEN  {n}: zet een vaste link in sneeuw-bronnen.json", file=sys.stderr)

    details, oud = {}, vorig.get("details", {})
    for i, url in enumerate(urls):
        if i:
            time.sleep(pauze)
        try:
            details[url] = lees_pagina(ophalen(url))
        except Exception as fout:
            print(f"FOUT  {url}: {fout}", file=sys.stderr)
            if url in oud:
                details[url] = oud[url]

    nieuw = {"bron": "sneeuwhoogte.nl", "koppelingen": vaste, "plaatsen": plaatsen, "details": details}
    if all(nieuw[k] == vorig.get(k) for k in nieuw):
        print("Geen wijzigingen.")
        return 0
    nieuw["opgehaald"] = datetime.now(timezone.utc).isoformat(timespec="minutes")
    UITVOER.write_text(json.dumps(nieuw, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"sneeuw.json bijgewerkt ({len(plaatsen)} plaatsen, {len(details)} met details).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
