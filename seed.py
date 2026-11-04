"""Load SAMPLE leads (fictional businesses, example.com addresses) for a demo run."""
from app import db
from app.main import _insert

SAMPLE = [
    ("Iron Republic Gym", "Faisal A.", "Owner", "Riyadh", "SA", "gym", "legitimate_interest_b2b"),
    ("Pulse Fitness Studio", "Noura K.", "General Manager", "Jeddah", "SA", "fitness studio", "legitimate_interest_b2b"),
    ("Desert CrossFit Box", "Omar S.", "Head Coach", "Dammam", "SA", "crossfit", "legitimate_interest_b2b"),
    ("Salmiya Sports Club", "Hessa M.", "Operations Director", "Salmiya", "KW", "sports club", "legitimate_interest_b2b"),
    ("Gulf Yoga House", "Reem T.", "Founder", "Kuwait City", "KW", "yoga studio", "opt_in"),
    ("Najd Ladies Fitness", "Sara B.", "Owner", "Riyadh", "SA", "gym", "legitimate_interest_b2b"),
    ("Hawally Muscle Lab", "Yousef D.", "Owner", "Hawally", "KW", "gym", "legitimate_interest_b2b"),
    ("Red Sea Swim Academy", "Khalid R.", "Director", "Jeddah", "SA", "sports club", "legitimate_interest_b2b"),
    ("Al Olaya Boxing Club", "Majed H.", "Manager", "Riyadh", "SA", "boxing club", "legitimate_interest_b2b"),
    ("Fahaheel Fitness Center", "Dana A.", "Owner", "Fahaheel", "KW", "fitness", "legitimate_interest_b2b"),
    ("Khobar Bakery Co", "Ali M.", "Owner", "Khobar", "SA", "bakery", "legitimate_interest_b2b"),
    ("Riyadh Print Shop", "Hassan F.", "Owner", "Riyadh", "SA", "printing", "legitimate_interest_b2b"),
    ("Mishref Padel Club", "Fatma Q.", "GM", "Mishref", "KW", "padel club", "none"),
]
if __name__ == "__main__":
    for i, (co, name, title, city, cc, ind, basis) in enumerate(SAMPLE, 1):
        slug = co.lower().replace(" ", "")[:14]
        _insert({"company": co, "contact_name": name, "title": title, "city": city, "country": cc, "industry": ind,
                 "email": f"{slug}@example.com", "phone": f"+9665{i:08d}" if cc == "SA" else f"+9655{i:07d}",
                 "consent_basis": basis, "source": "sample", "notes": "SAMPLE DATA, not a real business"}, "sample")
    print("seeded", len(SAMPLE))
