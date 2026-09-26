"""Load HM Land Registry sold prices into market.house_prices.

Streams the Price Paid Data yearly files (this year + last year), keeps standard
sales (PPD category A) from the last 12 months, and stores median prices for:
  - every postcode district we track rooms in   (area_type = 'district')
  - every town in the reports                   (area_type = 'town')

'ts' = terraced + semi-detached houses, the stock most HMOs are converted from.
'all' = every property type.

Runs in the monthly GitHub Actions workflow before report.py. Contains HM Land
Registry data © Crown copyright and database right. Licensed under the Open
Government Licence v3.0.

    python landreg.py
"""
import csv, datetime as dt, io, os, statistics, sys

import psycopg
import requests

BASE = "http://prod.publicdata.landregistry.gov.uk.s3-website-eu-west-1.amazonaws.com"
TOWN_SQL = ("case when postcode_district = 'NG10' then 'Long Eaton' "
            "when postcode_district in ('DE13','DE14','DE15') then 'Burton upon Trent' "
            "else search_area end")

DDL = """
create table if not exists market.house_prices (
  area_type    text not null,          -- 'district' or 'town'
  area         text not null,
  period_start date not null,
  period_end   date not null,
  median_ts    numeric,                -- terraced + semi-detached
  n_ts         int,
  median_all   numeric,
  n_all        int,
  updated      timestamptz not null default now(),
  primary key (area_type, area)
)"""


def stream_year(year):
    url = f"{BASE}/pp-{year}.csv"
    r = requests.get(url, stream=True, timeout=300)
    if r.status_code == 404:
        print("not published yet:", url)
        return
    r.raise_for_status()
    lines = (l.decode("utf-8", "replace") for l in r.iter_lines() if l)
    yield from csv.reader(lines)


def main():
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(DDL)
        towns_by_district = {}
        for town, district in conn.execute(
                f"select distinct {TOWN_SQL}, postcode_district from market.offered_listings "
                "where postcode_district is not null"):
            towns_by_district.setdefault(district.upper(), set()).add(town)
        if not towns_by_district:
            sys.exit("no postcode districts in market.offered_listings yet")

        year = dt.date.today().year
        sales = []   # (date, district, price, type)
        for y in (year - 1, year):
            n = 0
            for row in stream_year(y):
                # id, price, date, postcode, type, new, tenure, paon, saon, street, locality, town, district, county, ppd_cat, status
                if len(row) < 15 or row[14] != "A" or not row[3]:
                    continue
                district = row[3].split()[0].upper()
                if district not in towns_by_district:
                    continue
                sales.append((dt.date.fromisoformat(row[2][:10]), district, int(row[1]), row[4]))
                n += 1
            print(f"pp-{y}: {n:,} sales in tracked districts")
        if not sales:
            sys.exit("no sales found")

        end = max(s[0] for s in sales)
        start = end.replace(year=end.year - 1) + dt.timedelta(days=1)
        groups = {}
        for d, district, price, ptype in sales:
            if d < start:
                continue
            keys = [("district", district)] + [("town", t) for t in towns_by_district[district]]
            for k in keys:
                g = groups.setdefault(k, {"ts": [], "all": []})
                g["all"].append(price)
                if ptype in ("T", "S"):
                    g["ts"].append(price)

        rows = [(t, a, start, end,
                 statistics.median(g["ts"]) if g["ts"] else None, len(g["ts"]),
                 statistics.median(g["all"]), len(g["all"]))
                for (t, a), g in groups.items()]
        with conn.cursor() as cur:
            cur.executemany("""
              insert into market.house_prices (area_type, area, period_start, period_end, median_ts, n_ts, median_all, n_all)
              values (%s, %s, %s, %s, %s, %s, %s, %s)
              on conflict (area_type, area) do update set period_start = excluded.period_start,
                period_end = excluded.period_end, median_ts = excluded.median_ts, n_ts = excluded.n_ts,
                median_all = excluded.median_all, n_all = excluded.n_all, updated = now()""", rows)
        print(f"stored {len(rows)} areas, sales {start} to {end}")


if __name__ == "__main__":
    main()
