import csv, io, os, re, uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template_string, request

app = Flask(__name__)
API_KEY = os.environ.get("GOOGLE_API_KEY", "")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
BAD_END = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeadFinder/1.0)"}
PATHS = ["", "/contact", "/contact-us", "/about"]
CACHE = {}  # result_id -> rows (in memory, resets on restart)


def search_places(query, max_pages=3):
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,"
            "places.nationalPhoneNumber,places.websiteUri,nextPageToken"
        ),
    }
    body = {"textQuery": query, "pageSize": 20}
    out = []
    for _ in range(max_pages):
        r = requests.post(url, json=body, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        out += data.get("places", [])
        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
    return out


def allowed(url):
    try:
        p = urlparse(url)
        rp = RobotFileParser()
        r = requests.get(f"{p.scheme}://{p.netloc}/robots.txt", headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(HEADERS["User-Agent"], url)
    except Exception:
        return True


def find_emails(site):
    if not site:
        return []
    found = set()
    for path in PATHS:
        url = urljoin(site if site.endswith("/") else site + "/", path.lstrip("/"))
        try:
            if not allowed(url):
                continue
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select('a[href^="mailto:"]'):
                found.update(EMAIL_RE.findall(a["href"]))
            found.update(EMAIL_RE.findall(soup.get_text(" ")))
        except Exception:
            continue
    return sorted(e.lower() for e in found if not e.lower().endswith(BAD_END))


def build_row(place):
    site = place.get("websiteUri", "")
    return {
        "name": place.get("displayName", {}).get("text", ""),
        "phone": place.get("nationalPhoneNumber", ""),
        "address": place.get("formattedAddress", ""),
        "website": site,
        "emails": ", ".join(find_emails(site)),
    }


PAGE = """
<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Lead Finder</title>
<style>
body{font-family:system-ui;margin:16px;max-width:1000px}
input,button{padding:10px;font-size:16px}input{width:70%}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;margin-top:12px}
td,th{border:1px solid #ccc;padding:6px;text-align:left;font-size:14px}
</style>
<h2>Lead Finder</h2>
<form method=post>
<input name=q value="{{ q }}" placeholder="plumbers in Austin TX" required>
<button>Search</button></form>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
{% if rows is not none %}
<p>{{ rows|length }} businesses. <a href="/csv/{{ rid }}">Download CSV</a></p>
<div class=wrap><table>
<tr><th>Name<th>Phone<th>Website<th>Emails</tr>
{% for r in rows %}<tr><td>{{ r.name }}<td>{{ r.phone }}
<td>{% if r.website %}<a href="{{ r.website }}">site</a>{% endif %}<td>{{ r.emails }}</tr>{% endfor %}
</table></div>{% endif %}
"""


@app.route("/", methods=["GET", "POST"])
def index():
    q, rows, rid, error = "", None, None, None
    if request.method == "POST":
        q = request.form["q"].strip()
        if not API_KEY:
            error = "GOOGLE_API_KEY is not set."
        else:
            try:
                places = search_places(q)
                with ThreadPoolExecutor(max_workers=8) as ex:
                    rows = list(ex.map(build_row, places))
                rid = uuid.uuid4().hex
                CACHE[rid] = rows
                if len(CACHE) > 20:
                    CACHE.pop(next(iter(CACHE)))
            except Exception as e:
                error = f"Search failed: {e}"
    return render_template_string(PAGE, q=q, rows=rows, rid=rid, error=error)


@app.route("/csv/<rid>")
def csv_download(rid):
    rows = CACHE.get(rid, [])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["name", "phone", "address", "website", "emails"])
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=leads.csv"})


if __name__ == "__main__":
    app.run(debug=True)
