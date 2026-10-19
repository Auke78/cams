"""Haalt de actuele sneeuwhoogte op van sneeuwhoogte.nl en schrijft sneeuw.json.

Gebruik: python sneeuw.py

Welke skigebieden: alle "area"-namen uit de vaste webcams in index.html en,
als die bestaat, uit een geëxporteerde pistecams.json in dezelfde map.
De pagina op sneeuwhoogte.nl wordt automatisch gezocht op naam. Klopt die niet,
zet dan in sneeuw-bronnen.json een vaste link: {"Skigebied": "https://..."}.
Lukt een gebied niet, dan blijven de vorige gegevens van dat gebied staan.
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
SITEMAP = "https://www.sneeuwhoogte.nl/sitemaps-1-sitemap.xml"
USER_AGENT = "Pistecams/1.0 (persoonlijk webcamoverzicht; 1 verzoek per gebied per run)"
SUBPAGINAS = {"weer", "webcams", "sneeuwhistorie", "pistekaart", "reviews", "live-updates", "accommodaties"}


# ---------- welke gebieden ----------

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
        data = json.loads(EXPORT.read_text(encoding="utf-8"))
        voeg_toe(data.get("cams") if isinstance(data, dict) else data)
    return namen


# ---------- naam -> pagina op sneeuwhoogte.nl ----------

def slug(tekst):
    """'Saalbach Hinterglemm' -> 'saalbach-hinterglemm' (umlauts blijven staan)."""
    t = unicodedata.normalize("NFC", tekst.strip().lower())
    return re.sub(r"[^\w]+", "-", t).strip("-")


def zonder_umlaut(tekst):
    t = tekst.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return "".join(ch for ch in unicodedata.normalize("NFD", t) if not unicodedata.combining(ch))


def locaties(sitemap_xml_teksten):
    """Alle gebiedspagina's: /overzicht/land/regio/gebied[/plaats], zonder subpagina's."""
    uit = []
    for xml in sitemap_xml_teksten:
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            pad = urllib.parse.unquote(urllib.parse.urlparse(loc).path).strip("/").split("/")
            if len(pad) in (4, 5) and pad[0] == "overzicht" and pad[-1] not in SUBPAGINAS:
                uit.append((loc, [unicodedata.normalize("NFC", p.lower()) for p in pad[3:]]))
    return uit


def zoek_pagina(naam, kandidaten):
    """Beste match: plaats met precies die naam, dan gebied, dan plaats/gebied met die naam als deel."""
    doel = slug(naam)
    varianten = {doel, zonder_umlaut(doel)}

    def score(segmenten):
        laatste = segmenten[-1]
        is_plaats = len(segmenten) == 2
        for v in varianten:
            if laatste == v or zonder_umlaut(laatste) == zonder_umlaut(v):
                return 1 if is_plaats else 2
        delen = set(laatste.split("-")) | set(zonder_umlaut(laatste).split("-"))
        for v in varianten:
            if set(v.split("-")) <= delen:
                return 3 if is_plaats else 4
        return None

    beste = None
    for url, segmenten in kandidaten:
        s = score(segmenten)
        if s is not None and (beste is None or s < beste[0] or (s == beste[0] and len(url) < len(beste[1]))):
            beste = (s, url)
    return beste[1] if beste else None


def sitemap_teksten(ophalen):
    index = ophalen(SITEMAP)
    delen = [u for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index) if "section-locations" in u]
    return [ophalen(u) for u in delen] if delen else [index]


# ---------- sneeuwgegevens uit een pagina ----------

def tekst(fragment):
    zonder_tags = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(zonder_tags)).strip()


def cm(waarde):
    m = re.search(r"(\d+)\s*cm", waarde or "")
    return int(m.group(1)) if m else None


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


# ---------- hoofdprogramma ----------

def main(ophalen=haal_op, pauze=2.0):
    try:
        vaste = json.loads(BRONNEN.read_text(encoding="utf-8"))
    except FileNotFoundError:
        vaste = {}
    try:
        vorig = json.loads(UITVOER.read_text(encoding="utf-8")).get("gebieden", {})
    except (FileNotFoundError, ValueError):
        vorig = {}

    namen = gebieden_uit_bestanden()
    for n in vaste:
        if n.lower() not in {x.lower() for x in namen}:
            namen.append(n)

    # 1. Link per gebied: vaste link, anders eerder gevonden, anders zoeken in de sitemap.
    vaste_lc = {k.lower(): v for k, v in vaste.items()}
    vorig_lc = {k.lower(): v.get("url") for k, v in vorig.items() if v.get("url")}
    links, te_zoeken = {}, []
    for n in namen:
        url = vaste_lc.get(n.lower()) or vorig_lc.get(n.lower())
        if url:
            links[n] = url
        else:
            te_zoeken.append(n)

    if te_zoeken:
        try:
            kandidaten = locaties(sitemap_teksten(ophalen))
        except Exception as fout:
            print(f"FOUT  sitemap: {fout}", file=sys.stderr)
            kandidaten = []
        for n in te_zoeken:
            url = zoek_pagina(n, kandidaten)
            if url:
                links[n] = url
                print(f"GEVONDEN  {n}: {urllib.parse.unquote(url)}")
            else:
                print(f"NIET GEVONDEN  {n}: zet een vaste link in sneeuw-bronnen.json", file=sys.stderr)

    # 2. Sneeuwgegevens ophalen.
    gebieden, fouten = {}, 0
    for i, (naam, url) in enumerate(links.items()):
        if i:
            time.sleep(pauze)
        try:
            data = lees_pagina(ophalen(url))
            data["url"] = url
            gebieden[naam] = data
            print(f"OK    {naam}: berg {data['berg']} cm, dal {data['dal']} cm")
        except Exception as fout:
            fouten += 1
            print(f"FOUT  {naam}: {fout}", file=sys.stderr)
            oud = next((v for k, v in vorig.items() if k.lower() == naam.lower()), None)
            if oud:
                gebieden[naam] = oud

    if not gebieden:
        print("Niets opgehaald, sneeuw.json blijft ongewijzigd.", file=sys.stderr)
        return 1

    if gebieden == vorig:
        print("Geen wijzigingen.")
        return 0
    nieuw = {"bron": "sneeuwhoogte.nl", "gebieden": gebieden,
             "opgehaald": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    UITVOER.write_text(json.dumps(nieuw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"sneeuw.json bijgewerkt ({len(gebieden)} gebieden, {fouten} fouten).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
