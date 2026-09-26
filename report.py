"""
Houseshare Heroes – monthly HMO market report generator.

Builds a branded PDF for every tracked town plus a national "Top HMO Markets"
report, using the `market` schema in Neon.

Usage:
    python report.py --all                      # every town + national report
    python report.py --town Derby               # one town
    python report.py --national                 # national report only
    python report.py --town HotTown --label "Sample Town" --sample   # watermarked preview
"""
import argparse, datetime as dt, io, json, os, re, statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import psycopg
from psycopg.rows import dict_row

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, Image, KeepTogether, NextPageTemplate,
                                PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle)

HERE = os.path.dirname(os.path.abspath(__file__))
BANNER = os.path.join(HERE, "brand", "banner.png")
LOGO = os.path.join(HERE, "brand", "logo_on_orange.png")

ORANGE, NAVY = "#FAA73D", "#092934"
BLUE, LIGHT_BLUE, GREY, RED, GREEN = "#3E7CB1", "#9CC3E4", "#B8C2C8", "#D1495B", "#4F8A5B"
PALETTE = [NAVY, ORANGE, BLUE, LIGHT_BLUE, GREEN, GREY, RED]
WEBSITE = "houseshareheroes.co.uk"
SOURCE = "Source: Houseshare Heroes analysis of publicly advertised room listings."

SUB_TOWNS = {"Long Eaton": ("Nottingham", ["NG10"]),
             "Burton upon Trent": ("Derby", ["DE13", "DE14", "DE15"])}
TOWN_SQL = ("case when postcode_district = 'NG10' then 'Long Eaton' "
            "when postcode_district in ('DE13','DE14','DE15') then 'Burton upon Trent' "
            "else search_area end")


def parent_area(town):
    return SUB_TOWNS.get(town, (town,))[0]


# ======================================================================
# Data
# ======================================================================

class Data:
    def __init__(self, conn):
        self.conn = conn

    def q(self, sql, params=None):
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params or {})
            return cur.fetchall()

    def one(self, sql, params=None):
        rows = self.q(sql, params)
        return rows[0] if rows else {}

    def towns(self):
        areas = [r["search_area"] for r in self.q(
            "select search_area from market.area_runs where ad_type='offered' and last_run is not null order by 1")]
        return areas + [t for t, (p, _) in SUB_TOWNS.items() if p in areas]

    def run_info(self, town):
        return self.one("select first_run, last_run from market.area_runs where ad_type='offered' and search_area=%(a)s",
                        {"a": parent_area(town)})

    def base_cte(self):
        return f"""with r as (select last_run from market.area_runs where ad_type='offered' and search_area=%(area)s),
        b as (select l.*, r.last_run,
                ({TOWN_SQL}) as town,
                (l.room_category in ('single','double') and coalesce(l.singles,0)+coalesce(l.doubles,0)=1) as single_room,
                (l.last_seen - l.first_seen) + coalesce(l.days_old_at_first_seen,0) as dom
              from market.offered_listings l, r where l.search_area=%(area)s)"""

    def headline(self, town):
        p = {"area": parent_area(town), "town": town}
        h = self.one(self.base_cte() + """
          select
            count(*) filter (where last_seen = last_run) as live_rooms,
            count(*) filter (where last_seen < last_run and last_seen >= last_run - 30) as lets_30d,
            percentile_cont(0.5) within group (order by dom) filter (where last_seen < last_run and last_seen >= last_run - 182) as median_dtl,
            avg((dom <= 30)::int) filter (where last_seen < last_run and last_seen >= last_run - 182) as pct_30d,
            percentile_cont(0.5) within group (order by rate_pcm) filter (where last_seen = last_run and single_room and room_category='double') as double_pcm,
            percentile_cont(0.5) within group (order by rate_pcm) filter (where last_seen = last_run and single_room and room_category='single') as single_pcm,
            avg(bills_included::int) filter (where last_seen = last_run) as bills_share,
            count(*) filter (where last_seen < last_run and last_seen >= last_run - 182) as lets_6m
          from b where town = %(town)s""", p)
        cut = self.one(self.base_cte() + """
          select count(distinct c.listing_id)::numeric / nullif(count(distinct b.listing_id), 0) as cut_rate,
                 avg(c.change_pct) as avg_cut
          from b left join market.offered_price_changes c
            on c.listing_id = b.listing_id and c.new_rate_pcm < c.old_rate_pcm and c.change_date >= b.last_run - 182
          where b.town = %(town)s and b.single_room and b.last_seen >= b.last_run - 182""", p)
        tpr = self.one("""select max(live_count) filter (where ad_type='wanted' and stat_date >= current_date - 14)::numeric
                   / nullif(avg(live_count) filter (where ad_type='offered' and stat_date >= current_date - 14), 0) as tpr
                 from market.daily_stats where search_area = %(area)s""", p)
        h.update(cut)
        h.update(tpr)
        return h

    def monthly(self, town):
        return self.q(self.base_cte() + """,
          m as (select generate_series(date_trunc('month', (select min(first_seen) from b)),
                                       date_trunc('month', (select max(last_run) from b)), interval '1 month')::date as month)
          select m.month,
            count(b.*) filter (where b.first_seen < m.month + interval '1 month' and b.last_seen >= m.month) as advertised,
            count(b.*) filter (where b.last_seen < b.last_run and b.last_seen >= m.month and b.last_seen < m.month + interval '1 month') as let,
            percentile_cont(0.5) within group (order by b.dom) filter (where b.last_seen < b.last_run and b.last_seen >= m.month and b.last_seen < m.month + interval '1 month') as median_dtl,
            percentile_cont(0.5) within group (order by b.rate_pcm) filter (where b.single_room and b.room_category='double' and b.first_seen < m.month + interval '1 month' and b.last_seen >= m.month) as double_pcm,
            percentile_cont(0.5) within group (order by b.rate_pcm) filter (where b.single_room and b.room_category='single' and b.first_seen < m.month + interval '1 month' and b.last_seen >= m.month) as single_pcm
          from m left join b on b.town = %(town)s
          group by m.month order by m.month""", {"area": parent_area(town), "town": town})[-12:]

    def room_types(self, town):
        order = ["Single", "Double", "Single En Suite", "Double En Suite", "Studio"]
        rows = {r["room_type"]: r for r in self.q("select * from market.room_type_stats where town=%(t)s", {"t": town})}
        return [rows[k] for k in order if k in rows]

    def postcodes(self, town):
        return self.q(self.base_cte() + """
          select postcode_district as district,
            count(*) filter (where last_seen = last_run) as live,
            count(*) filter (where last_seen < last_run and last_seen >= last_run - 182) as lets,
            percentile_cont(0.5) within group (order by rate_pcm) filter (where single_room and last_seen >= last_run - 90) as median_pcm,
            percentile_cont(0.5) within group (order by dom) filter (where last_seen < last_run and last_seen >= last_run - 182) as median_dtl
          from b where town = %(town)s and postcode_district is not null
          group by 1 having count(*) filter (where last_seen >= last_run - 90) >= 5
          order by live desc""", {"area": parent_area(town), "town": town})

    def mix(self, town):
        p = {"area": parent_area(town), "town": town}
        roles = self.q(self.base_cte() + """
          select coalesce(advertiser_role,'unknown') as role, count(*) as n
          from b where town=%(town)s and last_seen = last_run group by 1 order by 2 desc""", p)
        sizes = self.q(self.base_cte() + """
          select least(rooms_in_property, 8) as rooms, count(*) as n
          from b where town=%(town)s and last_seen = last_run and rooms_in_property between 2 and 30
          group by 1 order by 1""", p)
        return roles, sizes

    def agents(self, town):
        return self.one("select * from market.agent_concentration where town=%(t)s", {"t": town})

    def rising(self, town):
        return self.q("select * from market.rising_districts where town=%(t)s order by position_in_city limit 5", {"t": town})

    def tenants(self, town):
        p = {"area": parent_area(town)}
        s = self.one("""
          select count(*) as ads,
            percentile_cont(0.5) within group (order by budget_pcm) filter (where budget_pcm between 100 and 3000) as median_budget,
            avg((rooms_sought ilike 'double%%')::int) as double_only,
            avg(en_suite::int) as en_suite, avg(studio::int) as studio
          from market.wanted_listings where search_area=%(area)s and last_seen >= current_date - 182""", p)
        bands = self.q("""
          select width_bucket(budget_pcm, 300, 900, 6) as band, count(*) as n
          from market.wanted_listings
          where search_area=%(area)s and last_seen >= current_date - 182 and budget_pcm between 100 and 3000
          group by 1 order by 1""", p)
        weekly = self.q("""
          select date_trunc('week', stat_date)::date as week,
            max(live_count) filter (where ad_type='wanted') as tenants,
            round(avg(live_count) filter (where ad_type='offered')) as rooms
          from market.daily_stats where search_area=%(area)s and stat_date >= current_date - 182
          group by 1 order by 1""", p)
        return s, bands, weekly

    def national_benchmarks(self):
        rows = self.q("select median_days_to_let, asking_double_pcm, price_cut_rate from market.city_metrics")
        def med(k):
            v = [float(r[k]) for r in rows if r[k] is not None]
            return statistics.median(v) if v else None
        return {"dtl": med("median_days_to_let"), "double": med("asking_double_pcm"), "cut": med("price_cut_rate"),
                "cities": len(rows)}

    def national(self):
        top = self.q("select * from market.top10_hmo_markets")
        provisional = False
        if len(top) < 10:
            top = self.q("select * from market.city_ranking_provisional limit 10")
            provisional = True
        rising = self.q("select * from market.rising_cities limit 10")
        districts = self.q("select * from market.rising_districts order by national_position limit 10")
        return top, provisional, rising, districts


# ======================================================================
# Charts (matplotlib -> PNG in memory)
# ======================================================================

def _style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color(GREY)
    ax.spines["bottom"].set_color(GREY)
    ax.tick_params(colors="#333333", labelsize=8)
    ax.yaxis.grid(True, color="#E6E9EB", linewidth=0.8)
    ax.set_axisbelow(True)


def _img(fig, width_mm):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    w, h = fig.get_size_inches()
    return Image(buf, width=width_mm * mm, height=width_mm * mm * h / w)


def bar_chart(labels, values, width_mm=170, color=NAVY, money=False, ylabel=None, highlight=None, h=2.6):
    fig, ax = plt.subplots(figsize=(max(3.2, 7 * width_mm / 170), h))
    cols = [ORANGE if highlight is not None and i == highlight else color for i in range(len(values))]
    bars = ax.bar(labels, [v or 0 for v in values], color=cols, width=0.62)
    for b, v in zip(bars, values):
        if v:
            ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"£{v:,.0f}" if money else f"{v:,.0f}",
                    ha="center", va="bottom", fontsize=7.5, color="#333333")
    if money:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"£{x:,.0f}"))
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8, color="#555555")
    _style(ax)
    plt.xticks(rotation=0 if len(labels) <= 8 else 45, ha="center" if len(labels) <= 8 else "right")
    return _img(fig, width_mm)


def line_chart(labels, series, width_mm=170, money=False, ylabel=None, h=2.6):
    fig, ax = plt.subplots(figsize=(7, h))
    for (name, vals), c in zip(series, [NAVY, ORANGE, BLUE, GREEN]):
        xs = [i for i, v in enumerate(vals) if v is not None]
        ys = [vals[i] for i in xs]
        if ys:
            ax.plot(xs, ys, marker="o", markersize=3.5, linewidth=2, color=c, label=name)
    step = max(1, -(-len(labels) // 12))  # at most ~12 labels
    ax.set_xticks(range(0, len(labels), step))
    shown = labels[::step]
    ax.set_xticklabels(shown, rotation=45 if len(shown) > 6 else 0, ha="right" if len(shown) > 6 else "center")
    if money:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"£{x:,.0f}"))
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=8, color="#555555")
    if len(series) > 1:
        ax.legend(frameon=False, fontsize=8, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=len(series))
    _style(ax)
    return _img(fig, width_mm)


def donut(labels, values, width_mm=80, h=2.6):
    fig, ax = plt.subplots(figsize=(3.4, h))
    total = sum(values)
    ax.pie(values, colors=PALETTE[:len(values)], startangle=90, counterclock=False,
           wedgeprops=dict(width=0.38, edgecolor="white"))
    ax.legend([f"{l} ({v / total:.0%})" for l, v in zip(labels, values)], loc="center left",
              bbox_to_anchor=(1, 0.5), frameon=False, fontsize=7.5)
    ax.set_aspect("equal")
    return _img(fig, width_mm)


def hbar(labels, values, width_mm=170, money=False, h=None):
    h = h or max(1.6, 0.32 * len(labels) + 0.4)
    fig, ax = plt.subplots(figsize=(7, h))
    ax.barh(labels[::-1], values[::-1], color=NAVY, height=0.6)
    for i, v in enumerate(values[::-1]):
        ax.text(v, i, f"  £{v:,.0f}" if money else f"  {v:,.0f}", va="center", fontsize=7.5)
    _style(ax)
    ax.xaxis.grid(True, color="#E6E9EB")
    ax.yaxis.grid(False)
    return _img(fig, width_mm)


# ======================================================================
# PDF building blocks
# ======================================================================

ST = {
    "town": ParagraphStyle("town", fontName="Helvetica-Bold", fontSize=34, leading=40, textColor=colors.HexColor(ORANGE), alignment=TA_CENTER),
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=22, leading=27, textColor=colors.HexColor(NAVY), alignment=TA_CENTER),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=12, leading=16, textColor=colors.HexColor("#555555"), alignment=TA_CENTER),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=colors.HexColor(NAVY), spaceBefore=4, spaceAfter=6),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=colors.HexColor(BLUE), spaceBefore=8, spaceAfter=3),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=colors.HexColor("#222222"), spaceAfter=5),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=7.5, leading=10, textColor=colors.HexColor("#666666")),
    "kpi_v": ParagraphStyle("kpi_v", fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=colors.HexColor(NAVY), alignment=TA_CENTER),
    "kpi_l": ParagraphStyle("kpi_l", fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#555555"), alignment=TA_CENTER),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=11),
    "cta": ParagraphStyle("cta", fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=colors.white, alignment=TA_CENTER),
    "cta_s": ParagraphStyle("cta_s", fontName="Helvetica", fontSize=9.5, leading=13, textColor=colors.white, alignment=TA_CENTER),
}


def money(v):
    return f"£{float(v):,.0f}" if v is not None else "–"


def pct(v, dp=0):
    return f"{float(v) * 100:.{dp}f}%" if v is not None else "–"


def num(v, dp=0, suffix=""):
    return f"{float(v):,.{dp}f}{suffix}" if v is not None else "–"


def kpi_tiles(items):
    cells = [[Paragraph(v, ST["kpi_v"]), ] for v, _ in items]
    row_v = [Paragraph(v, ST["kpi_v"]) for v, _ in items]
    row_l = [Paragraph(l, ST["kpi_l"]) for _, l in items]
    t = Table([row_v, row_l], colWidths=[170 * mm / len(items)] * len(items))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF4E5")),
        ("LINEABOVE", (0, 0), (-1, 0), 3, colors.HexColor(ORANGE)),
        ("INNERGRID", (0, 0), (-1, -1), 0, colors.white),
        ("BOX", (0, 0), (-1, -1), 0, colors.white),
        ("LINEAFTER", (0, 0), (-2, -1), 4, colors.white),
        ("TOPPADDING", (0, 0), (-1, 0), 9), ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def data_table(header, rows, widths, align_right_from=1):
    data = [[Paragraph(f"<b>{h}</b>", ST["cell"]) for h in header]] + \
           [[Paragraph(str(c), ST["cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF3")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9FA")]),
        ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor(NAVY)),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def building(first_run, what="This section"):
    when = first_run.strftime("%-d %B %Y") if first_run else "recently"
    t = Table([[Paragraph(f"<b>Data still building.</b> {what} will appear in future editions once enough daily "
                          f"data has been collected (tracking began {when}).", ST["body"])]],
              colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F2F4F5")),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def cta_box():
    t = Table([[Paragraph("Thinking about an HMO in this area?", ST["cta"])],
               [Paragraph(f"Houseshare Heroes manage HMOs across the Midlands – from finding tenants to "
                          f"compliance and maintenance. Get a free rental appraisal at <b>{WEBSITE}</b>", ST["cta_s"])]],
              colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY)),
                           ("LINEABOVE", (0, 0), (-1, 0), 4, colors.HexColor(ORANGE)),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))
    return KeepTogether([t])


class ReportDoc(BaseDocTemplate):
    def __init__(self, path, title, edition, sample=False):
        super().__init__(path, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                         topMargin=30 * mm, bottomMargin=18 * mm, title=title,
                         author="Houseshare Heroes", subject=f"{title} – {edition}")
        self.edition, self.sample, self.report_title = edition, sample, title
        cover = Frame(20 * mm, 18 * mm, 170 * mm, 205 * mm, id="cover")
        body = Frame(20 * mm, 16 * mm, 170 * mm, 250 * mm, id="body")
        self.addPageTemplates([PageTemplate("cover", [cover], onPage=self._cover),
                               PageTemplate("body", [body], onPage=self._body)])

    def _watermark(self, c):
        if self.sample:
            c.saveState()
            c.setFont("Helvetica-Bold", 44)
            c.setFillColor(colors.Color(0.82, 0.29, 0.36, alpha=0.13))
            c.translate(105 * mm, 148 * mm)
            c.rotate(40)
            c.drawCentredString(0, 0, "SAMPLE – ILLUSTRATIVE DATA")
            c.restoreState()

    def _footer(self, c):
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#777777"))
        c.drawString(20 * mm, 10 * mm, f"{self.report_title} · {self.edition} · {WEBSITE}")
        c.drawRightString(190 * mm, 10 * mm, f"Page {c.getPageNumber()}")

    def _cover(self, c, doc):
        c.drawImage(BANNER, 0, A4[1] - 64 * mm, width=A4[0], height=A4[0] * 364 / 1200, mask="auto")
        self._watermark(c)
        self._footer(c)

    def _body(self, c, doc):
        band = 18 * mm
        c.setFillColor(colors.HexColor(ORANGE))
        c.rect(0, A4[1] - band, A4[0], band, fill=1, stroke=0)
        c.drawImage(LOGO, 16 * mm, A4[1] - band + 1.5 * mm, width=15 * mm * 550 / 194, height=15 * mm, mask="auto")
        c.setFillColor(colors.HexColor(NAVY))
        c.rect(0, A4[1] - band - 5 * mm, A4[0], 5 * mm, fill=1, stroke=0)
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(190 * mm, A4[1] - band / 2 - 1.5 * mm, self.report_title)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(20 * mm, A4[1] - band - 3.4 * mm, "HMO PROPERTY MANAGEMENT SPECIALISTS")
        c.drawRightString(190 * mm, A4[1] - band - 3.4 * mm, self.edition.upper())
        self._watermark(c)
        self._footer(c)


# ======================================================================
# Commentary (plain-English takeaways, rule-based)
# ======================================================================

def takeaways(town, h, bench, monthly, area_name):
    out = []
    if h.get("median_dtl") is not None and bench.get("dtl"):
        d, n = float(h["median_dtl"]), bench["dtl"]
        faster = "faster than" if d < n * 0.95 else "slower than" if d > n * 1.05 else "in line with"
        out.append(f"Rooms in {town} took a median of <b>{d:.0f} days</b> to let over the last six months – "
                   f"{faster} the {bench['cities']}-city median of {n:.0f} days.")
    if h.get("double_pcm") is not None and bench.get("double"):
        diff = float(h["double_pcm"]) - bench["double"]
        out.append(f"A typical double room is advertised at <b>{money(h['double_pcm'])} a month</b>, "
                   f"{money(abs(diff))} {'above' if diff >= 0 else 'below'} the national median across the cities we track.")
    rents = [m["double_pcm"] for m in monthly if m["double_pcm"] is not None]
    if len(rents) >= 4:
        change = float(rents[-1]) / float(rents[0]) - 1
        out.append(f"Double room rents have moved <b>{change * 100:+.1f}%</b> over the period shown.")
    if h.get("cut_rate") is not None and h.get("lets_6m", 0) >= 30:
        out.append(f"<b>{pct(h['cut_rate'])}</b> of rooms had their price reduced while advertised"
                   + (f", by {abs(float(h['avg_cut'])):.0f}% on average." if h.get("avg_cut") else "."))
    if h.get("tpr") is not None:
        out.append(f"There are around <b>{float(h['tpr']):.2f} tenant 'room wanted' adverts for every room advertised</b> "
                   f"in the wider {area_name} area.")
    return out


# ======================================================================
# Town report
# ======================================================================

def town_report(db, town, path, edition, label=None, sample=False, bench=None):
    name = label or town
    info = db.run_info(town)
    first_run = info.get("first_run")
    h = db.headline(town)
    monthly = db.monthly(town)
    bench = bench or db.national_benchmarks()
    have_lets = (h.get("lets_6m") or 0) >= 30
    area_name = label or parent_area(town)

    doc = ReportDoc(path, f"{name} HMO Market Report", edition, sample)
    s = [NextPageTemplate("body")]

    # ---- cover
    s += [Spacer(1, 14 * mm), Paragraph(name, ST["town"]), Paragraph("HMO Lettings &amp; Market Report", ST["title"]), Spacer(1, 3 * mm),
          Paragraph(f"{edition} edition · rooms to rent, rents, demand and trends", ST["subtitle"]), Spacer(1, 10 * mm)]
    s.append(kpi_tiles([
        (money(h.get("double_pcm")), "Typical double room<br/>(per month)"),
        (money(h.get("single_pcm")), "Typical single room<br/>(per month)"),
        (num(h.get("median_dtl"), 0, " days") if have_lets else "–", "Median time<br/>to let"),
        (num(h.get("live_rooms")), "Rooms advertised<br/>right now"),
    ]))
    s += [Spacer(1, 8 * mm), Paragraph("Key takeaways", ST["h2"])]
    tk = takeaways(name, h, bench, monthly, area_name) if have_lets or h.get("double_pcm") else []
    for line in tk or ["Key takeaways will appear once enough data has been collected."]:
        s.append(Paragraph("• " + line, ST["body"]))
    s += [Spacer(1, 6 * mm), cta_box(), Spacer(1, 5 * mm),
          Paragraph("How to read this report: 'typical' figures are medians – the middle value – which are not "
                    "skewed by a handful of unusually cheap or expensive rooms. 'Let' means the advert came down "
                    "after being live, the best available signal that a room found a tenant. " + SOURCE, ST["small"]),
          PageBreak()]

    # ---- rents
    s.append(Paragraph("Rents", ST["h1"]))
    rt = db.room_types(town)
    if rt:
        s.append(Paragraph("Typical asking rent by room type", ST["h2"]))
        s.append(bar_chart([r["room_type"] for r in rt],
                           [float(r["median_asking_pcm"]) if r["median_asking_pcm"] else None for r in rt],
                           money=True, color=NAVY, h=2.3))
        s.append(data_table(["Room type", "Rooms advertised", "Share", "Typical asking", "Typical let price", "Days to let"],
                            [[r["room_type"], num(r["live_rooms"]), pct(r["share_of_live"]), money(r["median_asking_pcm"]),
                              money(r["median_let_pcm"]), num(r["median_days_to_let"], 0) if r["lets_6m"] and r["lets_6m"] >= 10 else "–"]
                             for r in rt], [40, 28, 20, 28, 30, 24]))
    labels = [m["month"].strftime("%b %y") for m in monthly]
    if len(monthly) >= 3:
        s.append(Paragraph("Rents over time", ST["h2"]))
        s.append(line_chart(labels, [("Double", [float(m["double_pcm"]) if m["double_pcm"] else None for m in monthly]),
                                     ("Single", [float(m["single_pcm"]) if m["single_pcm"] else None for m in monthly])],
                            money=True, h=2.3))
    else:
        s.append(building(first_run, "The rents-over-time chart"))

    # ---- postcodes
    pcs = db.postcodes(town)
    if pcs:
        s += [CondPageBreak(120 * mm), Paragraph("Postcode by postcode", ST["h1"]),
              Paragraph("Typical rent by postcode district (single-room adverts, last 3 months)", ST["h2"])]
        top = [p for p in pcs if p["median_pcm"]][:14]
        top.sort(key=lambda p: -float(p["median_pcm"]))
        s.append(hbar([p["district"] for p in top], [float(p["median_pcm"]) for p in top], money=True))
        s.append(data_table(["District", "Rooms advertised", "Lets (6 months)", "Typical rent", "Days to let"],
                            [[p["district"], num(p["live"]), num(p["lets"]), money(p["median_pcm"]),
                              num(p["median_dtl"]) if p["lets"] >= 10 else "–"] for p in pcs[:16]],
                            [30, 35, 35, 35, 35]))

    # ---- demand and speed
    s += [CondPageBreak(120 * mm), Paragraph("Demand and speed of letting", ST["h1"])]
    if have_lets and len(monthly) >= 2:
        s.append(Paragraph("Rooms advertised and rooms let each month", ST["h2"]))
        s.append(line_chart(labels, [("Advertised", [m["advertised"] for m in monthly]),
                                     ("Let", [m["let"] for m in monthly])], h=2.3))
        s.append(Paragraph("Median days to let, by month", ST["h2"]))
        s.append(bar_chart(labels, [float(m["median_dtl"]) if m["median_dtl"] else None for m in monthly], h=2.2))
        s.append(Paragraph(f"Over the last six months {pct(h.get('pct_30d'))} of rooms let within 30 days, and "
                           f"{pct(h.get('cut_rate'))} had a price reduction while advertised.", ST["body"]))
    else:
        s.append(building(first_run, "Time-to-let and monthly letting figures"))

    tstats, bands, weekly = db.tenants(town)
    s.append(CondPageBreak(70 * mm))
    s.append(Paragraph(f"Tenants looking for rooms in the {area_name} area", ST["h2"]))
    if tstats.get("ads"):
        if len(weekly) >= 3 and any(w["tenants"] for w in weekly):
            s.append(line_chart([w["week"].strftime("%d %b") for w in weekly],
                                [("Tenants looking", [w["tenants"] for w in weekly]),
                                 ("Rooms advertised", [float(w["rooms"]) if w["rooms"] else None for w in weekly])], h=2.2))
        band_labels = ["< £300", "£300–400", "£400–500", "£500–600", "£600–700", "£700–800", "£800–900", "£900+"]
        counts = {b["band"]: b["n"] for b in bands}
        s.append(KeepTogether([Paragraph("Tenant budgets (per month)", ST["h2"]),
                               bar_chart(band_labels, [counts.get(i, 0) for i in range(8)], color=BLUE, h=2.0)]))
        s.append(Paragraph(f"The typical tenant budget is <b>{money(tstats.get('median_budget'))}</b> a month. "
                           f"{pct(tstats.get('en_suite'))} of tenants mention wanting an en-suite and "
                           f"{pct(tstats.get('studio'))} a studio.", ST["body"]))
    else:
        s.append(building(first_run, "Tenant demand"))

    # ---- market make-up
    roles, sizes = db.mix(town)
    s += [CondPageBreak(110 * mm), Paragraph("Who is letting rooms", ST["h1"])]
    if roles:
        names = {"agent": "Letting agents", "live out landlord": "Landlords (not resident)",
                 "live in landlord": "Resident landlords", "current flatmate": "Current flatmates",
                 "former flatmate": "Outgoing flatmates", "current tenants": "Current tenants"}
        grouped = {}
        for r in roles:
            k = names.get(r["role"], "Other")
            grouped[k] = grouped.get(k, 0) + r["n"]
        items = sorted(grouped.items(), key=lambda x: -x[1])
        row = [donut([k for k, _ in items], [v for _, v in items], width_mm=95)]
        if sizes:
            row.append(bar_chart([f"{r['rooms']}{'+' if r['rooms'] == 8 else ''}" for r in sizes],
                                 [r["n"] for r in sizes], width_mm=75, color=BLUE, h=2.6))
        t = Table([[Paragraph("Advertiser type", ST["h2"]), Paragraph("Bedrooms in the house", ST["h2"]) if sizes else ""],
                   row], colWidths=[95 * mm, 75 * mm])
        s.append(t)
    ag = db.agents(town)
    if ag and ag.get("active_agents"):
        s.append(Paragraph("Letting agent market", ST["h2"]))
        s.append(Paragraph(f"<b>{num(ag['active_agents'])}</b> letting agents are currently advertising rooms, "
                           f"accounting for <b>{pct(ag['agent_share_of_market'])}</b> of rooms on the market. "
                           f"The three largest agents list <b>{pct(ag['top3_share'])}</b> of agent-advertised rooms "
                           f"– a {'fragmented' if (ag['hhi'] or 0) < 1500 else 'moderately concentrated' if (ag['hhi'] or 0) < 2500 else 'concentrated'} market.",
                           ST["body"]))
    if h.get("bills_share") is not None:
        s.append(Paragraph(f"{pct(h['bills_share'])} of rooms are advertised with bills included.", ST["body"]))

    # ---- rising areas
    ris = db.rising(town)
    s.append(Paragraph(f"Rising areas in {name}", ST["h2"]))
    if ris:
        s.append(Paragraph("Postcode districts where rents and letting speed have improved most over the last three "
                           "months compared with the three before.", ST["body"]))
        s.append(data_table(["District", "Rent before", "Rent now", "Days to let before", "Days to let now"],
                            [[r["postcode_district"], money(r["let_rent_before"]), money(r["let_rent_now"]),
                              num(r["days_to_let_before"]), num(r["days_to_let_now"])] for r in ris],
                            [30, 35, 35, 35, 35]))
    else:
        s.append(building(first_run, "Rising areas (which compare two three-month periods)"))

    # ---- method
    s += [CondPageBreak(75 * mm), Spacer(1, 6 * mm), cta_box(), Spacer(1, 5 * mm), Paragraph("About this report", ST["h2"]),
          Paragraph("Houseshare Heroes tracks publicly advertised rooms to rent every day. Each advert is followed from "
                    "the day it appears until it comes down; this gives time on market, price changes and lets. "
                    "Typical figures are medians. Weekly rents are converted to monthly (x52/12). Adverts covering "
                    "several rooms are excluded from room-type prices. En-suite and studio rooms are identified from "
                    "the advert headline and summary, so may be slightly under-counted. Tenant demand counts people "
                    "who have posted a 'room wanted' advert, so understates total demand; it is best used to compare "
                    "areas and track changes over time. " + SOURCE +
                    " This report is general market information, not financial advice.", ST["small"])]
    doc.build(s)


# ======================================================================
# National report
# ======================================================================

def national_report(db, path, edition, sample=False):
    top, provisional, rising, districts = db.national()
    doc = ReportDoc(path, "UK HMO Markets Report", edition, sample)
    s = [NextPageTemplate("body"), Spacer(1, 8 * mm),
         Paragraph("The UK's Top HMO Markets", ST["title"]), Spacer(1, 3 * mm),
         Paragraph(f"{edition} · where demand, rents and letting speed are strongest", ST["subtitle"]),
         Spacer(1, 4 * mm)]
    if top:
        s.append(Paragraph("Top 10 HMO markets" + (" (provisional – data still building)" if provisional else ""), ST["h2"]))
        s.append(hbar([r["city"] for r in top], [float(r["opportunity_score"]) for r in top], width_mm=140, h=1.7))
        s.append(data_table(["#", "City", "Score", "Tenant demand", "Ease of letting", "Rent growth", "Investor momentum"],
                            [[r["position"], r["city"], num(r["opportunity_score"], 1), num(r["tenant_demand"]),
                              num(r["ease_of_letting"]), num(r["rent_growth"]), num(r["investor_momentum"])] for r in top],
                            [10, 38, 18, 26, 26, 24, 28]))
        s.append(Paragraph("Scores are 0–100, relative to the other cities we track.", ST["small"]))
    s += [Spacer(1, 3 * mm), cta_box(), PageBreak(), Paragraph("Up-and-coming markets", ST["h1"])]
    if rising:
        s.append(Paragraph("Top 10 rising cities", ST["h2"]))
        s.append(Paragraph("Ranked purely on direction of travel: rents, tenant demand and letting speed over the last "
                           "three months compared with the three before.", ST["body"]))
        s.append(data_table(["#", "City", "Rising score", "Rent change", "Tenant demand change", "Days to let change"],
                            [[r["position"], r["city"], num(r["rising_score"], 1), pct(r["achieved_rent_growth"], 1),
                              pct(r["wanted_growth"]), num(r["days_to_let_trend"], 0, " days")] for r in rising],
                            [10, 40, 25, 28, 37, 30]))
    else:
        s.append(building(None, "The rising-cities ranking"))
    if districts:
        s.append(Paragraph("Top 10 up-and-coming postcode districts", ST["h2"]))
        s.append(data_table(["#", "District", "City", "Rent before", "Rent now", "Days to let now"],
                            [[r["national_position"], r["postcode_district"], r["town"], money(r["let_rent_before"]),
                              money(r["let_rent_now"]), num(r["days_to_let_now"])] for r in districts],
                            [10, 28, 42, 30, 30, 30]))
    s += [Spacer(1, 3 * mm), Paragraph("How the ranking works", ST["h2"]),
          Paragraph("Each city is scored on five areas: tenant demand (tenants looking per room and how fast demand is "
                    "growing), ease of letting (days to let, stale adverts, price cuts), rent growth (achieved and asking "
                    "rents, rises versus cuts, tenant budget headroom), investor momentum (new supply, landlord and HMO "
                    "trends) and management opportunity (self-managing landlords, agent competition and market size). "
                    "Cities need several months of data and a minimum market size to qualify. " + SOURCE +
                    " General market information, not financial advice.", ST["small"])]
    doc.build(s)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--town", action="append")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--national", action="store_true")
    ap.add_argument("--label", help="display name override (for samples)")
    ap.add_argument("--sample", action="store_true", help="add SAMPLE watermark")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()

    edition = dt.date.today().strftime("%B %Y")
    folder = os.path.join(args.out, dt.date.today().strftime("%Y-%m"))
    os.makedirs(folder, exist_ok=True)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        db = Data(conn)
        bench = db.national_benchmarks()
        towns = db.towns() if args.all else (args.town or [])
        manifest = []   # read by wix_publish.py
        for t in towns:
            name = args.label or t
            path = os.path.join(folder, f"{slug(name)}-hmo-market-report-{dt.date.today():%Y-%m}.pdf")
            town_report(db, t, path, edition, label=args.label, sample=args.sample, bench=bench)
            manifest.append({"area": name, "file": os.path.basename(path), "national": False})
            print("wrote", path)
        if args.all or args.national:
            path = os.path.join(folder, f"uk-top-hmo-markets-{dt.date.today():%Y-%m}.pdf")
            national_report(db, path, edition, sample=args.sample)
            manifest.append({"area": "UK Top HMO Markets", "file": os.path.basename(path), "national": True})
            print("wrote", path)
    with open(os.path.join(folder, "manifest.json"), "w") as f:
        json.dump({"edition": edition, "reports": manifest}, f, indent=2)


if __name__ == "__main__":
    main()
