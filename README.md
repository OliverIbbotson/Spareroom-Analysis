# Houseshare Heroes – SpareRoom market tracker

Tracks 53 UK cities on SpareRoom to score which markets are best for a new HMO
management franchise. Only public search-result pages are read (never individual
adverts, which SpareRoom's robots.txt excludes); robots.txt is checked before every
request, requests are made one at a time with a 2.5s pause, and if SpareRoom
returns a block (403/429) the script stops for that area rather than retrying.

## Schedules
- **Daily rooms-offered scrape** – 03:30 UTC, ~2 hours. Drives days-to-let, lets,
  prices and price cuts.
- **Weekly rooms-wanted scrape** – Mondays 10:00 UTC. Drives tenant demand metrics.

## Cities
Birmingham, Manchester, Leeds, Liverpool, Sheffield, Bristol, Nottingham, Leicester,
Coventry, Derby, Stoke-on-Trent, Lincoln, Dudley, Wolverhampton, Walsall, Newcastle
upon Tyne, Sunderland, Middlesbrough, Hull, Bradford, Huddersfield, Wakefield, York,
Doncaster, Preston, Bolton, Blackpool, Stockport, Oldham, Warrington, Chester, Telford,
Worcester, Gloucester, Northampton, Peterborough, Milton Keynes, Luton, Reading, Oxford,
Cambridge, Norwich, Ipswich, Southampton, Portsmouth, Brighton, Exeter, Plymouth,
Cardiff, Swansea, Newport, Glasgow, Edinburgh. Long Eaton and Burton upon Trent are
split out by postcode in `market.towns`. Edit `AREAS` in scrape.py to add or remove.

## Setup
1. Create a GitHub repository and upload these files (keep the `.github/workflows` folder).
   - **Public repo (recommended):** GitHub Actions minutes are free. The weekly job
     makes a small "heartbeat" commit so GitHub never pauses the schedule.
     The code contains no secrets.
   - **Private repo:** the daily run uses ~4,000 Actions minutes a month, more than
     the free allowance, so expect a monthly GitHub bill for the overage.
2. Settings → Secrets and variables → Actions → add `DATABASE_URL` (Neon connection
   string) and optionally `CONTACT_EMAIL`.
3. Actions tab → run each workflow once manually to test.
GitHub emails you automatically if a run fails.

## Reading the results (Neon SQL editor)
    select * from market.top10_hmo_markets;          -- the top 10 (after ~5 months)
    select * from market.city_ranking_provisional;   -- all cities, including early data
    select * from market.city_metrics;               -- full scorecard, one row per city
    select * from market.price_reductions where clean_signal;

A city enters the top 10 once it has 150+ days of successful runs in the last
6 months, 50+ lets, and an average of 100+ live rooms (big enough to build a
portfolio). Trend metrics compare the latest 3 months with the 3 months before.

## Scoring
Each metric is ranked against the other cities (0–100), grouped into five pillars,
and weighted. Change the weights any time – the ranking updates instantly:

    update market.score_weights set weight = 30 where pillar = 'rent_growth';

| Pillar | Default weight | Metrics |
|---|---|---|
| tenant_demand | 25 | tenants per room (x2), growth in rooms-wanted ads, share needing a room now |
| ease_of_letting | 25 | median days to let (x2), % let within 30 days, stale 60+ day stock, price-cut rate, days-to-let trend |
| rent_growth | 20 | achieved rent growth (x2), asking rent growth, rises per cut, tenant budget headroom |
| investor_momentum | 15 | growth in new adverts, trend in live-out landlord share, trend in 6+ bed HMOs |
| management_opportunity | 15 | share of self-managing live-out landlords, low agent share, market size, fragmented agent market (low HHI) |

## Monthly PDF reports
`report.py` builds a branded PDF for every town plus a national "Top HMO Markets" report.
The **Monthly market reports** workflow runs on the 1st of each month (or on demand from the
Actions tab); open the finished run and download the `market-reports` bundle at the bottom.

    python report.py --all                 # everything
    python report.py --town Derby          # one town
    python report.py --national            # national report only

## Files
- `scrape.py` – the scraper (`--dry-run`, `--area`, `--ad-type`, `--max-pages` for testing)
- `schema.sql` – full database schema; safe to re-run
- `report.py` – monthly PDF report generator
- `brand/` – banner and logo used in the reports
