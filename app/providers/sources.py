"""Lead sources. CSV import works out of the box. Google Places finds gyms/clubs by city
(GOOGLE_PLACES_API_KEY); Apollo enrichment adds decision-makers and emails (APOLLO_API_KEY)."""
import csv
import io
import os

import httpx

FIELDS = ["company", "contact_name", "title", "email", "phone", "website", "city", "country", "industry",
          "segment", "product", "consent_basis", "notes"]

# Spreadsheet header -> lead field. Anything not listed is kept in the lead's "extra" data for the agent.
ALIASES = {
    "company": ["company", "business", "business name", "company name", "name", "account", "organization", "organisation"],
    "contact_name": ["contact", "contact name", "contact_name", "full name", "owner", "person", "first name"],
    "title": ["title", "job title", "role", "position"],
    "email": ["email", "e-mail", "email address", "mail"],
    "phone": ["phone", "phone number", "mobile", "telephone", "tel", "whatsapp"],
    "website": ["website", "url", "site", "web", "domain"],
    "city": ["city", "town", "area", "location"],
    "country": ["country", "country code"],
    "industry": ["industry", "type", "business type"],
    "segment": ["segment", "category", "niche", "vertical", "list"],
    "product": ["product"],
    "consent_basis": ["consent_basis", "consent", "lawful basis"],
    "notes": ["notes", "note", "comments"],
    "status": ["status", "stage"],
    "date_contacted": ["date contacted", "contacted on", "last contacted"],
}


def _norm(h):
    return " ".join(str(h or "").strip().lower().replace("_", " ").split())


def _map_headers(headers):
    m = {}
    for i, h in enumerate(headers):
        n = _norm(h)
        for field, names in ALIASES.items():
            if n in [_norm(x) for x in names] and field not in m.values():
                m[i] = field
                break
    return m


def _clean(v):
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    v = str(v).strip() if not isinstance(v, (int, float)) else v
    return v if v != "" else None


def _rows_from_table(headers, data):
    hmap = _map_headers(headers)
    rows = []
    for r in data:
        if not any(c not in (None, "") for c in r):
            continue
        row, extra = {}, {}
        for i, v in enumerate(r):
            v = _clean(v)
            if i in hmap:
                row[hmap[i]] = str(v) if v is not None and not isinstance(v, str) and hmap[i] != "date_contacted" else v
            elif i < len(headers) and headers[i] and v is not None:
                extra[_norm(headers[i]).replace(" ", "_")] = v
        if row.get("company") or row.get("email") or row.get("website"):
            row["company"] = row.get("company") or row.get("website") or row.get("email")
            row["extra"] = extra
            rows.append(row)
    return rows


def parse_csv(text):
    reader = csv.reader(io.StringIO(text))
    table = list(reader)
    if not table:
        return []
    return _rows_from_table(table[0], table[1:])


def parse_xlsx(data: bytes):
    """Reads the first sheet as leads. A sheet with Website + Problem columns (e.g. "Issues") is
    attached to each lead as its list of issues, matched by website."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    leads, issues = [], {}
    for ws in wb.worksheets:
        table = [list(r) for r in ws.iter_rows(values_only=True)]
        if not table:
            continue
        headers = [_norm(h) for h in table[0]]
        if "website" in headers and "problem" in headers:
            for r in table[1:]:
                rec = {headers[i].replace(" ", "_"): _clean(v) for i, v in enumerate(r) if i < len(headers) and headers[i] and _clean(v) is not None}
                site = _site_key(rec.get("website"))
                if site:
                    issues.setdefault(site, []).append(rec)
        elif not leads:
            leads = _rows_from_table(table[0], table[1:])
    for row in leads:
        site = _site_key(row.get("website"))
        if site in issues:
            row["extra"]["issues"] = [{k: v for k, v in i.items() if k != "website"} for i in issues[site]][:15]
        if "top_issue_(scan)" in row["extra"]:
            row["extra"]["top_issue"] = row["extra"].pop("top_issue_(scan)")
    return leads


def _site_key(url):
    if not url:
        return None
    u = str(url).lower().split("//")[-1].split("/")[0]
    return u[4:] if u.startswith("www.") else u


def google_places(query, country="SA", limit=20):
    """e.g. google_places('gyms in Riyadh'). Business listings only: these are company lines, not personal numbers."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY is not set")
    r = httpx.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={"X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.internationalPhoneNumber,places.websiteUri,places.primaryType"},
        json={"textQuery": query, "regionCode": country, "pageSize": min(limit, 20)},
        timeout=20,
    )
    r.raise_for_status()
    out = []
    for p in r.json().get("places", []):
        out.append({
            "company": p.get("displayName", {}).get("text"), "phone": p.get("internationalPhoneNumber"),
            "website": p.get("websiteUri"), "city": (p.get("formattedAddress") or "").split(",")[-2:-1][0].strip() if "," in (p.get("formattedAddress") or "") else None,
            "country": country, "industry": p.get("primaryType") or "gym", "source": "google_places",
            "consent_basis": "legitimate_interest_b2b",
        })
    return out
