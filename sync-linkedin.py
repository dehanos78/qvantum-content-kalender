#!/usr/bin/env python3
"""
sync-linkedin.py — leest LinkedIn analytics "Content" exports (.xls) en
schrijft gepubliceerde posts weg naar linkedin-sync.local.json in het
content-kalender formaat.

Daarna in het dashboard: knop "Sync gepubliceerde posts" -> kies
linkedin-sync.local.json. Merge gebeurt op LinkedIn URL (urn:li:activity:NNN),
dus herhaald draaien maakt geen dubbele items.

BRON:
  LinkedIn analytics geeft een .xls met een sheet "Alle bijdragen" dat
  alle company-page-posts bevat. Deze exports staan al onder
  linkedin-dashboard/data/linkedin-exports/ — gegenereerd door de scrape
  daar. Dit script raakt die andere dashboard niet aan, leest alleen de
  .xls bestanden.

VEREIST:
  pip3 install --user xlrd==1.2.0

Gebruik:
  python3 sync-linkedin.py                                # alle .xls in default-map
  python3 sync-linkedin.py /pad/naar/één.xls
  python3 sync-linkedin.py /pad/naar/map_met_xls/
  python3 sync-linkedin.py --since 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import xlrd
except ImportError:
    sys.exit("FOUT: xlrd niet geinstalleerd. Run:\n  pip3 install --user xlrd==1.2.0")

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SCRIPT_DIR / "linkedin-sync.local.json"

# Default-map: linkedin-dashboard's eigen export folder (read-only).
DEFAULT_DIR = (
    SCRIPT_DIR.parent / "linkedin-dashboard" / "data" / "linkedin-exports"
)

# Map LinkedIn page slug (uit bestandsnaam) -> content-kalender kanaal-label.
PAGE_MAP = {
    "qvantum-nederland": ("LinkedIn — Qvantum Nederland", "Qvantum NL bedrijfspagina"),
}
DEFAULT_KANAAL = ("LinkedIn — Qvantum Nederland", "Qvantum NL bedrijfspagina")


def find_xls_files(arg_path: str | None) -> list[Path]:
    """Bepaal welke .xls files we lezen."""
    if arg_path:
        p = Path(arg_path).expanduser().resolve()
        if p.is_file():
            return [p]
        if p.is_dir():
            return sorted(p.glob("*content*.xls"))
        sys.exit(f"FOUT: pad bestaat niet: {p}")
    if not DEFAULT_DIR.exists():
        sys.exit(f"FOUT: default-map niet gevonden: {DEFAULT_DIR}\n"
                 f"Geef een pad mee: python3 sync-linkedin.py /pad/naar/file.xls")
    files = sorted(DEFAULT_DIR.glob("*content*.xls"))
    if not files:
        sys.exit(f"FOUT: geen *content*.xls bestanden gevonden in {DEFAULT_DIR}")
    return files


def parse_us_date(value) -> str:
    """LinkedIn geeft 'MM/DD/YYYY' -> YYYY-MM-DD."""
    if not value:
        return ""
    s = str(value).strip()
    # Soms is het al een float (Excel-datum) — handle apart
    try:
        if "/" in s:
            m, d, y = s.split(" ")[0].split("/")
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        # Als YYYY-MM-DD al
        if "-" in s and len(s) >= 10:
            return s[:10]
    except Exception:
        pass
    return s


def iso_week(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"Week {d.isocalendar()[1]}"
    except Exception:
        return ""


def kanaal_from_filename(name: str) -> tuple[str, str]:
    low = name.lower()
    for slug, mapping in PAGE_MAP.items():
        if slug in low:
            return mapping
    return DEFAULT_KANAAL


def read_alle_bijdragen(path: Path) -> list[dict]:
    """Lees sheet 'Alle bijdragen'. Rij 0 = uitleg, rij 1 = header, rij 2+ = data."""
    try:
        wb = xlrd.open_workbook(path)
    except Exception as e:
        print(f"  Skip {path.name}: kan niet openen ({e})", file=sys.stderr)
        return []
    if "Alle bijdragen" not in wb.sheet_names():
        print(f"  Skip {path.name}: geen sheet 'Alle bijdragen'", file=sys.stderr)
        return []
    ws = wb.sheet_by_name("Alle bijdragen")
    if ws.nrows < 3:
        return []

    header = [str(h).strip() for h in ws.row_values(1)]
    try:
        i_title    = header.index("Titel bijdrage")
        i_link     = header.index("Link plaatsen")
        i_type     = header.index("Soort bijdrage")
        i_campaign = header.index("Campagnenaam")
        i_author   = header.index("Geplaatst door")
        i_created  = header.index("Aangemaakt")
    except ValueError as e:
        print(f"  Skip {path.name}: header niet zoals verwacht ({e})", file=sys.stderr)
        return []

    kanaal, default_auteur = kanaal_from_filename(path.name)

    rows = []
    for r in range(2, ws.nrows):
        row = ws.row_values(r)
        link = str(row[i_link] or "").strip()
        if not link or "linkedin.com" not in link:
            continue
        title = str(row[i_title] or "").strip()
        author = str(row[i_author] or "").strip() or default_auteur
        datum = parse_us_date(row[i_created])
        campaign = str(row[i_campaign] or "").strip()
        post_type = str(row[i_type] or "").strip()

        rows.append({
            "hubspotId": link,                    # gebruik LinkedIn URL als unieke key
            "datum": datum,
            "week": iso_week(datum),
            "kanaal": kanaal,
            "auteur": author,
            "type": "LinkedIn post",
            "thema": campaign or ("Spontaan" if post_type == "Spontaan" else (post_type or "LinkedIn")),
            "status": "Gepubliceerd",
            "omschrijving": title[:120],
            "content": title,
            "linkedinUrl": link,
            "source": "LinkedIn analytics export",
            "exportFile": path.name,
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description="Sync LinkedIn analytics XLS exports naar content-kalender JSON.")
    ap.add_argument("path", nargs="?", help="Pad naar .xls of map (default: linkedin-dashboard/data/linkedin-exports/).")
    ap.add_argument("--since", help="Alleen posts vanaf deze datum (YYYY-MM-DD).")
    args = ap.parse_args()

    files = find_xls_files(args.path)
    print(f"{len(files)} bestand(en) verwerken:")
    for f in files:
        print(f"  - {f.name}")

    by_url: dict[str, dict] = {}  # dedupe op LinkedIn URL; later export wint
    skipped_date = 0
    for f in files:
        rows = read_alle_bijdragen(f)
        for row in rows:
            if args.since and row["datum"] and row["datum"] < args.since:
                skipped_date += 1
                continue
            # Last write wins — nieuwere exports kunnen titel/data verfijnen
            by_url[row["linkedinUrl"]] = row

    items = sorted(by_url.values(), key=lambda x: x["datum"])
    payload = {
        "_meta": {
            "source": "LinkedIn Analytics Content Export",
            "files": [f.name for f in files],
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
        },
        "items": items,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nKlaar: {len(items)} unieke post(s) weggeschreven naar {OUTPUT_FILE.name}")
    if items:
        print("\nVoorbeeld eerste 3:")
        for it in items[:3]:
            print(f"  {it['datum']} | {it['auteur']:18s} | {it['omschrijving'][:60]}")
        print("\nVoorbeeld laatste 3:")
        for it in items[-3:]:
            print(f"  {it['datum']} | {it['auteur']:18s} | {it['omschrijving'][:60]}")
    if skipped_date:
        print(f"  ({skipped_date} voor --since datum overgeslagen)")
    print("\nVolgende stap: dashboard -> 'Sync gepubliceerde posts' -> kies dit bestand.")


if __name__ == "__main__":
    main()
