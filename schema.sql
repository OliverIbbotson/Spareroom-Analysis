-- Houseshare Heroes SpareRoom market tracker – full schema (idempotent).
create schema if not exists market;

-- ---------- core tables ----------
create table if not exists market.offered_listings (
  listing_id bigint primary key,
  search_area text not null,
  postcode_district text, neighbourhood text, property_type text,
  rooms_in_property int, advertiser_role text,
  room_type_text text, room_category text, singles int, doubles int,
  rate_pcm numeric(10,2), headline_rate numeric(10,2), headline_period text,
  first_rate_pcm numeric(10,2),
  bills_included boolean, available_now boolean, available_from date,
  photos int, has_video boolean, brand text, early_bird boolean, verified boolean,
  days_old_at_first_seen int,
  first_seen date not null, last_seen date not null,
  en_suite boolean, studio boolean, agent_name text
);

create table if not exists market.wanted_listings (
  listing_id bigint not null,
  search_area text not null,
  budget_pcm numeric(10,2), headline_rate numeric(10,2), headline_period text,
  rooms_sought text, number_of_rooms int, areas_wanted text, gender_occupation text,
  available_now boolean, early_bird boolean, brand text,
  days_old_at_first_seen int,
  first_seen date not null, last_seen date not null,
  en_suite boolean, studio boolean,
  primary key (listing_id, search_area)
);

create table if not exists market.scrape_runs (
  run_id bigserial primary key,
  run_date date not null, search_area text not null, ad_type text not null,
  started_at timestamptz not null default now(), finished_at timestamptz,
  pages int default 0, listings int default 0,
  status text not null default 'running', error text
);

create table if not exists market.daily_stats (
  stat_date date not null, search_area text not null, ad_type text not null,
  live_count int, new_count int, avg_rate_pcm numeric(10,2),
  primary key (stat_date, search_area, ad_type)
);

create table if not exists market.offered_price_changes (
  id bigserial primary key,
  listing_id bigint not null references market.offered_listings(listing_id) on delete cascade,
  change_date date not null,
  old_rate_pcm numeric(10,2) not null, new_rate_pcm numeric(10,2) not null,
  old_period text, new_period text,
  change_pct numeric(6,2) generated always as (round((new_rate_pcm - old_rate_pcm) / old_rate_pcm * 100, 2)) stored
);

-- Adjustable weighting for the opportunity ranking (weights are relative).
create table if not exists market.score_weights (
  pillar text primary key,
  weight numeric not null
);
insert into market.score_weights values
  ('tenant_demand', 25), ('ease_of_letting', 25), ('rent_growth', 20),
  ('investor_momentum', 15), ('management_opportunity', 15)
on conflict (pillar) do nothing;

create index if not exists offered_area_seen_idx on market.offered_listings (search_area, last_seen);
create index if not exists offered_district_idx on market.offered_listings (postcode_district);
create index if not exists wanted_area_seen_idx on market.wanted_listings (search_area, last_seen);
create index if not exists runs_date_idx on market.scrape_runs (run_date, search_area, ad_type);
create index if not exists price_changes_listing_idx on market.offered_price_changes (listing_id, change_date);

-- ---------- price-change logging ----------
create or replace function market.log_price_change() returns trigger language plpgsql as $$
begin
  if old.rate_pcm is not null and new.rate_pcm is not null
     and abs(new.rate_pcm - old.rate_pcm) >= 1 then
    insert into market.offered_price_changes (listing_id, change_date, old_rate_pcm, new_rate_pcm, old_period, new_period)
    values (new.listing_id, new.last_seen, old.rate_pcm, new.rate_pcm, old.headline_period, new.headline_period);
  end if;
  return new;
end $$;
drop trigger if exists trg_log_price_change on market.offered_listings;
create trigger trg_log_price_change after update of rate_pcm on market.offered_listings
  for each row execute function market.log_price_change();

-- ---------- base views ----------
-- Created once only: its column list is fixed at creation, so it is not replaced on re-runs.
do $do$ begin
  if not exists (select 1 from pg_views where schemaname = 'market' and viewname = 'towns') then
    execute $v$
      create view market.towns as
      select o.*, case
        when postcode_district = 'NG10' then 'Long Eaton'
        when postcode_district in ('DE13','DE14','DE15') then 'Burton upon Trent'
        else search_area end as town
      from market.offered_listings o $v$;
  end if;
end $do$;

create or replace view market.area_runs as
select search_area, ad_type,
  min(run_date) filter (where status = 'success') as first_run,
  max(run_date) filter (where status = 'success') as last_run,
  count(distinct run_date) filter (where status = 'success' and run_date > current_date - 182) as run_days_6m
from market.scrape_runs group by search_area, ad_type;

-- Rooms that have come down (proxy for "let"), with days on market.
create or replace view market.offered_let as
select t.*,
  (t.last_seen - t.first_seen) + coalesce(t.days_old_at_first_seen, 0) as days_on_market,
  t.last_seen + 1 as let_date,
  (t.room_category in ('single','double') and coalesce(t.singles,0) + coalesce(t.doubles,0) = 1) as single_room
from market.towns t
join market.area_runs r on r.search_area = t.search_area and r.ad_type = 'offered'
where t.last_seen < r.last_run;

create or replace view market.price_reductions as
select t.town, t.search_area, t.postcode_district, t.room_category, t.advertiser_role,
  c.listing_id, c.change_date, c.old_rate_pcm, c.new_rate_pcm, c.change_pct,
  (c.change_date - t.first_seen) + coalesce(t.days_old_at_first_seen, 0) as days_listed_before_cut,
  (t.room_category in ('single','double') and coalesce(t.singles,0) + coalesce(t.doubles,0) = 1
     and c.old_period is not distinct from c.new_period) as clean_signal
from market.offered_price_changes c
join market.towns t using (listing_id)
where c.new_rate_pcm < c.old_rate_pcm;

-- ---------- city scorecard: last 6 months, split into two 3-month halves for trends ----------
create or replace view market.city_metrics as
with p as (select current_date - 182 as ws, current_date - 91 as wm),
ro as (select * from market.area_runs where ad_type = 'offered'),
rw as (select * from market.area_runs where ad_type = 'wanted'),
ds as (
  select d.search_area,
    avg(d.live_count) filter (where d.ad_type = 'offered') as live_avg,
    avg(d.live_count) filter (where d.ad_type = 'offered' and d.stat_date < p.wm) as live_h1,
    avg(d.live_count) filter (where d.ad_type = 'offered' and d.stat_date >= p.wm) as live_h2,
    avg(d.live_count) filter (where d.ad_type = 'wanted') as wanted_live_avg
  from market.daily_stats d, p where d.stat_date >= p.ws group by d.search_area),
o as (
  select l.*, (l.first_seen = ro.first_run) as initial, (l.last_seen = ro.last_run) as live_now,
    (ro.last_run - l.first_seen) + coalesce(l.days_old_at_first_seen, 0) as age_now,
    (l.room_category in ('single','double') and coalesce(l.singles,0) + coalesce(l.doubles,0) = 1) as single_room,
    (l.first_seen < p.wm) as h1
  from market.offered_listings l join ro using (search_area), p
  where l.last_seen >= p.ws),
oa as (
  select search_area,
    count(*) filter (where single_room) as single_room_listings,
    count(*) filter (where not initial and first_seen >= p.ws and h1) as new_h1,
    count(*) filter (where not initial and not h1) as new_h2,
    avg((advertiser_role = 'agent')::int) as agent_share,
    avg((advertiser_role = 'live out landlord')::int) as live_out_share,
    avg((advertiser_role = 'live in landlord')::int) as live_in_share,
    avg((advertiser_role = 'live out landlord')::int) filter (where not initial and first_seen >= p.ws and h1) as live_out_h1,
    avg((advertiser_role = 'live out landlord')::int) filter (where not initial and not h1) as live_out_h2,
    avg(rooms_in_property) as avg_rooms_in_property,
    avg((rooms_in_property >= 6)::int) as share_6plus,
    avg((rooms_in_property >= 6)::int) filter (where not initial and first_seen >= p.ws and h1) as share_6plus_h1,
    avg((rooms_in_property >= 6)::int) filter (where not initial and not h1) as share_6plus_h2,
    avg(photos) as avg_photos, avg(has_video::int) as video_share, avg(verified::int) as verified_share,
    avg(bills_included::int) as bills_share, avg((brand in ('featured','bold'))::int) as paid_advert_share,
    avg((age_now > 60)::int) filter (where live_now) as stale_share,
    avg(available_now::int) filter (where live_now) as available_now_share,
    avg((room_category = 'double')::int) filter (where single_room) as double_supply_share,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where live_now and single_room) as asking_room_now,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where live_now and single_room and room_category = 'single') as asking_single_now,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where live_now and single_room and room_category = 'double') as asking_double_now,
    percentile_cont(0.5) within group (order by first_rate_pcm) filter (where not initial and single_room and room_category = 'double' and first_seen >= p.ws and h1) as asking_double_h1,
    percentile_cont(0.5) within group (order by first_rate_pcm) filter (where not initial and single_room and room_category = 'double' and not h1) as asking_double_h2
  from o, p group by search_area),
lt as (
  select l.search_area, count(*) as lets,
    percentile_cont(0.5) within group (order by days_on_market) as median_days_to_let,
    percentile_cont(0.5) within group (order by days_on_market) filter (where let_date < p.wm) as dtl_h1,
    percentile_cont(0.5) within group (order by days_on_market) filter (where let_date >= p.wm) as dtl_h2,
    avg((days_on_market <= 14)::int) as pct_let_14d,
    avg((days_on_market <= 30)::int) as pct_let_30d,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room and room_category = 'double' and let_date < p.wm) as achieved_double_h1,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room and room_category = 'double' and let_date >= p.wm) as achieved_double_h2,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room) as achieved_room_rent
  from market.offered_let l, p where l.let_date >= p.ws group by l.search_area),
pc as (
  select l.search_area,
    count(distinct c.listing_id) filter (where c.new_rate_pcm < c.old_rate_pcm) as cut_listings,
    count(*) filter (where c.new_rate_pcm < c.old_rate_pcm) as cuts,
    count(*) filter (where c.new_rate_pcm > c.old_rate_pcm) as rises,
    avg(c.change_pct) filter (where c.new_rate_pcm < c.old_rate_pcm) as avg_cut_pct
  from market.offered_price_changes c join market.offered_listings l using (listing_id), p
  where c.change_date >= p.ws
    and l.room_category in ('single','double') and coalesce(l.singles,0) + coalesce(l.doubles,0) = 1
    and c.old_period is not distinct from c.new_period
  group by l.search_area),
w as (
  select wl.search_area,
    count(*) filter (where wl.first_seen <> rw.first_run and wl.first_seen >= p.ws and wl.first_seen < p.wm) as wanted_new_h1,
    count(*) filter (where wl.first_seen <> rw.first_run and wl.first_seen >= p.wm) as wanted_new_h2,
    avg(wl.available_now::int) as wanted_now_share,
    percentile_cont(0.5) within group (order by wl.budget_pcm) filter (where wl.budget_pcm between 100 and 3000) as median_budget,
    avg((wl.rooms_sought ilike 'double%')::int) as double_pref_share
  from market.wanted_listings wl join rw using (search_area), p
  where wl.last_seen >= p.ws group by wl.search_area)
select ro.search_area,
  ro.run_days_6m, coalesce(lt.lets, 0) as lets_6m,
  (ro.run_days_6m >= 150 and coalesce(lt.lets, 0) >= 50 and coalesce(ds.live_avg, 0) >= 100) as eligible,  -- enough history, lets and market size
  -- market size & investor momentum
  round(ds.live_avg) as live_rooms_avg,
  round((ds.live_h2 / nullif(ds.live_h1, 0) - 1)::numeric, 3) as supply_growth,
  round((oa.new_h2::numeric / nullif(oa.new_h1, 0) - 1), 3) as new_ads_growth,
  round(oa.agent_share, 3) as agent_share,
  round(oa.live_out_share, 3) as live_out_landlord_share,
  round(oa.live_in_share, 3) as live_in_landlord_share,
  round(oa.live_out_h2 - oa.live_out_h1, 3) as live_out_share_trend,
  round(oa.avg_rooms_in_property, 1) as avg_rooms_in_property,
  round(oa.share_6plus, 3) as share_6plus_bed,
  round(oa.share_6plus_h2 - oa.share_6plus_h1, 3) as share_6plus_trend,
  round(oa.avg_photos, 1) as avg_photos,
  round(oa.video_share, 3) as video_share,
  round(oa.verified_share, 3) as verified_share,
  round(oa.bills_share, 3) as bills_included_share,
  round(oa.paid_advert_share, 3) as paid_advert_share,
  -- tenant demand
  round(ds.wanted_live_avg) as wanted_live_avg,
  round((ds.wanted_live_avg / nullif(ds.live_avg, 0))::numeric, 3) as tenants_per_room,
  round((w.wanted_new_h2::numeric / nullif(w.wanted_new_h1, 0) - 1), 3) as wanted_growth,
  round(w.wanted_now_share, 3) as wanted_now_share,
  round(w.double_pref_share, 3) as double_pref_share,
  round(oa.double_supply_share, 3) as double_supply_share,
  round(w.double_pref_share - oa.double_supply_share, 3) as double_demand_gap,
  -- rents
  round(oa.asking_single_now::numeric) as asking_single_pcm,
  round(oa.asking_double_now::numeric) as asking_double_pcm,
  round(lt.achieved_room_rent::numeric) as achieved_room_pcm,
  round((oa.asking_double_h2 / nullif(oa.asking_double_h1, 0) - 1)::numeric, 3) as asking_rent_growth,
  round((lt.achieved_double_h2 / nullif(lt.achieved_double_h1, 0) - 1)::numeric, 3) as achieved_rent_growth,
  round(w.median_budget::numeric) as median_budget_pcm,
  round((w.median_budget - oa.asking_room_now)::numeric) as budget_headroom_pcm,
  round((pc.rises::numeric / nullif(pc.cuts, 0)), 2) as rises_per_cut,
  -- ease of letting
  round(lt.median_days_to_let::numeric, 1) as median_days_to_let,
  round((lt.dtl_h2 - lt.dtl_h1)::numeric, 1) as days_to_let_trend,
  round(lt.pct_let_14d, 3) as pct_let_14d,
  round(lt.pct_let_30d, 3) as pct_let_30d,
  round(oa.stale_share, 3) as stale_60d_share,
  round(oa.available_now_share, 3) as available_now_share,
  round(coalesce(pc.cut_listings, 0)::numeric / nullif(oa.single_room_listings, 0), 3) as price_cut_rate,
  round(pc.avg_cut_pct, 2) as avg_cut_pct
from ro
left join ds using (search_area)
left join oa using (search_area)
left join lt using (search_area)
left join pc using (search_area)
left join w using (search_area);

-- ---------- opportunity ranking ----------
-- Each metric becomes a 0-1 percentile rank across tracked cities (missing = 0.5),
-- flipped where lower is better, averaged into five pillars, then weighted.
create or replace view market.city_ranking as
with m as (select * from market.city_metrics where live_rooms_avg is not null),
r as (
  select search_area, eligible, run_days_6m, lets_6m, live_rooms_avg,
    -- tenant demand
    case when tenants_per_room is null then 0.5 else percent_rank() over (partition by tenants_per_room is null order by tenants_per_room) end as r_tpr,
    case when wanted_growth is null then 0.5 else percent_rank() over (partition by wanted_growth is null order by wanted_growth) end as r_wg,
    case when wanted_now_share is null then 0.5 else percent_rank() over (partition by wanted_now_share is null order by wanted_now_share) end as r_wnow,
    -- ease of letting (lower days / stale / cuts = better)
    case when median_days_to_let is null then 0.5 else percent_rank() over (partition by median_days_to_let is null order by median_days_to_let desc) end as r_dtl,
    case when pct_let_30d is null then 0.5 else percent_rank() over (partition by pct_let_30d is null order by pct_let_30d) end as r_l30,
    case when stale_60d_share is null then 0.5 else percent_rank() over (partition by stale_60d_share is null order by stale_60d_share desc) end as r_stale,
    case when price_cut_rate is null then 0.5 else percent_rank() over (partition by price_cut_rate is null order by price_cut_rate desc) end as r_cut,
    case when days_to_let_trend is null then 0.5 else percent_rank() over (partition by days_to_let_trend is null order by days_to_let_trend desc) end as r_dtlt,
    -- rent growth
    case when achieved_rent_growth is null then 0.5 else percent_rank() over (partition by achieved_rent_growth is null order by achieved_rent_growth) end as r_arg,
    case when asking_rent_growth is null then 0.5 else percent_rank() over (partition by asking_rent_growth is null order by asking_rent_growth) end as r_skg,
    case when rises_per_cut is null then 0.5 else percent_rank() over (partition by rises_per_cut is null order by rises_per_cut) end as r_rpc,
    case when budget_headroom_pcm is null then 0.5 else percent_rank() over (partition by budget_headroom_pcm is null order by budget_headroom_pcm) end as r_head,
    -- investor momentum
    case when new_ads_growth is null then 0.5 else percent_rank() over (partition by new_ads_growth is null order by new_ads_growth) end as r_nag,
    case when live_out_share_trend is null then 0.5 else percent_rank() over (partition by live_out_share_trend is null order by live_out_share_trend) end as r_lot,
    case when share_6plus_trend is null then 0.5 else percent_rank() over (partition by share_6plus_trend is null order by share_6plus_trend) end as r_6t,
    -- management opportunity (many self-managing landlords, few agents, big enough market)
    case when live_out_landlord_share is null then 0.5 else percent_rank() over (partition by live_out_landlord_share is null order by live_out_landlord_share) end as r_lo,
    case when agent_share is null then 0.5 else percent_rank() over (partition by agent_share is null order by agent_share desc) end as r_ag,
    percent_rank() over (order by live_rooms_avg) as r_size
  from m),
pillars as (
  select *,
    round((100 * (r_tpr * 2 + r_wg + r_wnow) / 4)::numeric, 1) as tenant_demand,
    round((100 * (r_dtl * 2 + r_l30 + r_stale + r_cut + r_dtlt) / 6)::numeric, 1) as ease_of_letting,
    round((100 * (r_arg * 2 + r_skg + r_rpc + r_head) / 5)::numeric, 1) as rent_growth,
    round((100 * (r_nag + r_lot + r_6t) / 3)::numeric, 1) as investor_momentum,
    round((100 * (r_lo + r_ag + r_size) / 3)::numeric, 1) as management_opportunity
  from r),
wt as (select
  max(weight) filter (where pillar = 'tenant_demand') as w1,
  max(weight) filter (where pillar = 'ease_of_letting') as w2,
  max(weight) filter (where pillar = 'rent_growth') as w3,
  max(weight) filter (where pillar = 'investor_momentum') as w4,
  max(weight) filter (where pillar = 'management_opportunity') as w5
  from market.score_weights)
select search_area as city,
  round((tenant_demand * w1 + ease_of_letting * w2 + rent_growth * w3
         + investor_momentum * w4 + management_opportunity * w5) / (w1 + w2 + w3 + w4 + w5), 1) as opportunity_score,
  tenant_demand, ease_of_letting, rent_growth, investor_momentum, management_opportunity,
  eligible, run_days_6m, lets_6m, live_rooms_avg
from pillars, wt;

-- Top 10: only cities with ~5 months of successful daily runs, 50+ lets and 100+ live rooms on average.
create or replace view market.top10_hmo_markets as
select rank() over (order by opportunity_score desc) as position, *
from market.city_ranking where eligible
order by opportunity_score desc limit 10;

-- Same ranking, including cities that don't yet have enough data (for early peeks).
create or replace view market.city_ranking_provisional as
select rank() over (order by opportunity_score desc) as position, *
from market.city_ranking order by opportunity_score desc;

-- ---------- en-suite & studio (flagged from the advert headline / short description) ----------
alter table market.offered_listings add column if not exists en_suite boolean;
alter table market.wanted_listings add column if not exists en_suite boolean;
alter table market.offered_listings add column if not exists studio boolean;
alter table market.wanted_listings add column if not exists studio boolean;
update market.offered_listings set room_category = 'studio'
  where room_type_text ilike '%studio%' and room_category is distinct from 'studio';

-- Views below are dropped and rebuilt so new table columns flow through.
drop view if exists market.en_suite_summary;
drop view if exists market.premium_rooms_summary;
drop view if exists market.room_type_stats;
drop view if exists market.room_types;

-- The room types used in the original area reports, plus Studio.
create view market.room_types as
select t.*,
  case when t.postcode_district = 'NG10' then 'Long Eaton'
       when t.postcode_district in ('DE13','DE14','DE15') then 'Burton upon Trent'
       else t.search_area end as town,
  case when t.room_category = 'studio' or t.studio then 'Studio'
       when t.room_category = 'single' and t.en_suite then 'Single En Suite'
       when t.room_category = 'single' then 'Single'
       when t.room_category = 'double' and t.en_suite then 'Double En Suite'
       when t.room_category = 'double' then 'Double' end as room_type
from market.offered_listings t
where (t.room_category in ('single','double') and coalesce(t.singles,0) + coalesce(t.doubles,0) = 1)
   or t.room_category = 'studio'
   or (t.studio and t.room_type_text ilike '1 bed%');

-- Per city/town and room type: share of rooms, asking rent, days to let (last 6 months).
create view market.room_type_stats as
with r as (select * from market.area_runs where ad_type = 'offered'),
live as (
  select rt.town, rt.room_type, count(*) as live_rooms,
    percentile_cont(0.5) within group (order by rt.rate_pcm) as median_asking_pcm,
    avg(rt.rate_pcm) filter (where rt.rate_pcm between 150 and 2500) as mean_asking_pcm
  from market.room_types rt join r using (search_area)
  where rt.last_seen = r.last_run group by 1, 2),
lets as (
  select rt.town, rt.room_type, count(*) as lets_6m,
    percentile_cont(0.5) within group (order by (rt.last_seen - rt.first_seen) + coalesce(rt.days_old_at_first_seen,0)) as median_days_to_let,
    percentile_cont(0.5) within group (order by rt.rate_pcm) as median_let_pcm
  from market.room_types rt join r using (search_area)
  where rt.last_seen < r.last_run and rt.last_seen >= current_date - 182 group by 1, 2)
select coalesce(live.town, lets.town) as town, coalesce(live.room_type, lets.room_type) as room_type,
  coalesce(live.live_rooms, 0) as live_rooms,
  round(coalesce(live.live_rooms, 0)::numeric / nullif(sum(coalesce(live.live_rooms, 0)) over (partition by coalesce(live.town, lets.town)), 0), 3) as share_of_live,
  round(live.median_asking_pcm::numeric) as median_asking_pcm,
  coalesce(lets.lets_6m, 0) as lets_6m,
  round(lets.median_days_to_let::numeric, 1) as median_days_to_let,
  round(lets.median_let_pcm::numeric) as median_let_pcm,
  round(live.mean_asking_pcm::numeric) as mean_asking_pcm
from live full join lets on live.town = lets.town and live.room_type = lets.room_type;

-- En-suite and studio: supply vs tenant demand, and rent premium over a standard double.
create view market.premium_rooms_summary as
with r as (select * from market.area_runs where ad_type = 'offered'),
o as (
  select rt.search_area,
    avg((rt.room_type like '%En Suite')::int) filter (where rt.en_suite is not null) as en_suite_share_of_rooms,
    avg((rt.room_type = 'Studio')::int) filter (where rt.studio is not null) as studio_share_of_rooms,
    percentile_cont(0.5) within group (order by rt.rate_pcm) filter (where rt.room_type = 'Double') as double_pcm,
    percentile_cont(0.5) within group (order by rt.rate_pcm) filter (where rt.room_type = 'Double En Suite') as double_en_suite_pcm,
    percentile_cont(0.5) within group (order by rt.rate_pcm) filter (where rt.room_type = 'Studio') as studio_pcm
  from market.room_types rt join r using (search_area)
  where rt.last_seen = r.last_run group by 1),
w as (
  select search_area,
    avg(en_suite::int) as en_suite_share_of_wanted,
    avg(studio::int) as studio_share_of_wanted
  from market.wanted_listings where last_seen >= current_date - 182 and en_suite is not null group by 1)
select o.search_area as city,
  round(o.double_pcm::numeric) as double_pcm,
  round(o.en_suite_share_of_rooms, 3) as en_suite_share_of_rooms,
  round(w.en_suite_share_of_wanted, 3) as en_suite_share_of_wanted,
  round(o.double_en_suite_pcm::numeric) as double_en_suite_pcm,
  round((o.double_en_suite_pcm - o.double_pcm)::numeric) as en_suite_premium_pcm,
  round(o.studio_share_of_rooms, 3) as studio_share_of_rooms,
  round(w.studio_share_of_wanted, 3) as studio_share_of_wanted,
  round(o.studio_pcm::numeric) as studio_pcm,
  round((o.studio_pcm - o.double_pcm)::numeric) as studio_premium_pcm
from o left join w using (search_area);

-- Weekly rooms available vs tenants looking, per city.
create or replace view market.supply_vs_demand as
select date_trunc('week', stat_date)::date as week_start,
  search_area as city,
  round(avg(live_count) filter (where ad_type = 'offered')) as rooms_available,
  max(live_count) filter (where ad_type = 'wanted') as tenants_looking,
  sum(new_count) filter (where ad_type = 'offered') as new_rooms_listed,
  max(new_count) filter (where ad_type = 'wanted') as new_tenant_ads,
  round(max(live_count) filter (where ad_type = 'wanted')::numeric
        / nullif(avg(live_count) filter (where ad_type = 'offered'), 0), 2) as tenants_per_room
from market.daily_stats
group by 1, 2;

-- ---------- up-and-coming markets: ranked on direction of change only ----------
-- Latest 3 months (h2) vs the 3 months before (h1).

-- Rising cities: smaller markets are allowed in (50+ live rooms, 30+ lets).
create or replace view market.rising_cities as
with m as (
  select * from market.city_metrics
  where run_days_6m >= 150 and lets_6m >= 30 and live_rooms_avg >= 50),
r as (
  select search_area,
    case when achieved_rent_growth is null then 0.5 else percent_rank() over (partition by achieved_rent_growth is null order by achieved_rent_growth) end as r_arg,
    case when asking_rent_growth is null then 0.5 else percent_rank() over (partition by asking_rent_growth is null order by asking_rent_growth) end as r_skg,
    case when wanted_growth is null then 0.5 else percent_rank() over (partition by wanted_growth is null order by wanted_growth) end as r_wg,
    case when days_to_let_trend is null then 0.5 else percent_rank() over (partition by days_to_let_trend is null order by days_to_let_trend desc) end as r_dtlt,
    case when rises_per_cut is null then 0.5 else percent_rank() over (partition by rises_per_cut is null order by rises_per_cut) end as r_rpc,
    case when new_ads_growth is null then 0.5 else percent_rank() over (partition by new_ads_growth is null order by new_ads_growth) end as r_nag,
    case when live_out_share_trend is null then 0.5 else percent_rank() over (partition by live_out_share_trend is null order by live_out_share_trend) end as r_lot,
    case when share_6plus_trend is null then 0.5 else percent_rank() over (partition by share_6plus_trend is null order by share_6plus_trend) end as r_6t,
    achieved_rent_growth, asking_rent_growth, wanted_growth, days_to_let_trend, new_ads_growth, live_rooms_avg
  from m)
select rank() over (order by rising_score desc) as position, *
from (
  select search_area as city,
    round((100 * (r_arg * 2 + r_skg + r_wg * 2 + r_dtlt * 2 + r_rpc + r_nag + r_lot + r_6t) / 11)::numeric, 1) as rising_score,
    achieved_rent_growth, asking_rent_growth, wanted_growth, days_to_let_trend, new_ads_growth, live_rooms_avg
  from r) s(city, rising_score, achieved_rent_growth, asking_rent_growth, wanted_growth, days_to_let_trend, new_ads_growth, live_rooms_avg)
order by rising_score desc;

-- Postcode-district trends (all districts with data).
create or replace view market.district_trends as
with p as (select current_date - 182 as ws, current_date - 91 as wm),
ro as (select * from market.area_runs where ad_type = 'offered'),
l as (
  select search_area, town, postcode_district,
    count(*) filter (where let_date < p.wm) as lets_h1,
    count(*) filter (where let_date >= p.wm) as lets_h2,
    percentile_cont(0.5) within group (order by days_on_market) filter (where let_date < p.wm) as dtl_h1,
    percentile_cont(0.5) within group (order by days_on_market) filter (where let_date >= p.wm) as dtl_h2,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room and let_date < p.wm) as let_rent_h1,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room and let_date >= p.wm) as let_rent_h2
  from market.offered_let, p
  where let_date >= p.ws and postcode_district is not null
  group by 1, 2, 3),
o as (
  select l.search_area, l.postcode_district,
    count(*) filter (where l.first_seen <> ro.first_run and l.first_seen >= p.ws and l.first_seen < p.wm) as new_h1,
    count(*) filter (where l.first_seen <> ro.first_run and l.first_seen >= p.wm) as new_h2,
    count(*) filter (where l.first_seen < p.wm and l.last_seen >= p.ws) as live_h1,
    count(*) filter (where l.last_seen >= p.wm) as live_h2,
    count(distinct l.listing_id) filter (where c.change_date >= p.ws and c.change_date < p.wm) as cut_h1,
    count(distinct l.listing_id) filter (where c.change_date >= p.wm) as cut_h2
  from market.offered_listings l join ro using (search_area)
  cross join p
  left join market.offered_price_changes c on c.listing_id = l.listing_id and c.new_rate_pcm < c.old_rate_pcm
  where l.last_seen >= p.ws and l.postcode_district is not null
  group by 1, 2)
select l.search_area as city, l.town, l.postcode_district,
  l.lets_h1, l.lets_h2,
  round((l.lets_h2::numeric / nullif(l.lets_h1, 0) - 1), 3) as lets_growth,
  round(l.dtl_h1::numeric, 1) as days_to_let_before, round(l.dtl_h2::numeric, 1) as days_to_let_now,
  round((l.dtl_h2 - l.dtl_h1)::numeric, 1) as days_to_let_change,
  round(l.let_rent_h1::numeric) as let_rent_before, round(l.let_rent_h2::numeric) as let_rent_now,
  round((l.let_rent_h2 / nullif(l.let_rent_h1, 0) - 1)::numeric, 3) as rent_growth,
  round((o.new_h2::numeric / nullif(o.new_h1, 0) - 1), 3) as new_listings_growth,
  round(o.cut_h1::numeric / nullif(o.live_h1, 0), 3) as cut_rate_before,
  round(o.cut_h2::numeric / nullif(o.live_h2, 0), 3) as cut_rate_now,
  (l.lets_h1 >= 10 and l.lets_h2 >= 10) as enough_data
from l left join o on o.search_area = l.search_area and o.postcode_district = l.postcode_district;

-- Rising postcode districts, ranked nationally and within each city.
create or replace view market.rising_districts as
with d as (select * from market.district_trends where enough_data),
r as (
  select *,
    case when rent_growth is null then 0.5 else percent_rank() over (partition by rent_growth is null order by rent_growth) end as r_rent,
    case when days_to_let_change is null then 0.5 else percent_rank() over (partition by days_to_let_change is null order by days_to_let_change desc) end as r_dtl,
    case when lets_growth is null then 0.5 else percent_rank() over (partition by lets_growth is null order by lets_growth) end as r_lets,
    case when cut_rate_now is null or cut_rate_before is null then 0.5
         else percent_rank() over (partition by cut_rate_now is null or cut_rate_before is null order by cut_rate_now - cut_rate_before desc) end as r_cut,
    case when new_listings_growth is null then 0.5 else percent_rank() over (partition by new_listings_growth is null order by new_listings_growth) end as r_new
  from d),
s as (
  select *, round((100 * (r_rent * 3 + r_dtl * 2 + r_lets * 2 + r_cut + r_new) / 9)::numeric, 1) as rising_score from r)
select rank() over (order by rising_score desc) as national_position,
  rank() over (partition by city order by rising_score desc) as position_in_city,
  city, town, postcode_district, rising_score,
  rent_growth, let_rent_before, let_rent_now, days_to_let_before, days_to_let_now,
  lets_growth, cut_rate_before, cut_rate_now, new_listings_growth, lets_h1, lets_h2
from s order by rising_score desc;


-- ---------- agent market share (agent adverts only, using the company names shown on SpareRoom) ----------
alter table market.offered_listings add column if not exists agent_name text;

-- Normalised company key so "Clayton & Co" / "CLAYTON & CO LTD" count as one agent.
create or replace function market.agent_key(n text) returns text language sql immutable as $$
  select nullif(regexp_replace(
    regexp_replace(lower(coalesce(n, '')), '\m(ltd|limited|llp|plc|and)\M', '', 'g'),
    '[^a-z0-9]', '', 'g'), '')
$$;

create or replace view market.agent_share as
with r as (select * from market.area_runs where ad_type = 'offered'),
b as (
  select l.*, r.last_run,
    case when l.postcode_district = 'NG10' then 'Long Eaton'
         when l.postcode_district in ('DE13','DE14','DE15') then 'Burton upon Trent'
         else l.search_area end as town,
    market.agent_key(l.agent_name) as agent_key,
    greatest(coalesce(l.singles, 0) + coalesce(l.doubles, 0), 1) as rooms,
    (l.last_seen - l.first_seen) + coalesce(l.days_old_at_first_seen, 0) as days_on_market,
    (l.room_category in ('single','double') and coalesce(l.singles,0) + coalesce(l.doubles,0) = 1) as single_room
  from market.offered_listings l join r using (search_area)
  where l.last_seen >= current_date - 182),
totals as (select town, sum(rooms) filter (where last_seen = last_run) as all_live_rooms from b group by town),
a as (
  select town, agent_key,
    mode() within group (order by agent_name) as agent,
    count(*) filter (where last_seen = last_run) as live_adverts,
    coalesce(sum(rooms) filter (where last_seen = last_run), 0) as live_rooms,
    coalesce(sum(rooms) filter (where last_seen < last_run), 0) as rooms_let_6m,
    percentile_cont(0.5) within group (order by days_on_market) filter (where last_seen < last_run) as median_days_to_let,
    percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room and last_seen = last_run) as median_asking_pcm,
    avg(photos) filter (where last_seen = last_run) as avg_photos
  from b where agent_key is not null group by town, agent_key)
select a.town, rank() over (partition by a.town order by a.live_rooms desc, a.rooms_let_6m desc) as position,
  a.agent, a.live_adverts, a.live_rooms,
  round(a.live_rooms::numeric / nullif(sum(a.live_rooms) over (partition by a.town), 0), 3) as share_of_agent_rooms,
  round(a.live_rooms::numeric / nullif(t.all_live_rooms, 0), 3) as share_of_all_rooms,
  a.rooms_let_6m,
  round(a.rooms_let_6m::numeric / nullif(sum(a.rooms_let_6m) over (partition by a.town), 0), 3) as share_of_agent_lets_6m,
  round(a.median_days_to_let::numeric, 1) as median_days_to_let,
  round(a.median_asking_pcm::numeric) as median_asking_pcm,
  round(a.avg_photos, 1) as avg_photos,
  (a.agent_key = 'houseshareheroes') as is_houseshare_heroes
from a join totals t using (town)
where a.live_rooms > 0 or a.rooms_let_6m > 0;

-- How concentrated each local letting-agent market is (a fragmented market = room for a newcomer).
create or replace view market.agent_concentration as
with s as (select * from market.agent_share),
t as (
  select l.town, sum(greatest(coalesce(l.singles,0) + coalesce(l.doubles,0), 1)) as all_live_rooms
  from (select o.*, case when o.postcode_district = 'NG10' then 'Long Eaton'
               when o.postcode_district in ('DE13','DE14','DE15') then 'Burton upon Trent'
               else o.search_area end as town
        from market.offered_listings o) l
  join market.area_runs r on r.search_area = l.search_area and r.ad_type = 'offered'
  where l.last_seen = r.last_run group by l.town)
select s.town,
  count(*) filter (where s.live_rooms > 0) as active_agents,
  sum(s.live_rooms) as agent_live_rooms,
  round(sum(s.live_rooms)::numeric / nullif(max(t.all_live_rooms), 0), 3) as agent_share_of_market,
  round(sum(s.share_of_agent_rooms) filter (where s.position = 1), 3) as top1_share,
  round(sum(s.share_of_agent_rooms) filter (where s.position <= 3), 3) as top3_share,
  round(sum(s.share_of_agent_rooms) filter (where s.position <= 5), 3) as top5_share,
  round(sum(power(s.share_of_agent_rooms * 100, 2))) as hhi,
  max(s.agent) filter (where s.position = 1) as largest_agent,
  sum(s.live_rooms) filter (where s.is_houseshare_heroes) as hsh_live_rooms,
  max(s.share_of_agent_rooms) filter (where s.is_houseshare_heroes) as hsh_share_of_agent_rooms,
  min(s.position) filter (where s.is_houseshare_heroes) as hsh_position
from s join t using (town)
group by s.town;
