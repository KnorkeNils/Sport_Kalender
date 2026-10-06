#!/usr/bin/env python3
"""Sport-Kalender-Agent: sammelt Termine und schreibt abonnierbare .ics-Feeds.

Ablauf pro Lauf:
  1. watchlist.yaml lesen
  2. jede Quelle abrufen (ICS-Feed, DFB-Tabelle, PandaScore-API, statische Liste)
  3. Ergebnis in data/events.json MERGEN (Vergangenes bleibt, Zukünftiges wird aktualisiert)
  4. docs/*.ics neu schreiben (pro Kalender + all.ics + index.html)
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "events.json"
OUT_DIR = ROOT / "docs"
UA = "sport-calendar-agent/1.0 (personal calendar feed)"
UTC = timezone.utc
log = logging.getLogger("sportcal")


# ----------------------------------------------------------------------------
# Hilfsfunktionen
# ----------------------------------------------------------------------------
def sha(*parts: str, n: int = 16) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:n]


def http_get(url: str, *, headers=None, params=None, retries: int = 3) -> requests.Response:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA, **(headers or {})},
                             params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url} fehlgeschlagen: {last}")


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def make_event(*, uid, calendar, source, title, start, end, all_day=False,
               location="", description="", url="", status="CONFIRMED") -> dict:
    """start/end: datetime (aware) oder date (all_day, end exklusiv)."""
    if all_day:
        s, e = start.isoformat(), end.isoformat()
    else:
        s, e = iso_utc(start), iso_utc(end)
    return {"uid": uid, "calendar": calendar, "source": source, "title": title,
            "start": s, "end": e, "all_day": all_day, "location": location,
            "description": description, "url": url, "status": status}


def fingerprint(e: dict) -> str:
    keys = ("title", "start", "end", "all_day", "location", "description", "url", "status")
    return sha(*[str(e.get(k, "")) for k in keys], n=20)


def is_relevant(e: dict, today_start: datetime, tz: ZoneInfo) -> bool:
    """Nur Termine ab heute (laufende mehrtägige Events zählen mit)."""
    if e["all_day"]:
        return date.fromisoformat(e["end"]) > today_start.astimezone(tz).date()
    return parse_iso_utc(e["end"]) > today_start


def is_future(e: dict, now: datetime, tz: ZoneInfo) -> bool:
    """Noch nicht begonnen -> darf bei Verschwinden aus der Quelle entfernt werden."""
    if e["all_day"]:
        return date.fromisoformat(e["start"]) > now.astimezone(tz).date()
    return parse_iso_utc(e["start"]) > now


# ----------------------------------------------------------------------------
# Quelle 1: vorhandener ICS-Feed (z.B. FC Bayern)
# ----------------------------------------------------------------------------
def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_unescape(v: str) -> str:
    return (v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
            .replace("\\;", ";").replace("\\\\", "\\"))


def _parse_ics_dt(value: str, params: dict, tz: ZoneInfo):
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date()
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    zone = tz
    if "TZID" in params:
        try:
            zone = ZoneInfo(params["TZID"])
        except Exception:  # noqa: BLE001
            zone = tz
    return naive.replace(tzinfo=zone)


def parse_ics(text: str, tz: ZoneInfo) -> list[dict]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *plist = head.split(";")
            params = dict(p.split("=", 1) for p in plist if "=" in p)
            cur[name.upper()] = (value, params)
    out = []
    for c in events:
        if "DTSTART" not in c:
            continue
        if "RRULE" in c:
            log.warning("RRULE in ICS-Feed wird nicht expandiert (Termin: %s)", c.get("SUMMARY", ("?",))[0])
        start = _parse_ics_dt(*c["DTSTART"], tz)
        if "DTEND" in c:
            end = _parse_ics_dt(*c["DTEND"], tz)
        elif "DURATION" in c:
            m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", c["DURATION"][0])
            end = start + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0)) if m else None
        else:
            end = None
        if end is None:
            end = start + (timedelta(days=1) if isinstance(start, date) and not isinstance(start, datetime)
                           else timedelta(hours=2))
        g = lambda k: _ics_unescape(c[k][0]) if k in c else ""  # noqa: E731
        out.append({"uid": g("UID"), "summary": g("SUMMARY"), "start": start, "end": end,
                    "location": g("LOCATION"), "description": g("DESCRIPTION"),
                    "url": g("URL"), "status": g("STATUS") or "CONFIRMED"})
    return out


def fetch_ics(src: dict, cal: dict, ctx: dict) -> list[dict]:
    url = src.get("url") or os.environ.get(src.get("url_env", ""), "")
    if not url:
        raise RuntimeError(f"Keine URL: Secret/Env '{src.get('url_env')}' nicht gesetzt")
    url = re.sub(r"^webcal(s?)://", lambda m: "https://", url.strip())
    text = http_get(url).text
    inc = re.compile(src["include_regex"]) if src.get("include_regex") else None
    exc = re.compile(src["exclude_regex"]) if src.get("exclude_regex") else None
    result = []
    for ev in parse_ics(text, ctx["tz"]):
        if inc and not inc.search(ev["summary"]):
            continue
        if exc and exc.search(ev["summary"]):
            continue
        all_day = isinstance(ev["start"], date) and not isinstance(ev["start"], datetime)
        result.append(make_event(
            uid=f"{src['id']}-{sha(ev['uid'] or ev['summary'] + str(ev['start']))}@sportcal",
            calendar=cal["id"], source=src["id"], title=f"{cal['emoji']} {ev['summary']}",
            start=ev["start"], end=ev["end"], all_day=all_day, location=ev["location"],
            description=ev["description"], url=ev["url"],
            status="CANCELLED" if ev["status"].upper() == "CANCELLED" else "CONFIRMED"))
    return result


# ----------------------------------------------------------------------------
# Quelle 2: DFB-Spielplan-Tabelle (Datum | Uhrzeit | Veranstaltung | Ort | TV)
# ----------------------------------------------------------------------------
COMP = {"LSP": "Länderspiel", "EMQ": "EM-Quali", "WMQ": "WM-Quali", "UNL": "Nations League",
        "UWNL": "Women's Nations League", "NL": "Nations League", "OLY": "Olympia"}


def parse_de_date_range(s: str):
    """'09.10.2026' | '24.06. – 25.07.2027' | '17. – 20.08.2026' -> (start, end_inclusive) oder None"""
    s = re.sub(r"\s+", " ", s.replace("–", "-").replace("—", "-")).strip()
    parts = [p.strip() for p in s.split("-")]
    m_end = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", parts[-1])
    if not m_end:
        return None
    d2, mo2, y2 = map(int, m_end.groups())
    end = date(y2, mo2, d2)
    if len(parts) == 1:
        return end, end
    m = re.fullmatch(r"(\d{1,2})\.(?:(\d{1,2})\.)?(?:(\d{4}))?", parts[0])
    if not m:
        return None
    d1 = int(m.group(1))
    mo1 = int(m.group(2) or mo2)
    y1 = int(m.group(3) or y2)
    return date(y1, mo1, d1), end


def fetch_dfb_table(src: dict, cal: dict, ctx: dict) -> list[dict]:
    from bs4 import BeautifulSoup
    tz = ctx["tz"]
    soup = BeautifulSoup(http_get(src["url"]).text, "html.parser")
    inc = re.compile(src["include_regex"]) if src.get("include_regex") else None
    dur = timedelta(minutes=int(src.get("duration_minutes", 120)))
    found_table, result = False, []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if "datum" not in headers:
            continue
        found_table = True
        idx = {h: i for i, h in enumerate(headers)}
        for tr in table.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 3:
                continue
            cell = lambda key: tds[idx[key]] if key in idx and idx[key] < len(tds) else ""  # noqa: E731
            rng = parse_de_date_range(cell("datum"))
            title_raw = cell("veranstaltung")
            if not rng or not title_raw or (inc and not inc.search(title_raw)):
                continue
            d_start, d_end = rng
            ort, tv = cell("ort"), cell("tv")
            is_match = bool(re.search(r"\S\s[-–]\s\S", title_raw))
            m_comp = re.search(r"\(([A-Za-z]+)\)\s*$", title_raw)
            comp = COMP.get(m_comp.group(1), m_comp.group(1)) if m_comp else ""
            nice = re.sub(r"\s[-–]\s", " – ", re.sub(r"\s*\([A-Za-z]+\)\s*$", "", title_raw))
            title = f"{cal['emoji']} {nice}" + (f" ({comp})" if comp else "")
            desc = "\n".join(x for x in (f"TV: {tv}" if tv else "", "Quelle: " + src["url"]) if x)
            if d_start != d_end or not is_match:
                if not src.get("include_ranges"):
                    continue
                result.append(make_event(
                    uid=f"{src['id']}-{sha(str(d_start), title_raw)}@sportcal", calendar=cal["id"],
                    source=src["id"], title=title, start=d_start, end=d_end + timedelta(days=1),
                    all_day=True, location=ort, description=desc, url=src["url"]))
                continue
            tm = re.search(r"(\d{1,2})[.:](\d{2})", cell("uhrzeit"))
            # UID nur am Datum festgemacht: Uhrzeit-/Gegner-Updates bleiben derselbe Termin
            uid = f"{src['id']}-{sha(str(d_start))}@sportcal"
            if tm:
                start = datetime(d_start.year, d_start.month, d_start.day,
                                 int(tm.group(1)), int(tm.group(2)), tzinfo=tz)
                result.append(make_event(uid=uid, calendar=cal["id"], source=src["id"], title=title,
                                         start=start, end=start + dur, location=ort,
                                         description=desc, url=src["url"]))
            else:  # Uhrzeit noch offen -> Ganztages-Termin, wird später automatisch zum Zeittermin
                result.append(make_event(uid=uid, calendar=cal["id"], source=src["id"],
                                         title=title + " – Uhrzeit offen", start=d_start,
                                         end=d_start + timedelta(days=1), all_day=True,
                                         location=ort, description=desc, url=src["url"]))
    if not found_table:
        raise RuntimeError("Keine Tabelle mit Spalte 'Datum' gefunden – Seitenaufbau geändert oder URL falsch?")
    return result


# ----------------------------------------------------------------------------
# Quelle 3: PandaScore (CS2)  –  Gratis-Token: pandascore.co
# ----------------------------------------------------------------------------
PS_BASE = "https://api.pandascore.co/csgo"  # CS2 läuft bei PandaScore unter /csgo/
GAMES_TO_MIN = {1: 75, 2: 120, 3: 180, 5: 270}


def _ps_headers(src: dict) -> dict:
    token = os.environ.get(src.get("token_env", "PANDASCORE_TOKEN"), "")
    if not token:
        raise RuntimeError("PANDASCORE_TOKEN nicht gesetzt")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def ps_team_candidates(src: dict) -> list[dict]:
    r = http_get(f"{PS_BASE}/teams", headers=_ps_headers(src),
                 params={"search[name]": src["team_name"], "per_page": 50})
    return r.json()


def ps_resolve_team(src: dict) -> int:
    if src.get("team_id"):
        return int(src["team_id"])
    want = src["team_name"].strip().lower()
    cands = ps_team_candidates(src)
    exact = [t for t in cands if (t.get("name") or "").lower() == want
             or (t.get("acronym") or "").lower() == want]
    if len(exact) == 1:
        return exact[0]["id"]
    listing = ", ".join(f"{t['id']}={t.get('name')}" for t in (exact or cands)[:10])
    raise RuntimeError(f"Team '{src['team_name']}' nicht eindeutig. Setze team_id. Kandidaten: {listing}")


def fetch_pandascore(src: dict, cal: dict, ctx: dict) -> list[dict]:
    headers, team_id = _ps_headers(src), ps_resolve_team(src)
    matches: dict[int, dict] = {}
    for endpoint in ("upcoming", "running"):
        page = 1
        while page <= 5:
            r = http_get(f"{PS_BASE}/matches/{endpoint}", headers=headers, params={
                "filter[opponent_id]": team_id, "sort": "scheduled_at", "per_page": 100, "page": page})
            data = r.json()
            for m in data:
                matches[m["id"]] = m
            if len(data) < 100:
                break
            page += 1
    result = []
    for m in matches.values():
        when = m.get("scheduled_at") or m.get("begin_at")
        if not when or m.get("status") in ("canceled",):
            continue
        start = datetime.fromisoformat(when.replace("Z", "+00:00"))
        names = [(o.get("opponent") or {}).get("name", "TBD") for o in m.get("opponents", [])]
        while len(names) < 2:
            names.append("TBD")
        bo = m.get("number_of_games") or 1
        league = (m.get("league") or {}).get("name", "")
        serie = (m.get("serie") or {}).get("full_name", "")
        stage = (m.get("tournament") or {}).get("name", "")
        title = f"{cal['emoji']} {names[0]} vs {names[1]} (BO{bo})" + (f" – {league}" if league else "")
        stream = m.get("official_stream_url") or next(
            (s.get("raw_url") for s in m.get("streams_list", []) if s.get("main")), "") or ""
        desc = "\n".join(x for x in (serie, stage, f"Stream: {stream}" if stream else "") if x)
        result.append(make_event(
            uid=f"pandascore-{cal['id']}-{m['id']}@sportcal", calendar=cal["id"], source=src["id"], title=title,
            start=start, end=start + timedelta(minutes=GAMES_TO_MIN.get(bo, 150)),
            description=desc, url=stream))
    return result


# ----------------------------------------------------------------------------
# Quelle 4: statische / manuelle Termine aus der YAML
# ----------------------------------------------------------------------------
def fetch_static(src: dict, cal: dict, ctx: dict) -> list[dict]:
    tz, out = ctx["tz"], []
    for item in src.get("events") or []:
        start_s = str(item["start"])
        title = item["title"] if item["title"].startswith(cal["emoji"]) else f"{cal['emoji']} {item['title']}"
        common = dict(calendar=cal["id"], source=src["id"], title=title,
                      location=item.get("location", ""), description=item.get("description", ""),
                      url=item.get("url", ""))
        uid = f"{src['id']}-{sha(item['title'], start_s)}@sportcal"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_s):
            d1 = date.fromisoformat(start_s)
            d2 = date.fromisoformat(str(item.get("end", start_s)))
            out.append(make_event(uid=uid, all_day=True, start=d1, end=d2 + timedelta(days=1), **common))
        else:
            start = datetime.strptime(start_s, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            end = start + timedelta(minutes=int(item.get("duration_minutes", 120)))
            out.append(make_event(uid=uid, start=start, end=end, **common))
    return out


FETCHERS = {"ics": fetch_ics, "dfb_table": fetch_dfb_table,
            "pandascore": fetch_pandascore, "static": fetch_static}


# ----------------------------------------------------------------------------
# Merge: Vergangenes bleibt, Zukünftiges wird aktualisiert
# ----------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"events": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def merge(state: dict, source_id: str, fetched: list[dict], now: datetime, tz: ZoneInfo) -> dict:
    store = state["events"]
    today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {"new": 0, "updated": 0, "removed": 0, "unchanged": 0}
    seen = set()
    for e in fetched:
        seen.add(e["uid"])
        fp = fingerprint(e)
        old = store.get(e["uid"])
        if old is None:
            if not is_relevant(e, today_start, tz):
                continue  # "ab heute": neue Termine in der Vergangenheit ignorieren
            store[e["uid"]] = {**e, "fingerprint": fp, "sequence": 0, "last_modified": iso_utc(now)}
            stats["new"] += 1
        elif old["fingerprint"] != fp:
            if not is_future(old, now, tz) and not is_relevant(e, today_start, tz):
                stats["unchanged"] += 1  # Vergangenes nicht mehr umschreiben
                continue
            store[e["uid"]] = {**e, "fingerprint": fp, "sequence": old["sequence"] + 1,
                               "last_modified": iso_utc(now)}
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1
    # Zukünftige Termine, die die Quelle nicht mehr liefert (abgesagt/verschoben) entfernen.
    # Sicherung: leere Antwort gilt als Fehler -> nichts löschen.
    mine = [u for u, e in store.items() if e["source"] == source_id and is_future(e, now, tz)]
    if fetched:
        for uid in mine:
            if uid not in seen:
                del store[uid]
                stats["removed"] += 1
    elif mine:
        log.warning("%s: Quelle lieferte 0 Termine, %d künftige bleiben unangetastet", source_id, len(mine))
    return stats


# ----------------------------------------------------------------------------
# ICS-Ausgabe
# ----------------------------------------------------------------------------
def esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # nicht mitten in einem UTF-8-Zeichen trennen
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b[cut:]
        b = b" " + b
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def ics_dt(s: str, all_day: bool) -> str:
    if all_day:
        return ";VALUE=DATE:" + s.replace("-", "")
    return ":" + s.replace("-", "").replace(":", "")


def build_ics(cal: dict, events: list[dict], settings: dict) -> str:
    hours = int(settings.get("refresh_hours", 6))
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sport-kalender-agent//DE", "CALSCALE:GREGORIAN",
         f"X-WR-CALNAME:{esc(cal['name'])}", f"X-WR-TIMEZONE:{settings.get('timezone', 'Europe/Berlin')}",
         f"REFRESH-INTERVAL;VALUE=DURATION:PT{hours}H", f"X-PUBLISHED-TTL:PT{hours}H"]
    if cal.get("color"):
        L += [f"X-APPLE-CALENDAR-COLOR:{cal['color']}", f"COLOR:{cal['color']}"]
    for e in sorted(events, key=lambda x: (x["start"], x["uid"])):
        L += ["BEGIN:VEVENT", f"UID:{e['uid']}", f"DTSTAMP:{e['last_modified'].replace('-', '').replace(':', '')}",
              f"LAST-MODIFIED:{e['last_modified'].replace('-', '').replace(':', '')}",
              f"SEQUENCE:{e['sequence']}",
              "DTSTART" + ics_dt(e["start"], e["all_day"]), "DTEND" + ics_dt(e["end"], e["all_day"]),
              f"SUMMARY:{esc(e['title'])}", f"STATUS:{e['status']}",
              "TRANSP:TRANSPARENT" if e["all_day"] else "TRANSP:OPAQUE"]
        if e.get("location"):
            L.append(f"LOCATION:{esc(e['location'])}")
        if e.get("description"):
            L.append(f"DESCRIPTION:{esc(e['description'])}")
        if e.get("url"):
            L.append(f"URL:{e['url']}")
        mins = cal.get("alarm_minutes")
        if mins and not e["all_day"]:
            L += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(e['title'])}",
                  f"TRIGGER:-PT{int(mins)}M", "END:VALARM"]
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(fold(x) for x in L) + "\r\n"


def write_outputs(cfg: dict, state: dict) -> None:
    settings = cfg.get("settings", {})
    suffix = settings.get("secret_suffix", "")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    base = settings.get("base_url") or (
        f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}" if "/" in repo else "")
    all_events, rows, seen_keys = [], [], set()
    for cal in cfg["calendars"]:
        evs = [e for e in state["events"].values() if e["calendar"] == cal["id"]]
        for e in evs:  # all.ics: Duplikate (z.B. NAVI vs BIG in zwei Kalendern) nur einmal
            key = (e["title"], e["start"])
            if key not in seen_keys:
                seen_keys.add(key)
                all_events.append(e)
        fname = f"{cal['id']}{suffix}.ics"
        (OUT_DIR / fname).write_text(build_ics(cal, evs, settings), encoding="utf-8", newline="")
        rows.append((cal["name"], fname, len(evs)))
    all_cal = {"name": "Sport – alles", "id": "all", "color": "#0A84FF"}
    (OUT_DIR / f"all{suffix}.ics").write_text(build_ics(all_cal, all_events, settings), encoding="utf-8", newline="")
    rows.append(("Alles zusammen (nur für einmaligen Import)", f"all{suffix}.ics", len(all_events)))
    items = []
    for name, fname, n in rows:
        link = f"{base}/{fname}" if base else fname
        wc = link.replace("https://", "webcal://") if base else link
        items.append(f"<li><b>{html.escape(name)}</b> ({n} Termine) – "
                     f"<a href='{html.escape(wc)}'>abonnieren</a> · <a href='{html.escape(link)}'>.ics</a></li>")
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Sport-Kalender</title><body style='font-family:system-ui;max-width:40em;margin:2em auto;padding:0 1em'>"
        f"<h1>Sport-Kalender</h1><ul>{''.join(items)}</ul><small>Stand: {stamp}</small></body>",
        encoding="utf-8")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "watchlist.yaml"))
    ap.add_argument("--only", help="nur diese Kalender-ID")
    ap.add_argument("--dry-run", action="store_true", help="nichts schreiben")
    ap.add_argument("--resolve-teams", action="store_true", help="PandaScore-Team-IDs auflisten")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    tz = ZoneInfo(cfg.get("settings", {}).get("timezone", "Europe/Berlin"))
    ctx = {"tz": tz}

    if args.resolve_teams:
        for cal in cfg["calendars"]:
            for src in cal["sources"]:
                if src["type"] == "pandascore":
                    print(f"\n== {src['team_name']} ==")
                    for t in ps_team_candidates(src):
                        print(f"  id={t['id']:<8} name={t.get('name')!s:<28} acronym={t.get('acronym')}")
        return 0

    state, now, failures = load_state(), datetime.now(UTC), 0
    for cal in cfg["calendars"]:
        if args.only and cal["id"] != args.only:
            continue
        for src in cal["sources"]:
            try:
                fetched = FETCHERS[src["type"]](src, cal, ctx)
                stats = merge(state, src["id"], fetched, now, tz)
                log.info("%-24s geliefert=%-3d %s", src["id"], len(fetched), stats)
            except Exception as exc:  # noqa: BLE001  – eine kaputte Quelle stoppt nie die anderen
                failures += 1
                log.error("%s: %s", src["id"], exc)
                print(f"::warning title=Quelle {src['id']} fehlgeschlagen::{exc}")
    if not args.dry_run:
        save_state(state)
        write_outputs(cfg, state)
    return 0 if failures == 0 else 0  # Fehler nur als Warnung, damit Commit-Schritt trotzdem läuft


if __name__ == "__main__":
    sys.exit(main())
