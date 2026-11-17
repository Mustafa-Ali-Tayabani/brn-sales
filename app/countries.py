"""Countries: names for the two-letter codes leads are stored with, reading a country someone typed ("Pakistan", "UAE",
"pk"), and working out a lead's country from what is actually known about it (phone prefix, web address) instead of
assuming one."""
import re

_ISO = """AF:Afghanistan AL:Albania DZ:Algeria AD:Andorra AO:Angola AR:Argentina AM:Armenia AU:Australia AT:Austria AZ:Azerbaijan
BS:Bahamas BH:Bahrain BD:Bangladesh BB:Barbados BY:Belarus BE:Belgium BZ:Belize BJ:Benin BT:Bhutan BO:Bolivia
BA:Bosnia_and_Herzegovina BW:Botswana BR:Brazil BN:Brunei BG:Bulgaria BF:Burkina_Faso BI:Burundi KH:Cambodia CM:Cameroon
CA:Canada CV:Cape_Verde CF:Central_African_Republic TD:Chad CL:Chile CN:China CO:Colombia KM:Comoros CG:Congo
CD:DR_Congo CR:Costa_Rica CI:Ivory_Coast HR:Croatia CU:Cuba CY:Cyprus CZ:Czechia DK:Denmark DJ:Djibouti DO:Dominican_Republic
EC:Ecuador EG:Egypt SV:El_Salvador GQ:Equatorial_Guinea ER:Eritrea EE:Estonia SZ:Eswatini ET:Ethiopia FJ:Fiji FI:Finland
FR:France GA:Gabon GM:Gambia GE:Georgia DE:Germany GH:Ghana GR:Greece GT:Guatemala GN:Guinea GY:Guyana HT:Haiti HN:Honduras
HK:Hong_Kong HU:Hungary IS:Iceland IN:India ID:Indonesia IR:Iran IQ:Iraq IE:Ireland IL:Israel IT:Italy JM:Jamaica JP:Japan
JO:Jordan KZ:Kazakhstan KE:Kenya KW:Kuwait KG:Kyrgyzstan LA:Laos LV:Latvia LB:Lebanon LS:Lesotho LR:Liberia LY:Libya
LI:Liechtenstein LT:Lithuania LU:Luxembourg MO:Macao MG:Madagascar MW:Malawi MY:Malaysia MV:Maldives ML:Mali MT:Malta
MR:Mauritania MU:Mauritius MX:Mexico MD:Moldova MC:Monaco MN:Mongolia ME:Montenegro MA:Morocco MZ:Mozambique MM:Myanmar
NA:Namibia NP:Nepal NL:Netherlands NZ:New_Zealand NI:Nicaragua NE:Niger NG:Nigeria MK:North_Macedonia NO:Norway OM:Oman
PK:Pakistan PS:Palestine PA:Panama PG:Papua_New_Guinea PY:Paraguay PE:Peru PH:Philippines PL:Poland PT:Portugal PR:Puerto_Rico
QA:Qatar RO:Romania RU:Russia RW:Rwanda SA:Saudi_Arabia SN:Senegal RS:Serbia SC:Seychelles SL:Sierra_Leone SG:Singapore
SK:Slovakia SI:Slovenia SO:Somalia ZA:South_Africa KR:South_Korea SS:South_Sudan ES:Spain LK:Sri_Lanka SD:Sudan SR:Suriname
SE:Sweden CH:Switzerland SY:Syria TW:Taiwan TJ:Tajikistan TZ:Tanzania TH:Thailand TG:Togo TT:Trinidad_and_Tobago TN:Tunisia
TR:Turkey TM:Turkmenistan UG:Uganda UA:Ukraine AE:United_Arab_Emirates GB:United_Kingdom US:United_States UY:Uruguay
UZ:Uzbekistan VE:Venezuela VN:Vietnam YE:Yemen ZM:Zambia ZW:Zimbabwe"""
NAMES = {c: n.replace("_", " ") for c, n in (x.split(":") for x in _ISO.split())}
_BY_NAME = {n.lower(): c for c, n in NAMES.items()}
_BY_NAME.update({"uk": "GB", "britain": "GB", "great britain": "GB", "england": "GB", "scotland": "GB", "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
                 "united states of america": "US", "uae": "AE", "emirates": "AE", "dubai": "AE", "ksa": "SA", "saudi": "SA", "holland": "NL", "czech republic": "CZ",
                 "turkiye": "TR", "türkiye": "TR", "korea": "KR", "russian federation": "RU", "viet nam": "VN"})
# country calling codes that belong to one country (+1 and +7 are shared, so they say nothing)
_DIAL = {"966": "SA", "971": "AE", "965": "KW", "974": "QA", "973": "BH", "968": "OM", "92": "PK", "91": "IN", "880": "BD", "94": "LK", "977": "NP", "20": "EG", "962": "JO",
         "961": "LB", "964": "IQ", "90": "TR", "44": "GB", "353": "IE", "49": "DE", "33": "FR", "39": "IT", "34": "ES", "351": "PT", "31": "NL", "32": "BE", "41": "CH", "43": "AT",
         "46": "SE", "47": "NO", "45": "DK", "358": "FI", "48": "PL", "420": "CZ", "30": "GR", "40": "RO", "36": "HU", "380": "UA", "61": "AU", "64": "NZ", "65": "SG", "60": "MY",
         "62": "ID", "63": "PH", "66": "TH", "84": "VN", "81": "JP", "82": "KR", "86": "CN", "852": "HK", "886": "TW", "27": "ZA", "234": "NG", "254": "KE", "233": "GH",
         "212": "MA", "216": "TN", "213": "DZ", "55": "BR", "52": "MX", "54": "AR", "57": "CO", "56": "CL", "51": "PE", "972": "IL"}
# country endings that companies everywhere use as a brand (.io, .co, .ai ...): they say nothing about where a lead is
_NOT_A_PLACE = {"io", "co", "ai", "me", "tv", "cc", "ly", "fm", "gg", "to", "sh", "vc", "ws", "la", "so", "im", "st", "gl", "nu", "tk", "ml", "ga", "cf", "gq", "pw", "ac", "is", "am", "in"}
_NO_SITE = ("linkedin.com", "facebook.com", "instagram.com", "reddit.com", "twitter.com", "x.com", "google.com", "youtube.com")


def name(code):
    return NAMES.get((code or "").upper(), code or "")


def code(text):
    """The two-letter code for what someone wrote ("Pakistan", "pk", "UAE"), or None when it is not a country."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().strip(".").lower()
    if not t or t == "--":
        return None
    if len(t) == 2 and t.upper() in NAMES:
        return t.upper()
    return _BY_NAME.get(t)


def codes(text):
    """Every country in a comma-separated list, as codes, in the order written. What is not a country is left out."""
    out = []
    for part in re.split(r"[,;\n]", str(text or "")):
        c = code(part)
        if c and c not in out:
            out.append(c)
    return out


def guess(row):
    """A lead's country from evidence on the lead itself: its phone number's country code, else the ending of its web or
    email address (.pk, .co.uk). None when nothing says where it is; a .com address proves nothing."""
    phone = re.sub(r"[^\d+]", "", str(row.get("phone") or ""))
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    if phone.startswith("+"):
        for n in (3, 2):
            if phone[1:1 + n] in _DIAL:
                return _DIAL[phone[1:1 + n]]
    for value in (row.get("website"), (row.get("email") or "").split("@")[-1]):
        host = re.sub(r"^(https?://)?(www\.)?", "", str(value or "").strip().lower()).split("/")[0].split(":")[0]
        if not host or "." not in host or any(host == s or host.endswith("." + s) for s in _NO_SITE):
            continue
        end = host.rsplit(".", 1)[-1]
        if end == "uk":
            return "GB"
        if len(end) == 2 and end not in _NOT_A_PLACE and end.upper() in NAMES:
            return end.upper()
    return None
