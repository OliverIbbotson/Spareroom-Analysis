"""
Houseshare Heroes – daily SpareRoom market tracker.

Fetches the public search-result pages for each area (rooms offered and rooms
wanted), reads the summary data on each listing card, and upserts it into the
`market` schema in Neon. Only result pages are fetched; individual advert
pages are excluded by SpareRoom's robots.txt and are never requested.

Usage:
    python scrape.py                 # full run, writes to DATABASE_URL
    python scrape.py --dry-run --area Dudley --max-pages 2   # test, prints JSON
"""
import argparse, datetime as dt, html, json, os, re, sys, time
import urllib.robotparser
import requests

BASE = "https://www.spareroom.co.uk"
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2.5"))
MAX_PAGES = 250  # safety cap per area/ad type
CONTACT = os.environ.get("CONTACT_EMAIL", "")
USER_AGENT = f"HSH-MarketResearch/1.0 (Houseshare Heroes daily market research{'; ' + CONTACT if CONTACT else ''})"

# Display name -> SpareRoom search slug. Most slugs cover the whole postcode area
# (e.g. derby = all DE). Long Eaton (NG10) and Burton (DE13-15) come through the
# Nottingham and Derby searches and are split out by postcode in `market.towns`.
# London is deliberately excluded: it is larger than all other areas combined.
AREAS = {
    "Birmingham": "birmingham",
    "Manchester": "manchester",
    "Leeds": "leeds",
    "Liverpool": "liverpool",
    "Sheffield": "sheffield",
    "Bristol": "bristol",
    "Nottingham": "nottingham",
    "Leicester": "leicester",
    "Coventry": "coventry",
    "Derby": "derby",
    "Stoke-on-Trent": "stoke-on-trent",
    "Lincoln": "lincoln",
    "Dudley": "dudley",
    "Wolverhampton": "wolverhampton",
    "Walsall": "walsall",
    "Newcastle upon Tyne": "newcastle_upon_tyne",
    "Sunderland": "sunderland",
    "Middlesbrough": "middlesbrough",
    "Hull": "hull",
    "Bradford": "bradford",
    "Huddersfield": "huddersfield",
    "Wakefield": "wakefield",
    "York": "york",
    "Doncaster": "doncaster",
    "Preston": "preston",
    "Bolton": "bolton",
    "Blackpool": "blackpool",
    "Stockport": "stockport",
    "Oldham": "oldham",
    "Warrington": "warrington",
    "Chester": "chester",
    "Telford": "telford",
    "Worcester": "worcester",
    "Gloucester": "gloucester",
    "Northampton": "northampton",
    "Peterborough": "peterborough",
    "Milton Keynes": "milton_keynes",
    "Luton": "luton",
    "Reading": "reading",
    "Oxford": "oxford",
    "Cambridge": "cambridge",
    "Norwich": "norwich",
    "Ipswich": "ipswich",
    "Southampton": "southampton",
    "Portsmouth": "portsmouth",
    "Brighton": "brighton",
    "Exeter": "exeter",
    "Plymouth": "plymouth",
    "Cardiff": "cardiff",
    "Swansea": "swansea",
    "Newport": "newport",
    "Glasgow": "glasgow",
    "Edinburgh": "edinburgh",
}
AD_TYPES = {"offered": "flatshare", "wanted": "flatmate"}

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
robots = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")


class Blocked(Exception):
    pass


def fetch(url):
    if not robots.can_fetch(USER_AGENT, url):
        raise Blocked(f"robots.txt disallows {url}")
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30, allow_redirects=False)
        except requests.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (301, 302):
            return None  # paged past the end
        if r.status_code in (403, 429):
            # We stop rather than try to get around a block.
            raise Blocked(f"HTTP {r.status_code} on {url}")
        if r.status_code >= 500 and attempt < 2:
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.text
    return None


# ---------- parsing ----------

def _attrs(block):
    return {k: html.unescape(v) for k, v in re.findall(r'data-listing-([a-z-]+)="([^"]*)"', block)}


def _money(s):
    m = re.search(r"[\d,]+(?:\.\d+)?", s or "")
    return float(m.group().replace(",", "")) if m else None


def _pcm(amount, period):
    if amount is None:
        return None
    return round(amount * 52 / 12, 2) if period == "pw" else amount


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _text(block, cls):
    m = re.search(rf'class="{cls}"[^>]*>(.*?)</', block, re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else None


def _room(room_text):
    t = (room_text or "").lower()
    singles = sum(int(n or 1) for n in re.findall(r"(\d+)?\s*singles?\b", t))
    doubles = sum(int(n or 1) for n in re.findall(r"(\d+)?\s*doubles?\b", t))
    if "studio" in t:
        cat = "studio"
    elif "bed flat" in t or "bed house" in t:
        cat = "whole property"
    elif singles and doubles:
        cat = "mixed"
    elif doubles:
        cat = "double"
    elif singles:
        cat = "single"
    else:
        cat = None
    return cat, singles or None, doubles or None


EN_SUITE = re.compile(r"\ben[\s-]?suites?\b", re.I)
NO_EN_SUITE = re.compile(r"\b(no|not|without|non)[\s-]+en[\s-]?suites?\b", re.I)


STUDIO = re.compile(r"\bstudios?\b", re.I)


def _short_desc(block):
    m = re.search(r'class="listing-card__short_description[^"]*"[^>]*>(.*?)</', block, re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""


def _en_suite(block, attrs):
    """True if the advert's headline or short description mentions an en-suite.
    Only the flag is stored, not the advert text."""
    text = (attrs.get("title") or "") + " " + _short_desc(block)
    return bool(EN_SUITE.search(text)) and not NO_EN_SUITE.search(text)


def _agent_name(block, attrs):
    """Company name shown on agent adverts. Only stored for agents -
    private landlords' and flatmates' names are never collected."""
    if attrs.get("advertiser-role") != "agent":
        return None
    m = re.search(r'class="advertiser-info__name">\s*([^<]*?)\s*<', block)
    name = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""
    return name or None


def _avail_date(block):
    m = re.search(r"Available (\d{1,2})(?:st|nd|rd|th) (\w{3}) (\d{4})", block)
    if not m:
        return None
    try:
        return dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def parse_page(page_html, ad_type):
    out = []
    for block in page_html.split('<li class="listing-result"')[1:]:
        a = _attrs(block)
        lid = _int(a.get("id"))
        if not lid:
            continue
        rate = _money(a.get("ad-headline-rate"))
        period = a.get("ad-headline-rate-period") or None
        common = {
            "listing_id": lid,
            "headline_rate": rate,
            "headline_period": period,
            "available_now": a.get("available-now") == "1",
            "early_bird": bool(a.get("early-bird")),
            "brand": a.get("brand") or None,
            "days_old": _int(a.get("days-old")),
            "en_suite": _en_suite(block, a),
        }
        if ad_type == "offered":
            room_text = _text(block, "listing-card__room offered")
            cat, singles, doubles = _room(room_text)
            norm = _money(a.get("ad-rate-normalised"))
            norm_period = a.get("ad-rate-normalised-period") or period
            common.update({
                "postcode_district": a.get("postcode") or None,
                "neighbourhood": a.get("neighbourhood") or None,
                "property_type": a.get("property-type") or None,
                "rooms_in_property": _int(a.get("rooms-in-property")),
                "advertiser_role": a.get("advertiser-role") or None,
                "agent_name": _agent_name(block, a),
                "room_type_text": room_text,
                "room_category": cat,
                "studio": cat == "studio" or bool(STUDIO.search(a.get("title") or "")),
                "singles": singles,
                "doubles": doubles,
                "rate_pcm": _pcm(norm if norm is not None else rate, norm_period),
                "bills_included": "listing-card__bills-included" in block,
                "available_from": _avail_date(block),
                "photos": _int(a.get("ad-pics")),
                "has_video": a.get("ad-video") == "yes",
                "verified": a.get("ad-verified") == "yes",
            })
        else:
            # Aggregate-useful fields only: no titles, names, photos or free text.
            common.update({
                "budget_pcm": _pcm(rate, period),
                "rooms_sought": a.get("rooms-sought") or None,
                "number_of_rooms": _int(a.get("number-of-rooms")),
                "areas_wanted": a.get("example-matching-area") or None,
                "gender_occupation": a.get("gender-and-occupation") or None,
                "studio": bool(STUDIO.search((a.get("title") or "") + " " + _short_desc(block))),
            })
        out.append(common)
    return out


PAGE_CAP = 100      # SpareRoom stops paging after 100 pages (1,000 results)
SPLIT_AT = 950      # areas with more live rooms than this are read district by district


def scrape_path(path, ad_type, max_pages, first_html=None):
    """Page through one search (e.g. flatshare/derby or flatshare/b29)."""
    seen, pages = {}, 0
    for page in range(1, max_pages + 1):
        if page == 1 and first_html is not None:
            body = first_html
        else:
            body = fetch(f"{BASE}/{path}" + (f"/page{page}" if page > 1 else ""))
            time.sleep(DELAY_SECONDS)
        if body is None:
            break
        rows = parse_page(body, ad_type)
        new = [r for r in rows if r["listing_id"] not in seen]
        for r in rows:
            seen.setdefault(r["listing_id"], r)
        if not new:  # empty page, or only repeated featured ads left
            break
        pages += 1
    return seen, pages


def scrape(slug, ad_type, max_pages):
    """Returns (rows, pages, note)."""
    path = f"{AD_TYPES[ad_type]}/{slug}"
    first = fetch(f"{BASE}/{path}")
    time.sleep(DELAY_SECONDS)
    if first is None:
        return [], 0, None
    total = re.search(r"([\d,]+) rooms? to rent now", first)
    total = int(total.group(1).replace(",", "")) if total else 0
    letters = re.search(r"Showing ads from all ([A-Z]{1,2}) postcodes", first)

    if ad_type == "offered" and total >= SPLIT_AT and letters:
        # Too big for one search: read each postcode district separately
        # (e.g. B1 ... B99). District pages contain only that district.
        prefix = letters.group(1).lower()
        seen, pages = {}, 0
        for n in range(1, 100):
            rows, p = scrape_path(f"flatshare/{prefix}{n}", ad_type, max_pages)
            for k, v in rows.items():
                seen.setdefault(k, v)
            pages += max(p, 1)
        note = f"district mode: {len(seen)} of {total} advertised"
        return list(seen.values()), pages, note

    seen, pages = scrape_path(path, ad_type, max_pages, first_html=first)
    note = None
    if pages >= PAGE_CAP:
        note = f"hit SpareRoom's {PAGE_CAP}-page cap - results may be incomplete"
    return list(seen.values()), pages, note


# ---------- database ----------

OFFERED_SQL = """
insert into market.offered_listings (listing_id, search_area, postcode_district, neighbourhood,
  property_type, rooms_in_property, advertiser_role, room_type_text, room_category, singles, doubles,
  rate_pcm, headline_rate, headline_period, first_rate_pcm, bills_included, available_now,
  available_from, photos, has_video, brand, early_bird, verified, days_old_at_first_seen,
  first_seen, last_seen, en_suite, studio, agent_name)
values (%(listing_id)s, %(area)s, %(postcode_district)s, %(neighbourhood)s, %(property_type)s,
  %(rooms_in_property)s, %(advertiser_role)s, %(room_type_text)s, %(room_category)s, %(singles)s,
  %(doubles)s, %(rate_pcm)s, %(headline_rate)s, %(headline_period)s, %(rate_pcm)s, %(bills_included)s,
  %(available_now)s, %(available_from)s, %(photos)s, %(has_video)s, %(brand)s, %(early_bird)s,
  %(verified)s, %(days_old)s, %(today)s, %(today)s, %(en_suite)s, %(studio)s, %(agent_name)s)
on conflict (listing_id) do update set
  en_suite = excluded.en_suite, studio = excluded.studio, agent_name = excluded.agent_name,
  last_seen = excluded.last_seen, rate_pcm = excluded.rate_pcm, headline_rate = excluded.headline_rate,
  headline_period = excluded.headline_period, bills_included = excluded.bills_included,
  available_now = excluded.available_now, available_from = excluded.available_from,
  photos = excluded.photos, has_video = excluded.has_video, brand = excluded.brand,
  early_bird = excluded.early_bird, verified = excluded.verified,
  room_type_text = excluded.room_type_text, room_category = excluded.room_category,
  singles = excluded.singles, doubles = excluded.doubles
"""

WANTED_SQL = """
insert into market.wanted_listings (listing_id, search_area, budget_pcm, headline_rate, headline_period,
  rooms_sought, number_of_rooms, areas_wanted, gender_occupation, available_now, early_bird, brand,
  days_old_at_first_seen, first_seen, last_seen, en_suite, studio)
values (%(listing_id)s, %(area)s, %(budget_pcm)s, %(headline_rate)s, %(headline_period)s,
  %(rooms_sought)s, %(number_of_rooms)s, %(areas_wanted)s, %(gender_occupation)s, %(available_now)s,
  %(early_bird)s, %(brand)s, %(days_old)s, %(today)s, %(today)s, %(en_suite)s, %(studio)s)
on conflict (listing_id, search_area) do update set
  en_suite = excluded.en_suite, studio = excluded.studio,
  last_seen = excluded.last_seen, budget_pcm = excluded.budget_pcm, headline_rate = excluded.headline_rate,
  headline_period = excluded.headline_period, rooms_sought = excluded.rooms_sought,
  number_of_rooms = excluded.number_of_rooms, areas_wanted = excluded.areas_wanted,
  available_now = excluded.available_now, early_bird = excluded.early_bird, brand = excluded.brand
"""


def save(conn, area, ad_type, rows, pages, today, run_id, note=None):
    table = "offered_listings" if ad_type == "offered" else "wanted_listings"
    rate_col = "rate_pcm" if ad_type == "offered" else "budget_pcm"
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(OFFERED_SQL if ad_type == "offered" else WANTED_SQL,
                        [dict(r, area=area, today=today) for r in rows])
        cur.execute(f"""
            insert into market.daily_stats (stat_date, search_area, ad_type, live_count, new_count, avg_rate_pcm)
            select %(d)s, %(a)s, %(t)s, count(*), count(*) filter (where first_seen = %(d)s),
                   round(avg({rate_col}) filter (where {rate_col} between 100 and 3000), 2)
            from market.{table} where search_area = %(a)s and last_seen = %(d)s
            on conflict (stat_date, search_area, ad_type) do update set
              live_count = excluded.live_count, new_count = excluded.new_count,
              avg_rate_pcm = excluded.avg_rate_pcm""", {"d": today, "a": area, "t": ad_type})
        cur.execute("""update market.scrape_runs set status='success', finished_at=now(),
                       pages=%s, listings=%s, error=%s where run_id=%s""", (pages, len(rows), note, run_id))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--area", action="append", help="limit to these areas (display names)")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument("--ad-type", choices=list(AD_TYPES), action="append",
                    help="offered (daily) and/or wanted (weekly); default both")
    args = ap.parse_args()

    robots.read()
    today = dt.date.today().isoformat()
    areas = {k: v for k, v in AREAS.items() if not args.area or k in args.area}
    conn = None
    if not args.dry_run:
        import psycopg
        conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)

    failures = []
    for area, slug in areas.items():
        for ad_type in (args.ad_type or AD_TYPES):
            run_id = None
            if conn:
                run_id = conn.execute(
                    "insert into market.scrape_runs (run_date, search_area, ad_type) values (%s,%s,%s) returning run_id",
                    (today, area, ad_type)).fetchone()[0]
            try:
                rows, pages, note = scrape(slug, ad_type, args.max_pages)
                if not rows:
                    raise RuntimeError("no listings parsed - page layout may have changed")
                if conn:
                    save(conn, area, ad_type, rows, pages, today, run_id, note)
                else:
                    print(json.dumps({"area": area, "ad_type": ad_type, "pages": pages, "note": note,
                                      "listings": len(rows), "sample": rows[:2]}, indent=1, default=str))
                print(f"{area} {ad_type}: {len(rows)} listings from {pages} pages" + (f" ({note})" if note else ""), file=sys.stderr)
            except Exception as e:
                status = "blocked" if isinstance(e, Blocked) else "failed"
                failures.append(f"{area} {ad_type}: {status} - {e}")
                if conn:
                    conn.execute("update market.scrape_runs set status=%s, error=%s, finished_at=now() where run_id=%s",
                                 (status, str(e)[:500], run_id))
                if isinstance(e, Blocked):
                    break  # leave this area alone for today
            time.sleep(DELAY_SECONDS)

    if failures:
        print("FAILURES:\n" + "\n".join(failures), file=sys.stderr)
        sys.exit(1)  # makes the GitHub Actions run go red and email you


if __name__ == "__main__":
    main()
