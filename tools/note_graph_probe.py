"""Phase-one capacity probe (2026-09-08): land a customer → orders → category note graph on the
Woolly calendar as passive context notes (anchor date, all-day, `Note:` titles).

  python tools/note_graph_probe.py --customer 80            # print notes + sizes
  python tools/note_graph_probe.py --customer 80 --apply    # insert (or replace) on the calendar

Grain per the accepted design: SKU × month, additive facts only (units, revenue); static
multipliers once per SKU; category total as first row; rows sorted by 12-month units.
"""
import argparse, asyncio, csv, sys, tomllib
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import asyncpg

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import comms_poller as cp  # noqa: E402

DSN = "postgresql://USER:PASSWORD@localhost:5432/DATABASE"
PACK_INFO = Path.home() / "woolly-workplace/data/sy_pack_info.csv"
ANCHOR = "2000-01-01"
CAP = 7400  # Google description cap is ~8k chars; leave room for the part line


def months_back(n: int) -> list[str]:
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def fmt(x) -> str:
    x = float(x or 0)
    return str(int(x)) if x == int(x) else f"{x:.1f}"


async def load(customer_id: int):
    c = await asyncpg.connect(DSN)
    cust = await c.fetchrow("select id, tags, created_at from customers where id=$1", customer_id)
    orders = await c.fetch("""
        select o.id, o.order_name, o.created_at::date d, o.total
        from shopify_orders o where o.customer_id=$1 and o.cancelled_at is null and o.created_at >= now()-interval '24 months' order by o.created_at""", customer_id)
    lines = await c.fetch("""
        select l.order_id, l.matched_sku sku, p.name, p.category,
               coalesce(a.adjusted_quantity, l.quantity) qty,
               coalesce(a.adjusted_price, l.price) price
        from shopify_order_lines l join shopify_orders o on o.id=l.order_id
        join products p on p.sku=l.matched_sku
        left join shopify_order_line_adjustments a on a.line_id=l.id
        where o.customer_id=$1 and o.cancelled_at is null and o.created_at >= now()-interval '24 months' and coalesce(a.adjusted_quantity, l.quantity) > 0
        order by l.order_id, p.category, p.name""", customer_id)
    cats = sorted({r["category"] for r in lines})
    prods = await c.fetch("""
        select p.sku, p.name, p.category, p.current_inventory stock,
          (select unit_cost_aud from product_cost_history h where h.sku=p.sku and h.effective_from<=current_date
             order by effective_from desc limit 1) cost,
          coalesce((select sum(pl.quantity_ordered - coalesce(pl.quantity_received,0)) from purchase_order_lines pl
             join purchase_orders po on po.id=pl.purchase_order_id
             where pl.product_id=p.id and po.status='sent'),0) inflight
        from products p where p.category = any($1::text[])
          and (p.status='active' or p.sku in (select matched_sku from shopify_order_lines l join shopify_orders o on o.id=l.order_id
                                              where o.created_at>=now()-interval '12 months'))""", cats)
    sales = await c.fetch("""
        select l.matched_sku sku, to_char(o.created_at at time zone 'Australia/Melbourne','YYYY-MM') ym,
               sum(coalesce(a.adjusted_quantity,l.quantity)) u,
               sum(coalesce(a.adjusted_quantity,l.quantity)*coalesce(a.adjusted_price,l.price)) r
        from shopify_order_lines l join shopify_orders o on o.id=l.order_id
        join products p on p.sku=l.matched_sku
        left join shopify_order_line_adjustments a on a.line_id=l.id
        where p.category = any($1::text[]) and o.cancelled_at is null and o.created_at>=date_trunc('year', now()-interval '2 years')
        group by 1,2""", cats)
    await c.close()
    return cust, orders, lines, prods, sales


def pack_info():
    out = {}
    with open(PACK_INFO, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            units = float(row["Unit / Pack"] or 1)
            out[row["Category"]] = (float(row["Volume"]) / units, float(row["Weight kgs"]) * 1000 / units)
    return out


def colour(name: str, cat: str) -> str:
    n = name.strip()
    if n.lower().endswith(cat.lower()):
        n = n[: -len(cat)].strip()
    return n.removeprefix("CK ").strip() or name


def build(customer_id: int):
    cust, orders, lines, prods, sales = asyncio.run(load(customer_id))
    ms = months_back(12)
    mtd = ms[0]
    y1, y2 = str(date.today().year - 1), str(date.today().year - 2)
    pi = pack_info()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    by_sku = defaultdict(dict)
    for s in sales:
        by_sku[s["sku"]][s["ym"]] = (float(s["u"]), float(s["r"]))
    notes = {}

    # --- category notes
    cat_titles = {}
    for cat in sorted({p["category"] for p in prods}):
        vol, wt = pi.get(cat, (None, None))
        rows = []
        for p in prods:
            if p["category"] != cat:
                continue
            sm = by_sku.get(p["sku"], {})
            u12 = [sm.get(m, (0, 0))[0] for m in ms]
            r12 = [sm.get(m, (0, 0))[1] for m in ms]
            yu = lambda y: sum(v[0] for k, v in sm.items() if k.startswith(y))
            yr = lambda y: sum(v[1] for k, v in sm.items() if k.startswith(y))
            rows.append((sum(u12), p["sku"], colour(p["name"], cat), p["cost"], p["stock"], p["inflight"],
                         u12, r12, yu(y1), yr(y1), yu(y2), yr(y2)))
        rows.sort(key=lambda r: (-r[0], r[1]))
        def line(sku, col, cost, stock, infl, u12, r12, u1, r1, u2, r2):
            return "|".join([sku, col, fmt(cost), fmt(stock), fmt(infl),
                             ",".join(fmt(x) for x in u12), ",".join(fmt(x) for x in r12),
                             f"{fmt(u1)},{fmt(r1)}", f"{fmt(u2)},{fmt(r2)}"])
        tot = line("TOTAL", cat, 0, sum(r[4] or 0 for r in rows), sum(r[5] or 0 for r in rows),
                   [sum(r[6][i] for r in rows) for i in range(12)], [sum(r[7][i] for r in rows) for i in range(12)],
                   sum(r[8] for r in rows), sum(r[9] for r in rows), sum(r[10] for r in rows), sum(r[11] for r in rows))
        header = (f"Category {cat}. Sales by SKU by month, AUD ex adjustments; data {stamp}.\n"
                  f"Per unit: volume {fmt(vol) if vol else '?'} L, weight {fmt(wt) if wt else '?'} g (same for every SKU in this category).\n"
                  f"Columns: sku|colour|cost_per_unit|stock|inflight|units {ms[0]} first, back to {ms[-1]} ({mtd} is month to date)|revenue same months|units,revenue {y1}|units,revenue {y2}\n"
                  f"First row is the category total. Rows sorted by 12-month units.\n")
        body_rows = [tot] + [line(*r[1:]) for r in rows]
        # split alphabetically-by-order if over the cap
        chunks, cur = [], []
        for br in body_rows:
            if len(header) + sum(len(x) + 1 for x in cur) + len(br) + 1 > CAP and cur:
                chunks.append(cur); cur = []
            cur.append(br)
        chunks.append(cur)
        for i, ch in enumerate(chunks):
            title = f"Note: Category {cat}" + (f" part {i+1} of {len(chunks)}" if len(chunks) > 1 else "")
            part = f"Part {i+1} of {len(chunks)}; the total row is in part 1.\n" if len(chunks) > 1 else ""
            notes[title] = header + part + "\n".join(ch)
        cat_titles[cat] = f"Note: Category {cat}" + (f" (parts 1–{len(chunks)})" if len(chunks) > 1 else "")

    # --- order notes
    code = f"C{customer_id}"
    order_titles = []
    roll = defaultdict(float); cat_units = defaultdict(float); spend = []
    for o in orders:
        ol = [l for l in lines if l["order_id"] == o["id"]]
        if not ol:
            continue
        title = f"Note: Order {o['order_name']}"
        order_titles.append((title, o["d"], sum(float(l["qty"] * l["price"]) for l in ol)))
        rows = []
        for l in ol:
            rev = float(l["qty"] * l["price"])
            rows.append("|".join([l["sku"], colour(l["name"], l["category"]), l["category"], fmt(l["qty"]), fmt(rev)]))
            vol, wt = pi.get(l["category"], (0, 0))
            roll["units"] += float(l["qty"]); roll["rev"] += rev
            roll["vol"] += float(l["qty"]) * vol; roll["wt"] += float(l["qty"]) * wt
            cat_units[l["category"]] += float(l["qty"])
        spend.append((o["d"], sum(float(l["qty"] * l["price"]) for l in ol)))
        notes[title] = (f"Order {o['order_name']} for customer {code}, placed {o['d']}, {len(ol)} lines, "
                        f"{fmt(sum(float(l['qty']) for l in ol))} units, AUD {fmt(sum(float(l['qty']*l['price']) for l in ol))}.\n"
                        f"Columns: sku|colour|category|qty|line_revenue. Category figures: see {', '.join(cat_titles[c] for c in sorted({l['category'] for l in ol}))}.\n"
                        + "\n".join(rows))

    # --- customer note
    fav = max(cat_units, key=cat_units.get)
    title = f"Note: Customer {code}"
    notes[title] = (f"Customer {code}: wholesale stockist (tags: {cust['tags']}), first order in this window {order_titles[0][1]}, "
                    f"{len(order_titles)} orders. Identity kept off this calendar; ask Woolly.\n"
                    f"Orders (load by exact title):\n" + "\n".join(f"- {t} ({d}, AUD {fmt(a)})" for t, d, a in order_titles)
                    + "\nRoll-up (pre-composed, compare against walking the graph): "
                    f"units {fmt(roll['units'])}, revenue AUD {fmt(roll['rev'])}, volume {fmt(roll['vol'])} L, weight {fmt(roll['wt']/1000)} kg, "
                    f"favourite category {fav} ({fmt(cat_units[fav])} units).\n"
                    f"Categories bought: {', '.join(cat_titles[c] for c in sorted(cat_units))}.")
    return notes


def apply(notes: dict):
    cfg = cp.load_config(HERE / "comms.toml")
    svc = cp.get_service(cfg)
    existing = {}
    page = None
    while True:
        resp = svc.events().list(calendarId=cfg["calendar_id"], timeMin=f"{ANCHOR}T00:00:00Z", timeMax="2000-01-03T00:00:00Z",
                                 singleEvents=True, pageToken=page, maxResults=250).execute()
        for ev in resp.get("items", []):
            existing[ev.get("summary")] = ev["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    for title, body in notes.items():
        ev = {"summary": title, "description": body, "start": {"date": ANCHOR}, "end": {"date": "2000-01-02"},
              "reminders": {"useDefault": False}, "transparency": "transparent",
              "extendedProperties": {"private": {"comms_kind": "note", "comms_writer": "note_graph_probe"}}}
        if title in existing:
            svc.events().update(calendarId=cfg["calendar_id"], eventId=existing[title], body=ev).execute()
            print("updated ", title)
        else:
            svc.events().insert(calendarId=cfg["calendar_id"], body=ev).execute()
            print("inserted", title)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    notes = build(a.customer)
    for t, b in notes.items():
        print(f"{len(b):5d} chars  {t}")
    if a.apply:
        apply(notes)
    else:
        print("\n" + "\n\n=====\n".join(f"{t}\n{b}" for t, b in notes.items()))
