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

CASTS_DAY = "3007-01-01"     # default; comms.toml [layers] casts = <year> overrides (the band move derives every year from config)


def casts_day(cfg) -> str:
    y = (cfg.get("layers") or {}).get("casts")
    return f"{y}-01-01" if y else CASTS_DAY
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
    return a.frame or f"{z} {a.ayanamsa} · {a.carving} · {a.houses.replace(',', '+')} · {a.aspects}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


ASPECT_LABEL = {"conjunction": "☌", "opposition": "☍", "trine": "△", "square": "□", "sextile": "⚹"}


class _AspectTables:
    """The same tables as skyriver.adapters.astrology (kept in step by hand: the relay venv has no ephemeris)."""
    ASPECTS = {"conjunction": 0.0, "sextile": 60.0, "quintile": 72.0, "square": 90.0, "trine": 120.0, "biquintile": 144.0, "opposition": 180.0}
    DEFAULT_ORBS = {"conjunction": 8.0, "sextile": 4.0, "quintile": 2.0, "square": 6.0, "trine": 6.0, "biquintile": 2.0, "opposition": 8.0}
    HARMONIC_NAMES = {2: ["opposition"], 3: ["trine"], 4: ["square"], 5: ["quintile", "biquintile"], 6: ["sextile"],
                      7: ["septile", "biseptile", "triseptile"], 8: ["semisquare", "sesquisquare"],
                      9: ["novile", "binovile", "quadnovile"], 10: ["decile", "tridecile"],
                      11: ["undecile", "biundecile", "triundecile", "quadundecile", "quinundecile"], 12: ["semisextile", "quincunx"]}
    HARMONIC_ORBS = {2: 8.0, 3: 6.0, 4: 6.0, 5: 2.0, 6: 4.0, 7: 1.5, 8: 2.0, 9: 1.5, 10: 1.5, 11: 1.0, 12: 2.0}

    def harmonic_aspects(self, max_h=12):
        from math import gcd
        angles, orbs, harm = {"conjunction": 0.0}, {"conjunction": 8.0}, {"conjunction": 1}
        for n in range(2, max_h + 1):
            names = self.HARMONIC_NAMES.get(n, [f"{n}th"]); idx = 0
            for k in range(1, n // 2 + 1):
                if gcd(k, n) != 1:
                    continue
                name = names[idx] if idx < len(names) else f"{k}/{n}"; idx += 1
                angles[name] = 360.0 * k / n; orbs[name] = self.HARMONIC_ORBS.get(n, 1.0); harm[name] = n
        return angles, orbs, harm


def birth_of(rec):
    b = rec["birth"]
    return ({k: b[k] for k in ("year", "month", "day", "hour", "minute", "place") if k in b},
            f"{b['day']} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][b['month'] - 1]} {b['year']}, {b['hour']:02d}:{b['minute']:02d}, {b.get('place', '')}")


def synastry(a, svc, cal, vault, dry, tag):
    """Cross-aspects between two charts over the chosen aspect set, ranked by orb; plus a focus list for
    one body of A if --frame names it as 'focus:<Body>' is not needed: the Uranus focus is always shown when present."""
    astro_mod = _AspectTables()
    recs = [find_person(a.code), find_person(a.with_code)]
    givens = [re.split(r"[\s-]+", r.get("name", c).strip())[0].title() for r, c in zip(recs, (a.code, a.with_code))]
    zodiac = "tropical" if a.carving == "equal" else a.carving
    charts_ = []
    for rec in recs:
        birth, _ = birth_of(rec)
        r = charts.sky_post("/sky/astrology", birth, houses="whole_sign", ayanamsa=a.ayanamsa, zodiac=zodiac)
        if r is None:
            sys.exit("astrology lookup failed")
        charts_.append(r["chart"])
    frame_key = "placements_sidereal" if a.zodiac.split(",")[0] == "sidereal" else "placements"
    if a.aspects == "harmonic12":
        table, orbs, harm = astro_mod.harmonic_aspects(12)
    else:
        table, orbs, harm = dict(astro_mod.ASPECTS), dict(astro_mod.DEFAULT_ORBS), {k: None for k in astro_mod.ASPECTS}
        if a.aspects == "majors":
            table = {k: v for k, v in table.items() if k in MAJORS | {"conjunction"}}
    pa, pb = charts_[0][frame_key], charts_[1][frame_key]
    rows = []
    for x, px in pa.items():
        for y, py in pb.items():
            sep = abs((px["lon"] - py["lon"] + 180) % 360 - 180)
            for name, angle in sorted(table.items(), key=lambda kv: kv[1]):
                d = abs(sep - angle)
                if d <= orbs[name]:
                    rows.append((round(d, 2), x, name, y, harm.get(name)))
                    break
    rows.sort()
    ayan = charts_[0].get("precession_lens", {}).get("ayanamsa", {})
    frame_txt = (f"sidereal ({charts.AYANAMSA_LABEL.get(ayan.get('name'), ayan.get('name', ''))} ayanamsa {charts.dms(ayan.get('degrees', 0))})"
                 if frame_key == "placements_sidereal" else "tropical")
    frame = a.frame or f"synastry · {a.aspects}"
    lines = [f"# {givens[0]} × {givens[1]} — Synastry · {frame}", "",
             f"**{givens[0]}:** {birth_of(recs[0])[1]}", f"**{givens[1]}:** {birth_of(recs[1])[1]}",
             f"**Frame:** {frame_txt}; signs {'equal' if a.carving == 'equal' else a.carving}; aspects {a.aspects}, cross-aspects only, ranked by orb, tightest first.",
             "**Orbs:** " + ", ".join(f"h{h} {o:g}°" for h, o in sorted(astro_mod.HARMONIC_ORBS.items())) + " (conjunction 8°)" if a.aspects == "harmonic12"
             else "**Orbs:** " + ", ".join(f"{k} {v:g}°" for k, v in orbs.items()),
             "**Cast:** " + frame + (" — kept frame." if a.keep else " — rendered on demand; recycled overnight, cast again to see it."), ""]

    def lab(name):
        return f"`glyph:{name}`" if name in ASPECT_LABEL else charts.ASPECT_TEXT.get(name, name)

    def body(n):
        return f"{charts.glyph(n)} {n.replace('_', ' ')}"
    lines += [f"## {givens[0]}'s Uranus → {givens[1]}", "", "| Aspect | | Orb |", "| --- | --- | --- |"]
    ura = [r for r in rows if r[1] == "Uranus"]
    lines += [f"| {lab(nm)} {nm.replace('_', ' ').title()}{' (h' + str(h) + ')' if h else ''} | {givens[0]} Uranus → {givens[1]} {body(y)} | {d:.2f}° |" for d, x, nm, y, h in ura] or ["(none within orb)"]
    lines += ["", PART_MARK, "", f"## Cross-aspects, {len(rows)} within orb", "", "| Orb | Aspect | " + givens[0] + " | " + givens[1] + " |", "| --- | --- | --- | --- |"]
    lines += [f"| {d:.2f}° | {lab(nm)} {nm.replace('_', ' ').title()}{' (h' + str(h) + ')' if h else ''} | {body(x)} | {body(y)} |" for d, x, nm, y, h in rows]
    text = "\n".join(lines)
    code2 = f"{a.code} × {a.with_code}"
    title = f"Note: Cast · synastry · {code2} · {frame}"
    rel = f"{CASTS_DIR}/{mirror.file_name(givens[0] + ' × ' + givens[1] + ' · ' + frame)}"
    return land(a, svc, cal, vault, dry, tag, text, title, rel, code2, frame)


PART_MARK = charts.PART_MARK


CFG = {}


def land(a, svc, cal, vault, dry, tag, text, title, rel, code, frame):
    cfg = CFG
    events = mirror.list_future(svc, cal)
    def is_this_cast(e):
        pv = e.get("extendedProperties", {}).get("private", {})
        return pv.get("comms_kind") == "cast" and pv.get("cast_code") == code and pv.get("cast_frame") == frame
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
        print(text[:1500]); return
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    priv = {"comms_kind": "cast", "comms_writer": "cast", "mirror_path": rel, "mirror_hash": mirror.h(canon), "cast_code": code, "cast_frame": frame,
            "cast_keep": "1" if a.keep else ""}
    if cur:
        old_rel = cur.get("extendedProperties", {}).get("private", {}).get("mirror_path")
        if old_rel and old_rel != rel and (vault / old_rel).exists():
            (vault / old_rel).unlink()
        mirror.write_event(svc, cal, group, canon, False)
        for e in group:
            cp.write_event(svc, cal, e["id"], {"extendedProperties": {"private": priv}}, existing=e, verify=False)
    else:
        cd = casts_day(cfg); ev = cp.insert_event(svc, cal, {"summary": title, "start": {"date": cd}, "end": {"date": cd[:-2] + "02"}, "location": key,
                                        "description": "(casting)", "extendedProperties": {"private": priv}})
        mirror.write_event(svc, cal, [ev], canon, False)
    print(f"cast landed: {rel} and {title} on {casts_day(cfg)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code", nargs="?")
    ap.add_argument("--zodiac", default="sidereal,tropical")
    ap.add_argument("--ayanamsa", default="skyriver", help="skyriver | fagan_bradley | lahiri | supersidereal")
    ap.add_argument("--houses", default="placidus,whole_sign,whole_sign_sidereal,equal,koch")
    ap.add_argument("--aspects", default="all", choices=["majors", "majors+quintiles", "all", "harmonic12"])
    ap.add_argument("--carving", default="equal", choices=["equal", "pythagorean", "duodene"],
                    help="how the circle is cut from the frame's zero (ADR-0010): equal 30°, 3-limit (apotome/limma), 5-limit (duodene)")
    ap.add_argument("--wheel", default="tropical", choices=["tropical", "sidereal"])
    ap.add_argument("--frame")
    ap.add_argument("--with", dest="with_code", help="synastry: a second code-name; renders the cross-aspect grid (their planets to each other's) instead of a solo chart")
    ap.add_argument("--keep", action="store_true", help="a named frame kept as a standing habit: survives the nightly recycle")
    ap.add_argument("--recycle", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--config", default=str(CC / "comms.toml"))
    a = ap.parse_args()
    dry = not a.apply; tag = "[dry] " if dry else ""
    cfg = cp.load_config(Path(a.config)); svc = cp.get_service(cfg); cal = cfg["calendar_id"]
    vault = Path(cfg["mirror_dir"]).expanduser()
    CFG.update(cfg)

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

    if a.with_code:
        return synastry(a, svc, cal, vault, dry, tag)
    rec = find_person(a.code)
    birth = {k: rec["birth"][k] for k in ("year", "month", "day", "hour", "minute", "place") if k in rec["birth"]}
    given = re.split(r"[\s-]+", rec.get("name", a.code).strip())[0].title()   # the store's name may be a slug
    houses = [h for h in a.houses.split(",") if h]
    primary = "whole_sign" if "placidus" not in houses and "whole_sign" in houses else ("placidus" if "placidus" in houses else houses[0])
    hsys = {"whole_sign": "whole_sign"}.get(primary, "placidus")
    zodiac = "tropical" if a.carving == "equal" else a.carving
    astro = charts.sky_post("/sky/astrology", birth, houses=hsys, ayanamsa=a.ayanamsa, zodiac=zodiac,
                            aspect_set="harmonic12" if a.aspects == "harmonic12" else "majors")
    if astro is None:
        sys.exit("astrology lookup failed")
    req = urllib.request.Request(f"{API}/sky/render/wheel-data",
                                 data=json.dumps({"birth": birth, "palette": "native", "houses": hsys, "ayanamsa": a.ayanamsa,
                                                  "zodiac": zodiac if a.carving != "equal" else a.wheel}).encode(),
                                 headers={"Authorization": f"Bearer {charts.API_TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        block = json.dumps(json.loads(resp.read()), separators=(",", ":"))
    b = rec["birth"]
    birth_str = f"{b['day']} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][b['month'] - 1]} {b['year']}, {b['hour']:02d}:{b['minute']:02d}, {b.get('place', '')}"
    want = None if a.aspects in ("all", "harmonic12") else (MAJORS | FIFTH if a.aspects == "majors+quintiles" else MAJORS)
    frame = frame_name(a)
    sel = {"zodiacs": a.zodiac.split(","), "houses": houses, "aspects": want, "frame": frame, "keep": a.keep, "carving": a.carving}
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
        cd = casts_day(cfg); ev = cp.insert_event(svc, cal, {"summary": title, "start": {"date": cd}, "end": {"date": cd[:-2] + "02"}, "location": key,
                                        "description": "(casting)", "extendedProperties": {"private": priv}})
        mirror.write_event(svc, cal, [ev], canon, False)
    print(f"cast landed: {rel} and {title} on {casts_day(cfg)}")


if __name__ == "__main__":
    main()
