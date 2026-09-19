#!/usr/bin/env python3
"""Sky River spell kit: cast a chart view on demand (ADR-0009, 2026-09-19).

The RAW is the truth: birth data in the people store, the anchors and the math in Sky River. A cast is a
disposable view rendered from the raw with SELECTIONS (render knobs) into one vault note under
Skyriver/Casts/ and one calendar event on the casts layer (3007-01-01). Overwritten on recast; recycled
overnight (--recycle) so loading a set of people into view is a deliberate act each session.

  cast.py <code-name> [--zodiac sidereal,tropical] [--ayanamsa skyriver|fagan_bradley|lahiri|snapped]
          [--houses placidus,whole_sign,whole_sign_sidereal,equal,koch] [--aspects majors|majors+quintiles|all]
          [--wheel tropical|sidereal] [--frame NAME] [--apply]
  cast.py --recycle [--apply]          # wipe every cast (vault + calendar); the raw is untouched
"""
import argparse, json, re, sys, urllib.request
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC)); sys.path.insert(0, str(CC / "tools")); sys.path.insert(0, str(Path.home() / "projects" / "tilde"))
import comms_poller as cp  # noqa: E402
import links  # noqa: E402
import mirror  # noqa: E402
import charts  # noqa: E402  (tilde/charts.py: the renderer)

CASTS_DAY = "3007-01-01"
CASTS_DIR = "Skyriver/Casts"
API = charts.API
MAJORS = charts.MAJOR_ASPECTS
FIFTH = {"quintile", "biquintile"}


def find_person(code: str) -> dict:
    """code-name = slug(full name)-YYYYMMDD; the people store is keyed by slug alone."""
    m = re.match(r"^(.*)-(\d{4})(\d{2})(\d{2})$", code)
    if not m:
        sys.exit(f"not a code-name: {code}")
    slug, y, mo, d = m.groups()
    for p in sorted(charts.PEOPLE.glob(f"{slug}*.json")):
        rec = json.loads(p.read_text())
        b = rec["birth"]
        if (b["year"], b["month"], b["day"]) == (int(y), int(mo), int(d)) and charts.slugify(rec.get("name", slug)) in (slug, p.stem.rsplit("-", 1)[0], p.stem):
            return rec
    sys.exit(f"no person in the raw store for {code}")


def frame_name(a) -> str:
    z = "+".join(a.zodiac.split(","))
    return a.frame or f"{z} {a.ayanamsa} · {a.houses.replace(',', '+')} · {a.aspects}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="?")
    ap.add_argument("--zodiac", default="sidereal,tropical")
    ap.add_argument("--ayanamsa", default="skyriver", help="skyriver | fagan_bradley | lahiri | supersidereal")
    ap.add_argument("--houses", default="placidus,whole_sign,whole_sign_sidereal,equal,koch")
    ap.add_argument("--aspects", default="all", choices=["majors", "majors+quintiles", "all"])
    ap.add_argument("--wheel", default="tropical", choices=["tropical", "sidereal"])
    ap.add_argument("--frame")
    ap.add_argument("--keep", action="store_true", help="a named frame kept as a standing habit: survives the nightly recycle")
    ap.add_argument("--recycle", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    dry = not a.apply; tag = "[dry] " if dry else ""
    cfg = cp.load_config(Path(a.config)); svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    vault = Path(cfg["mirror_dir"]).expanduser()

    if a.recycle:
        events = mirror.list_future(svc, cal)
        allc = [e for e in events if e.get("extendedProperties", {}).get("private", {}).get("comms_kind") == "cast"]
        kept = [e for e in allc if e.get("extendedProperties", {}).get("private", {}).get("cast_keep")]
        casts = [e for e in allc if e not in kept]
        kept_keys = {links.key_of(e.get("description") or "") for e in kept}
        files = [f for f in (sorted((vault / CASTS_DIR).glob("*.md")) if (vault / CASTS_DIR).is_dir() else [])
                 if links.key_of(mirror.canonical(f.read_text(encoding="utf-8", errors="replace"))) not in kept_keys]
        for e in kept:
            print(f"{tag}kept    {e['summary']} (named frame)")
        for e in casts:
            print(f"{tag}recycle {e['summary']}")
            if not dry:
                cp.pace(); svc.events().delete(calendarId=cal, eventId=e["id"]).execute()
        for f in files:
            print(f"{tag}recycle {f.relative_to(vault)}")
            if not dry:
                f.unlink()
        print(f"{tag}recycled {len(casts)} cast events and {len(files)} cast notes; the raw is untouched")
        return
    if not a.code:
        sys.exit("give a code-name, or --recycle")

    rec = find_person(a.code)
    birth = {k: rec["birth"][k] for k in ("year", "month", "day", "hour", "minute", "place") if k in rec["birth"]}
    given = re.split(r"[\s-]+", rec.get("name", a.code).strip())[0].title()   # the store's name may be a slug
    houses = [h for h in a.houses.split(",") if h]
    primary = "whole_sign" if "placidus" not in houses and "whole_sign" in houses else ("placidus" if "placidus" in houses else houses[0])
    hsys = {"whole_sign": "whole_sign"}.get(primary, "placidus")
    astro = charts.sky_post("/sky/astrology", birth, houses=hsys, ayanamsa=a.ayanamsa)
    if astro is None:
        sys.exit("astrology lookup failed")
    req = urllib.request.Request(f"{API}/sky/render/wheel-data",
                                 data=json.dumps({"birth": birth, "palette": "native", "houses": hsys, "ayanamsa": a.ayanamsa,
                                                  "zodiac": a.wheel}).encode(),
                                 headers={"Authorization": f"Bearer {charts.API_TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        block = json.dumps(json.loads(resp.read()), separators=(",", ":"))
    b = rec["birth"]
    birth_str = f"{b['day']} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][b['month'] - 1]} {b['year']}, {b['hour']:02d}:{b['minute']:02d}, {b.get('place', '')}"
    want = None if a.aspects == "all" else (MAJORS | FIFTH if a.aspects == "majors+quintiles" else MAJORS)
    frame = frame_name(a)
    sel = {"zodiacs": a.zodiac.split(","), "houses": houses, "aspects": want, "frame": frame, "keep": a.keep}
    text = charts.cast_note(given, birth_str, astro.get("chart", {}), block, sel)

    title = f"Note: Cast · {a.code} · {frame}"
    rel = f"{CASTS_DIR}/{mirror.file_name(given + ' · ' + frame)}"
    events = mirror.list_future(svc, cal)
    def is_this_cast(e):
        pv = e.get("extendedProperties", {}).get("private", {})
        return pv.get("comms_kind") == "cast" and pv.get("cast_code") == a.code and pv.get("cast_frame") == frame
    group = sorted((e for e in events if is_this_cast(e)), key=lambda e: int(e.get("extendedProperties", {}).get("private", {}).get("mirror_part") or 1))
    cur = group[0] if group else None
    key = links.key_of(cur.get("description") or "") if cur else None
    if not key:
        taken = {t for e in events for t in (e.get("location") or "").split() if links.is_key(t)}
        key = links.mint(taken)
    body = text.rstrip() + "\n\n---\n" + key + "\n"
    canon = mirror.canonical(body)
    parts, _mode = mirror.split_parts(canon)
    print(f"{tag}cast    {title} -> {rel} ({len(canon)} chars, {len(parts)} part{'s' if len(parts) > 1 else ''})")
    if dry:
        print(text[:1200]); return
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    priv = {"comms_kind": "cast", "comms_writer": "cast", "mirror_path": rel, "mirror_hash": mirror.h(canon), "cast_code": a.code, "cast_frame": frame,
            "cast_keep": "1" if a.keep else ""}
    for stale in (vault / CASTS_DIR).glob("*.md"):      # an earlier render of this cast under another file name
        if stale != path and links.key_of(mirror.canonical(stale.read_text(encoding="utf-8", errors="replace"))) == key:
            stale.unlink()
    if cur:
        old_rel = cur.get("extendedProperties", {}).get("private", {}).get("mirror_path")
        if old_rel and old_rel != rel and (vault / old_rel).exists():
            (vault / old_rel).unlink()
        mirror.write_event(svc, cal, group, canon, False)
        for e in group:
            cp.write_event(svc, cal, e["id"], {"extendedProperties": {"private": priv}}, existing=e, verify=False)
    else:
        ev = cp.insert_event(svc, cal, {"summary": title, "start": {"date": CASTS_DAY}, "end": {"date": "3007-01-02"}, "location": key,
                                        "description": "(casting)", "extendedProperties": {"private": priv}})
        mirror.write_event(svc, cal, [ev], canon, False)
    print(f"cast landed: {rel} and {title} on {CASTS_DAY}")


if __name__ == "__main__":
    main()
