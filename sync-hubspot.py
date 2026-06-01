#!/usr/bin/env python3
"""
sync-hubspot.py — leest een HubSpot Social "Export posts" XLSX en schrijft
gepubliceerde LinkedIn-posts uit Qvantum Nederland + Viktor de Haan weg
naar hubspot-sync.local.json in het content-kalender formaat.

Daarna in het dashboard: knop "Sync uit HubSpot" -> kies hubspot-sync.local.json.
De import matcht op hubspotId (de LinkedIn Published URL), dus herhaald
draaien maakt geen dubbele items.

WAAROM XLSX en geen API:
  De HubSpot Broadcast API werkt niet meer met de nieuwe Social-tool van
  HubSpot (Insights Beta, Social Post Agent, etc.). De UI-export is de
  enige betrouwbare weg om gepubliceerde posts te exporteren.

VEREIST:
  pip3 install --user openpyxl

EXPORT MAKEN IN HUBSPOT:
  Marketing > Social > Manage > [filters naar smaak] > Export posts
  (HubSpot exporteert in praktijk alles ongeacht je filters; dat is OK,
   we filteren hier opnieuw op kanaal.)

Gebruik:
  python3 sync-hubspot.py                           # zoekt nieuwste Downloads/hubspot-social-published-*.xlsx
  python3 sync-hubspot.py /pad/naar/export.xlsx
  python3 sync-hubspot.py export.xlsx --all         # geen kanaal-filter, alle channels meenemen
  python3 sync-hubspot.py export.xlsx --since 2026-01-01
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("FOUT: openpyxl niet geinstalleerd. Run:\n  pip3 install --user openpyxl")

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SCRIPT_DIR / "hubspot-sync.local.json"
DOWNLOADS = Path.home() / "Downloads"

# Filter: alleen deze HubSpot channel-namen worden gesynct.
# Tuple: (channel-naam in export, kalender-kanaal-label, default auteur).
# Pas aan als je meer accounts wilt meenemen, of gebruik --all.
CHANNEL_FILTER = [
    ("Qvantum Nederland", "LinkedIn — Qvantum Nederland", "Qvantum NL bedrijfspagina"),
    ("Viktor de Haan",    "LinkedIn — Viktor de Haan",     "Viktor de Haan"),
]


def find_latest_export() -> Path | None:
    """Zoek de meest recente HubSpot social export in ~/Downloads."""
    candidates = sorted(
        DOWNLOADS.glob("hubspot-social-published-*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def parse_date(value) -> str:
    """Geef YYYY-MM-DD terug. Accepteert datetime of string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value)
    return s[:10]


def iso_week(date_str: str) -> str:
    """'2026-03-08' -> 'Week 10'."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"Week {d.isocalendar()[1]}"
    except Exception:
        return ""


def first_line(text: str, max_len: int = 120) -> str:
    if not text:
        return ""
    line = text.strip().split("\n", 1)[0]
    return line[:max_len].rstrip()


def read_export(path: Path) -> tuple[list[str], list[tuple]]:
    """Lees de Excel en geef (header, rows) terug. Negeert openpyxl warnings."""
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        sys.exit(f"FOUT: leeg sheet in {path.name}.")
    return list(rows[0]), rows[1:]


def col(header: list[str], name: str, required: bool = True) -> int:
    """Vind een kolom-index op naam; case-insensitief, tolerant voor witruimte."""
    norm = {str(h).strip().lower(): i for i, h in enumerate(header) if h is not None}
    idx = norm.get(name.strip().lower())
    if idx is None and required:
        sys.exit(f"FOUT: kolom '{name}' niet gevonden. Beschikbaar: {list(norm.keys())}")
    return idx if idx is not None else -1


def main():
    ap = argparse.ArgumentParser(description="Sync HubSpot Social XLSX-export naar content-kalender JSON.")
    ap.add_argument("file", nargs="?", help="Pad naar XLSX (default: nieuwste in ~/Downloads).")
    ap.add_argument("--all", action="store_true", help="Geen kanaal-filter, neem alle channels mee.")
    ap.add_argument("--since", help="Alleen posts vanaf deze datum (YYYY-MM-DD).")
    ap.add_argument("--linkedin-only", action="store_true",
                    help="In combinatie met --all: alleen LinkedIn channels (geen FB/IG/YT).")
    args = ap.parse_args()

    # Bestand vinden
    if args.file:
        path = Path(args.file).expanduser().resolve()
    else:
        latest = find_latest_export()
        if not latest:
            sys.exit(f"FOUT: geen export gevonden in {DOWNLOADS}/hubspot-social-published-*.xlsx\n"
                     f"Geef het pad expliciet mee: python3 sync-hubspot.py /pad/naar/file.xlsx")
        path = latest
        print(f"Gebruik nieuwste export: {path.name}")

    if not path.exists():
        sys.exit(f"FOUT: bestand niet gevonden: {path}")

    header, rows = read_export(path)
    print(f"  {len(rows)} rij(en) in export.")

    # Kolommen
    i_status   = col(header, "Status")
    i_chtype   = col(header, "Channel Type")
    i_chname   = col(header, "Channel Name")
    i_pubtime  = col(header, "Publish Time")
    i_campaign = col(header, "Campaign", required=False)
    i_message  = col(header, "Published Message")
    i_url      = col(header, "Published URL")
    i_link     = col(header, "Original Link", required=False)

    # Filter map: channel-naam -> (kalender-kanaal, auteur)
    filter_map = {name: (label, author) for name, label, author in CHANNEL_FILTER}

    items = []
    skipped_status = 0
    skipped_channel = 0
    skipped_type = 0
    skipped_date = 0

    for r in rows:
        if not r or r[i_chname] is None:
            continue

        # Alleen geslaagd gepubliceerde posts
        if str(r[i_status]).upper() != "SUCCESS":
            skipped_status += 1
            continue

        ch_name = str(r[i_chname]).strip()
        ch_type = str(r[i_chtype] or "").strip()

        # Kanaal-filter
        if not args.all:
            if ch_name not in filter_map:
                skipped_channel += 1
                continue
            kanaal, auteur = filter_map[ch_name]
        else:
            if args.linkedin_only and "linkedin" not in ch_type.lower():
                skipped_type += 1
                continue
            # Auto-mapping bij --all: probeer match in filter_map, anders fallback
            if ch_name in filter_map:
                kanaal, auteur = filter_map[ch_name]
            else:
                kanaal = f"LinkedIn — {ch_name}" if "linkedin" in ch_type.lower() else f"{ch_type} — {ch_name}"
                auteur = ch_name

        datum = parse_date(r[i_pubtime])
        if args.since and datum and datum < args.since:
            skipped_date += 1
            continue

        message = str(r[i_message] or "")
        url = str(r[i_url] or "")
        link = str(r[i_link] or "") if i_link >= 0 else ""

        if not url:
            # Geen Published URL -> geen stabiele hubspotId, sla over
            continue

        campaign = str(r[i_campaign] or "").strip() if i_campaign >= 0 else ""

        items.append({
            "hubspotId": url,  # LinkedIn URN-URL is uniek per post
            "datum": datum,
            "week": iso_week(datum),
            "kanaal": kanaal,
            "auteur": auteur,
            "type": "LinkedIn post",
            "thema": campaign or "LinkedIn",
            "status": "Gepubliceerd",
            "omschrijving": first_line(message),
            "content": message.strip(),
            "linkedinUrl": url,
            "originalLink": link,
        })

    items.sort(key=lambda x: x["datum"])
    payload = {
        "_meta": {
            "source": "HubSpot Social UI Export (XLSX)",
            "file": path.name,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
            "filter": "all channels" if args.all else [n for n, _, _ in CHANNEL_FILTER],
        },
        "items": items,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # Rapport
    print(f"\nKlaar: {len(items)} post(s) weggeschreven naar {OUTPUT_FILE.name}")
    if items:
        print("\nVoorbeeld eerste 3:")
        for it in items[:3]:
            print(f"  {it['datum']} | {it['kanaal']:38s} | {it['omschrijving'][:60]}")
    if skipped_status: print(f"  ({skipped_status} niet-SUCCESS overgeslagen)")
    if skipped_channel: print(f"  ({skipped_channel} buiten kanaal-filter)")
    if skipped_type: print(f"  ({skipped_type} niet-LinkedIn)")
    if skipped_date: print(f"  ({skipped_date} voor --since datum)")
    print("\nVolgende stap: dashboard -> 'Sync uit HubSpot' -> kies dit bestand.")


if __name__ == "__main__":
    main()
