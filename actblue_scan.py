#!/usr/bin/env python3
"""
ActBlue IL "Local Executive" directory scanner, Chicago filter.

Two modes:
  --once     Pull every page now and print/export the full current Chicago list.
             This is the "give me the complete list right now" mode.
  (default)  Pull, filter to Chicago, and diff against a stored snapshot so a
             scheduled run only reports Chicago names that are NEW since last time.

Why this works when hand-paging didn't: the ?page=N pagination returns real,
distinct results to a normal HTTP client with browser-like headers. The parser
targets stable content patterns (the /donate/<slug>?refcode=directory links, the
"<Place>-Exec Coun-<n>" jurisdiction labels, and the displayed cycle year), not
fragile CSS classes, so it survives markup tweaks.

Notes / honest limits baked in:
  * ActBlue only lists candidates who use ActBlue AND opt into the public
    directory, so this skews Democratic and is not a full field.
  * The "Exec Coun-<n>" suffix is the ward number for Chicago entries.
  * Year tags on the directory are sometimes stale (e.g. an incumbent tagged to
    an old cycle), so CYCLE_FILTER defaults to off; filter yourself if you want.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time

BASE = "https://secure.actblue.com/directory/IL/all/local-exec"
TOTAL_PAGES_DEFAULT = 21
CITY_FILTER = "chicago"          # matched case-insensitively in the jurisdiction
CYCLE_FILTER = None              # e.g. "2027" to keep only that year; None = all
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}

# --- stable extraction patterns ------------------------------------------------
DONATE_RE = re.compile(
    r'href="https://secure\.actblue\.com/donate/([^"?]+)\?refcode=directory"'
    r'[^>]*?title="Contribute to ([^"]+)"',
    re.I,
)
TAG_RE = re.compile(r'<[^>]+>')
YEAR_RE = re.compile(r'\b(20\d\d)\b')
# Constant directory chrome that can bleed into the text before an entry.
BOILER = ("Donate now", "Create contribution form", "Go to website")


def _plain(s):
    return re.sub(r'\s+', ' ', html.unescape(TAG_RE.sub(' ', s))).strip()


def _trim_to_content(page_html):
    """Drop the footer/pagination so stray years/text don't pollute parsing."""
    for marker in ("← Previous", "Previous</a", "The Organization"):
        idx = page_html.find(marker)
        if idx > 2000:  # keep the body, cut the tail
            return page_html[:idx]
    return page_html


def parse_page(page_html):
    """Return (rows, counts). Each entry is anchored on its donate link; the
    jurisdiction and cycle year are read from the text just before it, so any
    office label (mayor, clerk, treasurer, or 'Exec Coun-<ward>') parses."""
    body = _trim_to_content(page_html)
    # cut the directory header so the first entry isn't polluted by nav text
    cpos = body.find("/directory/IL/candidate/")
    if cpos != -1:
        end = body.find("</a>", cpos)
        if end != -1:
            body = body[end + 4:]

    rows, prev_end = [], 0
    matches = list(DONATE_RE.finditer(body))
    for m in matches:
        name = html.unescape(m.group(2)).strip()
        slug = m.group(1).strip()
        pre = _plain(body[prev_end:m.start()])
        ym = YEAR_RE.search(pre)
        juris = pre[:ym.start()] if ym else pre
        year = ym.group(1) if ym else ""
        for ph in BOILER:
            juris = juris.replace(ph, " ")
        juris = re.sub(r'\s+', ' ', juris).strip()
        rows.append({
            "name": name, "slug": slug, "jurisdiction": juris, "year": year,
            "donate_url": f"https://secure.actblue.com/donate/{slug}?refcode=directory",
        })
        prev_end = m.end()

    counts = {"donates": len(matches),
              "juris": sum(1 for r in rows if r["jurisdiction"]),
              "years": sum(1 for r in rows if r["year"])}
    return rows, counts


def is_chicago(row):
    if CITY_FILTER not in row["jurisdiction"].lower():
        return False
    if CYCLE_FILTER and row["year"] != CYCLE_FILTER:
        return False
    return True


def ward_of(jurisdiction):
    m = re.search(r"-Exec Coun-(\d+)", jurisdiction)
    return int(m.group(1)) if m else None


# Known incumbents (by donate slug) so the JSON output can label pills.
# Extend as you confirm more; everything else is treated as a challenger.
INCUMBENTS = {
    "neighbors-for-daniel-la-spata-1",
    "brian-hopkins-1",
    "citizens-for-pat-dowell-1",
    "lamont-robinson-for-alderman-1",
}


def _pages_html(total_pages, sleep, fixture_dir):
    """Yield the raw HTML of each directory page (live) or fixture files (test)."""
    if fixture_dir:
        import glob
        for path in sorted(glob.glob(os.path.join(fixture_dir, "*.html"))):
            with open(path, encoding="utf-8") as f:
                yield f.read()
        return
    import requests
    for p in range(1, total_pages + 1):
        r = requests.get(f"{BASE}?page={p}", headers=HEADERS, timeout=60)
        r.raise_for_status()
        yield r.text
        time.sleep(sleep)


def collect_rows(total_pages=TOTAL_PAGES_DEFAULT, sleep=1.0, fixture_dir=None):
    all_rows, seen_slugs = [], set()
    for page_html in _pages_html(total_pages, sleep, fixture_dir):
        rows, counts = parse_page(page_html)
        if counts["donates"] != counts["juris"]:
            print(f"  [warn] page token counts differ {counts}", file=sys.stderr)
        for row in rows:
            if row["slug"] in seen_slugs:   # boundary rows repeat across pages
                continue
            seen_slugs.add(row["slug"])
            all_rows.append(row)
    return all_rows


# backward-compatible alias
def fetch_all(total_pages):
    return collect_rows(total_pages=total_pages)


def classify(jurisdiction):
    """Return (office, ward). Chicago aldermanic entries tag as
    'Chicago-Exec Coun-<ward>'; mayor/clerk/treasurer tag by name."""
    j = jurisdiction.lower()
    if "mayor" in j:     return ("mayor", None)
    if "clerk" in j:     return ("clerk", None)
    if "treasurer" in j: return ("treasurer", None)
    m = re.search(r"-exec coun-(\d+)", j)
    if m:                return ("alderman", int(m.group(1)))
    return ("other", None)


def to_page_json(rows):
    """Map scanner rows to the exact shape the directory page reads.
    status is only asserted for known incumbents; everything else is left
    blank rather than guessed, so the page shows a neutral tag."""
    out = []
    for r in rows:
        office, ward = classify(r["jurisdiction"])
        yr = r["year"]
        out.append({
            "name": r["name"],
            "office": office,
            "ward": ward,
            "cycle": int(yr) if yr.isdigit() else None,
            "status": "incumbent" if r["slug"] in INCUMBENTS else "",
            "site": r.get("site", ""),
            "donate": r["slug"],
            "stale": bool(yr) and yr != "2027",
            "jurisdiction": r["jurisdiction"],
        })
    return out


def chicago_rows(all_rows):
    rows = [r for r in all_rows if is_chicago(r)]
    rows.sort(key=lambda r: (ward_of(r["jurisdiction"]) or 999, r["name"]))
    return rows


def load_snapshot(path):
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f).get("seen_slugs", []))
    return set()


def save_snapshot(path, slugs):
    with open(path, "w") as f:
        json.dump({"seen_slugs": sorted(slugs),
                   "updated": dt.datetime.now(dt.timezone.utc).isoformat()}, f, indent=2)


def to_markdown(rows, title):
    if not rows:
        return ""
    out = [f"# {title} ({dt.date.today()})", "", f"{len(rows)} committees", ""]
    for r in rows:
        w = ward_of(r["jurisdiction"])
        ward = f"Ward {w}" if w else r["jurisdiction"]
        yr = f" · {r['year']}" if r["year"] else ""
        out.append(f"- **{r['name']}** — {ward}{yr}  \n  {r['donate_url']}")
    return "\n".join(out) + "\n"


def to_csv(rows):
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "ward", "jurisdiction", "year", "slug", "donate_url"])
    for r in rows:
        w.writerow([r["name"], ward_of(r["jurisdiction"]) or "", r["jurisdiction"],
                    r["year"], r["slug"], r["donate_url"]])
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="Print/export full current Chicago list, no diffing.")
    ap.add_argument("--pages", type=int, default=TOTAL_PAGES_DEFAULT)
    ap.add_argument("--snapshot", default="actblue_seen.json")
    ap.add_argument("--out", default="actblue_new.md")
    ap.add_argument("--csv", default="", help="Optional path to also write a CSV of the full Chicago list.")
    ap.add_argument("--json", default="", help="Optional path to write the Chicago list as JSON in the web page's shape.")
    ap.add_argument("--fixture", default="", help="Parse a local HTML file instead of the network (for testing).")
    args = ap.parse_args()

    if args.fixture:
        with open(args.fixture) as f:
            rows, counts = parse_page(f.read())
        print(f"[fixture] parsed {counts}")
        all_rows = rows
    else:
        all_rows = fetch_all(args.pages)

    chi = chicago_rows(all_rows)

    if args.csv:
        with open(args.csv, "w") as f:
            f.write(to_csv(chi))

    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_page_json(chi), f, indent=2)

    if args.once:
        md = to_markdown(chi, "Chicago ActBlue committees")
        with open(args.out, "w") as f:
            f.write(md)
        print(f"Full current Chicago list: {len(chi)} committees "
              f"(across {len(all_rows)} IL local-exec entries).")
        for r in chi:
            print(f"  W{ward_of(r['jurisdiction']) or '?':<2} {r['year'] or '----'}  {r['name']}")
        return

    # diff mode
    seen = load_snapshot(args.snapshot)
    new = [r for r in chi if r["slug"] not in seen]
    with open(args.out, "w") as f:
        f.write(to_markdown(new, "New Chicago ActBlue committees") if new else "")
    save_snapshot(args.snapshot, seen | {r["slug"] for r in chi})
    print(f"{len(chi)} Chicago committees; {len(new)} new since last run.")
    for r in new:
        print(f"  NEW  W{ward_of(r['jurisdiction']) or '?'}  {r['name']}")


if __name__ == "__main__":
    main()
