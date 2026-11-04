"""Lead sources. CSV import works out of the box. Google Places finds gyms/clubs by city
(GOOGLE_PLACES_API_KEY); Apollo enrichment adds decision-makers and emails (APOLLO_API_KEY)."""
import csv
import io
import os

import httpx

FIELDS = ["company", "contact_name", "title", "email", "phone", "website", "city", "country", "industry", "consent_basis", "notes"]


def parse_csv(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        row = {k: (r.get(k) or "").strip() or None for k in FIELDS}
        if row["company"]:
            row["country"] = (row["country"] or "SA").upper()
            row["consent_basis"] = row["consent_basis"] or "legitimate_interest_b2b"
            rows.append(row)
    return rows


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
