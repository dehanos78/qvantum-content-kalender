#!/usr/bin/env python3
"""
sync-hubspot.py — haalt gepubliceerde LinkedIn-posts op uit HubSpot's Social tool
en schrijft ze naar hubspot-sync.local.json in het content-kalender formaat.

Daarna in het dashboard: knop "Sync uit HubSpot" -> kies hubspot-sync.local.json.
De import matcht op hubspotId, dus herhaald draaien maakt geen dubbele items.

VEREIST:
  - Een HubSpot Private App token met de 'social' scope.
    Aanmaken: HubSpot > Settings > Integrations > Private Apps > Create.
    Scopes tabblad -> zoek op "social" -> vink de read-scope aan.
  - Token in .env als:  HUBSPOT_TOKEN=pat-eu1-xxxxxxxx

LET OP: HubSpot's Broadcast/Social API is "legacy". Hij werkt nog, maar de
respons-structuur kan per account verschillen. Draai met --debug om de ruwe
respons te zien als de mapping niet klopt, en stuur die output door.

Gebruik:
  python3 sync-hubspot.py              # normale run
  python3 sync-hubspot.py --debug      # toont ruwe API-respons + kanalen
  python3 sync-hubspot.py --since 2026-01-01   # alleen posts vanaf datum
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://api.hubapi.com"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SCRIPT_DIR / "hubspot-sync.local.json"

# Map van HubSpot channel-naam (lowercase substring) -> content-kalender kanaal-label.
# Pas deze aan als jouw LinkedIn-accounts anders heten in HubSpot.
CHANNEL_MAP = [
    ("qvantum", "LinkedIn — Qvantum Nederland"),
    ("viktor", "LinkedIn — Viktor de Haan"),
]


def load_token() -> str:
    """Lees HUBSPOT_TOKEN uit omgeving of uit een .env in dezelfde map."""
    token = os.environ.get("HUBSPOT_TOKEN")
    if token:
        return token.strip()
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("HUBSPOT_TOKEN") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(
        "FOUT: geen HUBSPOT_TOKEN gevonden.\n"
        "Zet hem in een .env bestand naast dit script:\n"
        "  HUBSPOT_TOKEN=pat-eu1-xxxxxxxx\n"
    )


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    url = BASE + path
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 401:
            sys.exit("FOUT 401: token ongeldig of mist de 'social' scope.")
        if e.code == 403:
            sys.exit(f"FOUT 403: token heeft geen toegang tot {path}. Controleer de 'social' scope.\n{body}")
        sys.exit(f"FOUT {e.code} bij {path}:\n{body}")
    except URLError as e:
        sys.exit(f"NETWERKFOUT bij {path}: {e.reason}")


def fetch_channels(token: str) -> dict:
    """Haal gekoppelde social channels op -> {channelGuid: channelName}."""
    data = api_get("/broadcast/v1/channels/setting/publish/current", token)
    channels = {}
    items = data if isinstance(data, list) else data.get("results", data.get("objects", []))
    for ch in items or []:
        guid = ch.get("channelGuid") or ch.get("channelId") or ch.get("guid")
        name = ch.get("name") or ch.get("channelName") or ch.get("accountName") or ""
        ctype = (ch.get("channelKey") or ch.get("type") or "").lower()
        if guid:
            channels[guid] = {"name": name, "type": ctype}
    return channels


def map_kanaal(channel_name: str, channel_type: str) -> str | None:
    """Bepaal het content-kalender kanaal-label op basis van de channel-naam.
    Geeft None terug als het geen LinkedIn-kanaal is dat we willen syncen."""
    if "linkedin" not in channel_type and "linkedin" not in channel_name.lower():
        return None
    low = channel_name.lower()
    for needle, label in CHANNEL_MAP:
        if needle in low:
            return label
    # LinkedIn maar onbekend account -> standaard bedrijfspagina
    return "LinkedIn — Qvantum Nederland"


def to_iso_date(ms_or_iso) -> str:
    """HubSpot timestamps zijn epoch-ms. Geef YYYY-MM-DD terug."""
    if ms_or_iso is None:
        return ""
    if isinstance(ms_or_iso, (int, float)):
        return datetime.fromtimestamp(ms_or_iso / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(ms_or_iso)
    return s[:10] if len(s) >= 10 else s


def fetch_broadcasts(token: str, since: str | None, debug: bool) -> list:
    """Haal gepubliceerde broadcasts op met paginatie."""
    out = []
    offset = 0
    while True:
        params = {"count": 100, "offset": offset, "status": "SUCCESS"}
        data = api_get("/broadcast/v1/broadcasts", token, params)
        batch = data if isinstance(data, list) else data.get("results", data.get("objects", []))
        if debug and offset == 0:
            print("=== RUWE BROADCAST RESPONS (eerste batch) ===", file=sys.stderr)
            print(json.dumps(batch[:2] if batch else data, indent=2)[:4000], file=sys.stderr)
            print("=== EINDE DEBUG ===\n", file=sys.stderr)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    if since:
        out = [b for b in out if to_iso_date(b.get("finishedAt") or b.get("triggerAt")) >= since]
    return out


def main():
    ap = argparse.ArgumentParser(description="Sync gepubliceerde LinkedIn-posts uit HubSpot.")
    ap.add_argument("--debug", action="store_true", help="Toon ruwe API-respons en kanalen.")
    ap.add_argument("--since", help="Alleen posts vanaf deze datum (YYYY-MM-DD).")
    args = ap.parse_args()

    token = load_token()

    print("Kanalen ophalen...")
    channels = fetch_channels(token)
    if args.debug:
        print("=== GEKOPPELDE KANALEN ===", file=sys.stderr)
        print(json.dumps(channels, indent=2), file=sys.stderr)
        print("=== EINDE ===\n", file=sys.stderr)
    print(f"  {len(channels)} kanaal(en) gevonden.")

    print("Gepubliceerde broadcasts ophalen...")
    broadcasts = fetch_broadcasts(token, args.since, args.debug)
    print(f"  {len(broadcasts)} gepubliceerde broadcast(s).")

    items = []
    skipped = 0
    for b in broadcasts:
        guid = b.get("broadcastGuid") or b.get("guid")
        ch_guid = b.get("channelGuid") or b.get("channel")
        ch = channels.get(ch_guid, {})
        ch_name = ch.get("name", "")
        ch_type = ch.get("type", "") or (b.get("channelKey") or "")

        kanaal = map_kanaal(ch_name, ch_type)
        if not kanaal:
            skipped += 1
            continue

        content = b.get("content", {}) or {}
        body = content.get("body") or content.get("message") or b.get("message") or ""
        body = body.strip()
        first_line = body.split("\n", 1)[0][:120] if body else ""

        link = (
            content.get("shortenedLinkUrl")
            or content.get("linkUrl")
            or b.get("linkUrl")
            or b.get("clickThroughUrl")
            or ""
        )

        items.append({
            "hubspotId": guid,
            "datum": to_iso_date(b.get("finishedAt") or b.get("triggerAt")),
            "kanaal": kanaal,
            "auteur": "Viktor de Haan" if "Viktor" in kanaal else "Qvantum NL bedrijfspagina",
            "type": "LinkedIn post",
            "status": "Gepubliceerd",
            "omschrijving": first_line,
            "content": body,
            "linkedinUrl": link,
        })

    items.sort(key=lambda x: x["datum"])
    payload = {
        "_meta": {
            "source": "HubSpot Social (Broadcast API)",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
        },
        "items": items,
    }
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nKlaar: {len(items)} LinkedIn-post(s) weggeschreven naar {OUTPUT_FILE.name}")
    if skipped:
        print(f"  ({skipped} niet-LinkedIn broadcast(s) overgeslagen)")
    print("\nVolgende stap: open het dashboard -> 'Sync uit HubSpot' -> kies dit bestand.")


if __name__ == "__main__":
    main()
