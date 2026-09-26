"""Publish the monthly PDF reports to the Houseshare Heroes Wix site.

For every PDF listed in reports/<YYYY-MM>/manifest.json (written by report.py):
  1. upload it to the Wix Media Manager, in a "Market Reports" folder
  2. upsert one row per town in the Wix CMS collection "MarketReports", so the row
     always points at the latest edition. Bind a Wix page (repeater / dataset) or a
     lead-capture form's thank-you download button to that collection.

Needs two GitHub secrets:
  WIX_API_KEY  - Wix API key with "Manage Media Manager" and "Wix Data" permissions
  WIX_SITE_ID  - the site's ID (the GUID after /dashboard/ in the Wix dashboard URL)
If either is missing the script exits quietly, so the monthly workflow still succeeds.

    python wix_publish.py                 # latest month in reports/
    python wix_publish.py --month 2026-10
    python wix_publish.py --dry-run
"""
import argparse, datetime as dt, glob, json, os, re, sys, time

import requests

API = "https://www.wixapis.com"
COLLECTION = "MarketReports"
FOLDER_NAME = "Market Reports"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class Wix:
    def __init__(self, api_key, site_id):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": api_key, "wix-site-id": site_id,
                               "Content-Type": "application/json"})

    def call(self, method, path, ok=(200,), **kw):
        for attempt in range(4):
            r = self.s.request(method, API + path, timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 3)
                continue
            break
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    # --- media -------------------------------------------------------------
    def folder_id(self):
        res = self.call("GET", "/site-media/v1/folders", params={"parentFolderId": "media-root"})
        for f in res.get("folders", []):
            if f.get("displayName") == FOLDER_NAME:
                return f["id"]
        res = self.call("POST", "/site-media/v1/folders",
                        json={"displayName": FOLDER_NAME, "parentFolderId": "media-root"})
        return res["folder"]["id"]

    def upload_pdf(self, path, folder_id):
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        res = self.call("POST", "/site-media/v1/files/generate-upload-url", json={
            "mimeType": "application/pdf", "fileName": name,
            "sizeInBytes": str(len(data)), "parentFolderId": folder_id})
        up = requests.put(res["uploadUrl"], params={"filename": name}, data=data,
                          headers={"Content-Type": "application/pdf"}, timeout=120)
        if up.status_code != 200:
            raise RuntimeError(f"upload {name} -> {up.status_code}: {up.text[:500]}")
        return up.json()["file"]

    # --- CMS ---------------------------------------------------------------
    def ensure_collection(self):
        r = self.s.get(f"{API}/wix-data/v2/collections/{COLLECTION}", timeout=60)
        if r.status_code == 200:
            return
        if r.status_code != 404:
            raise RuntimeError(f"get collection -> {r.status_code}: {r.text[:500]}")
        self.call("POST", "/wix-data/v2/collections", json={"collection": {
            "id": COLLECTION,
            "displayName": "Market Reports",
            "fields": [
                {"key": "title", "displayName": "Title", "type": "TEXT"},
                {"key": "area", "displayName": "Area", "type": "TEXT"},
                {"key": "edition", "displayName": "Edition", "type": "TEXT"},
                {"key": "pdfUrl", "displayName": "PDF link", "type": "URL"},
                {"key": "national", "displayName": "National report", "type": "BOOLEAN"},
                {"key": "published", "displayName": "Published", "type": "DATE"},
            ],
            "permissions": {"read": "ANYONE", "insert": "ADMIN", "update": "ADMIN", "remove": "ADMIN"},
        }})
        print("created CMS collection", COLLECTION)

    def upsert(self, item_id, data):
        self.call("POST", "/wix-data/v2/items/save", json={
            "dataCollectionId": COLLECTION, "dataItem": {"id": item_id, "data": data}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM folder under reports/ (default: latest)")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    months = sorted(os.path.basename(p) for p in glob.glob(os.path.join(args.reports, "????-??")))
    month = args.month or (months[-1] if months else None)
    manifest_path = os.path.join(args.reports, month or "", "manifest.json")
    if not month or not os.path.exists(manifest_path):
        sys.exit(f"no manifest found at {manifest_path}; run report.py first")
    manifest = json.load(open(manifest_path))
    folder = os.path.dirname(manifest_path)

    key, site = os.environ.get("WIX_API_KEY"), os.environ.get("WIX_SITE_ID")
    if args.dry_run or not (key and site):
        if not args.dry_run:
            print("WIX_API_KEY / WIX_SITE_ID not set - skipping Wix publish.")
        for r in manifest["reports"]:
            print("would publish", r["area"], "->", r["file"])
        return

    wix = Wix(key, site)
    wix.ensure_collection()
    fid = wix.folder_id()
    today = dt.date.today().isoformat()
    for r in manifest["reports"]:
        f = wix.upload_pdf(os.path.join(folder, r["file"]), fid)
        title = r["area"] if r["national"] else f"{r['area']} HMO Market Report"
        wix.upsert(slug(r["area"]), {
            "title": title, "area": r["area"], "edition": manifest["edition"],
            "pdfUrl": f.get("url"), "national": r["national"],
            "published": today,
        })
        print("published", title, "->", f.get("url"))


if __name__ == "__main__":
    main()
