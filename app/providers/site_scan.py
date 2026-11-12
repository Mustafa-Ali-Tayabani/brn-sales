#!/usr/bin/env python3
"""Website scanner (stdlib only), used by the agent to find real, observable problems before it writes to a lead.
Originally site_issues.py - find real, observable problems on any website. Python 3 stdlib only.

Give it URLs (command line, a .txt with one URL per line, or a .csv with a 'url' column).
It crawls a few pages like a normal visitor and writes:
  issues.csv   one row per problem: site, severity, category, problem, evidence, suggested fix
  summary.csv  one row per site with a health score and counts (sort by this to find hot leads)
  report.md    plain-English report per site (paste into a free health-check email)

Examples:
  python3 site_issues.py https://example.com https://another.com
  python3 site_issues.py prospects.csv --outdir out --max-pages 12 --workers 8

Every finding is something the script actually saw. Anything marked "confirm" can be a
network blip or bot protection, so open the site yourself before telling an owner about it.
"""
import argparse, csv, datetime as dt, os, re, socket, ssl, sys, time
import urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; FixifySiteCheck/2.0)"
_SL = bool(os.environ.get("VERCEL"))  # serverless: keep each scan inside the function time limit
TIMEOUT = int(os.environ.get("SCAN_TIMEOUT", "7" if _SL else "15"))
WAITS = tuple(int(x) for x in os.environ.get("SCAN_WAITS", "1,3" if _SL else "5,12").split(","))
BUDGET = int(os.environ.get("SCAN_BUDGET", "38" if _SL else "600"))
NOW = dt.datetime.now(dt.timezone.utc)
SEV = {"critical": 0, "high": 1, "medium": 2, "low": 3}
PENALTY = {"critical": 40, "high": 15, "medium": 6, "low": 2}


# ---------------------------------------------------------------- fetching
def safe(u):
    return urllib.parse.quote(u, safe=":/?#[]@!$&'()*+,;=%~-._")


SKIP_EXTERNAL = ("twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com", "pinterest.com",
                 "youtube.com", "youtu.be", "tiktok.com", "linktr.ee", "godaddy.com", "wa.me", "google.com",
                 "goo.gl", "addtoany.com", "etsy.com", "amazon.com", "faire.com", "squareup.com", "square.link",
                 "paypal.com", "apple.com", "wixsite.com", "shopify.com", "wordpress.com", "weebly.com", "yelp.com")
SKIP_PATHS = ("/cdn-cgi/", "customer_authentication", "/cart", "/checkout", "/account", "email-protection",
              "/wp-login", "/login", "/cdn/", "?share=", "intent/tweet")
BLOCKED = (401, 403, 406, 409, 429)


def http(url, method="GET", limit=1_500_000):
    url = safe(url)
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": UA, "Accept-Encoding": "identity", "Accept": "text/html,*/*"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(limit) if method == "GET" else b""
            return dict(status=r.status, headers={k.lower(): v for k, v in r.headers.items()},
                        body=body, secs=time.time() - t0, url=r.geturl(), err=None)
    except urllib.error.HTTPError as e:
        return dict(status=e.code, headers={k.lower(): v for k, v in (e.headers or {}).items()},
                    body=b"", secs=time.time() - t0, url=url, err=None)
    except Exception as e:
        return dict(status=None, headers={}, body=b"", secs=time.time() - t0, url=url,
                    err=f"{type(e).__name__}: {e}")


def head_status(url):
    r = http(url, "HEAD")
    if r["status"] in (403, 405, 501) or r["status"] is None:  # some servers dislike HEAD
        r = http(url, "GET", limit=2000)
    return r["status"], r["err"]


def cert_info(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = ss.getpeercert()
        exp = dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
        return (exp - NOW).days, None
    except ssl.SSLCertVerificationError as e:
        return None, e.verify_message
    except Exception:
        return None, None


# ---------------------------------------------------------------- parsing
class Doc(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""; self._t = False
        self.meta = {}; self.links = []; self.imgs = []; self.scripts = []; self.styles = []
        self.h1 = 0; self.lang = None; self.canonical = None; self.viewport = False
        self.favicon = False; self.forms = 0; self.text = []; self._skip = 0

    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "html": self.lang = a.get("lang")
        if tag == "title": self._t = True
        if tag == "h1": self.h1 += 1
        if tag in ("script", "style"): self._skip += 1
        if tag == "meta":
            n = (a.get("name") or a.get("property") or "").lower()
            if n: self.meta[n] = a.get("content", "")
            if n == "viewport": self.viewport = True
        if tag == "a" and a.get("href"): self.links.append(a["href"])
        if tag == "img": self.imgs.append((a.get("src") or a.get("data-src") or "", a.get("alt")))
        if tag == "script" and a.get("src"): self.scripts.append(a["src"])
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            if "stylesheet" in rel and a.get("href"): self.styles.append(a["href"])
            if "canonical" in rel: self.canonical = a.get("href")
            if "icon" in rel: self.favicon = True
        if tag == "form": self.forms += 1

    def handle_endtag(self, tag):
        if tag == "title": self._t = False
        if tag in ("script", "style") and self._skip: self._skip -= 1

    def handle_data(self, d):
        if self._t: self.title += d
        elif not self._skip: self.text.append(d)


def parse(body):
    d = Doc()
    try: d.feed(re.sub(r"&(?=[A-Za-z0-9_\-]+=)", "&amp;", body.decode("utf-8", "replace")))
    except Exception: pass
    return d


# ---------------------------------------------------------------- the checks
class Report:
    def __init__(self, url):
        self.url = url; self.rows = []

    def add(self, sev, cat, problem, evidence="", fix=""):
        self.rows.append(dict(site=self.url, severity=sev, category=cat, problem=problem,
                              evidence=evidence, fix=fix))


def scan(url, max_pages):
    if not url.startswith("http"): url = "https://" + url
    R = Report(url)
    t_start = time.time()
    host = urllib.parse.urlparse(url).hostname

    r = http(url)
    for wait in WAITS:  # retry so a network blip is never reported as an outage
        if not (r["err"] or r["status"] is None): break
        time.sleep(wait); r = http(url)
    if r["err"] or r["status"] is None:
        e = r["err"] or "no response"
        if "getaddrinfo" in e or "Name or service" in e or "nodename" in e:
            R.add("high", "availability", "Domain did not resolve after 3 tries from this computer (UNCONFIRMED: could be your network; check in a browser)", e[:120],
                  "If really down: check domain renewal and DNS records; set up uptime monitoring")
        elif "SSL" in e or "TLS" in e or "certificate" in e.lower():
            R.add("high", "availability", "Secure connection failed (UNCONFIRMED: could be a limit of this scanner; check in a browser)", e[:120],
                  "Check the SSL certificate and server TLS settings")
        else:
            R.add("critical", "availability", "Website did not load after 3 tries (UNCONFIRMED: check in a browser)", e[:120],
                  "Check hosting and uptime; set up monitoring and alerts")
        return R
    if r["status"] in BLOCKED or (r["status"] == 503 and "cloudflare" in r["headers"].get("server", "").lower()):
        R.add("low", "availability", f"Site blocks automated checks (HTTP {r['status']}); nothing could be assessed, open it in a browser", r["url"])
        return R
    final = r["url"]; fh = urllib.parse.urlparse(final).hostname or host
    if r["status"] >= 500:
        R.add("critical", "availability", f"Home page returns a server error (HTTP {r['status']}); confirm in a browser", final,
              "Check server logs, PHP/app errors, plugin conflicts; restore from backup if needed")
    elif r["status"] >= 400:
        R.add("critical", "availability", f"Home page returns HTTP {r['status']}; confirm in a browser", final, "Fix the page or redirect")

    # parked / placeholder
    html = r["body"].decode("utf-8", "replace"); low = html.lower()
    for needle, label in [("this domain is for sale", "domain is parked/for sale"),
                          ("coming soon", "page says 'coming soon'"), ("lorem ipsum", "placeholder 'lorem ipsum' text"),
                          ("just another wordpress site", "default WordPress tagline still showing"),
                          ("under construction", "page says 'under construction'")]:
        if needle in low:
            R.add("high", "content", f"Placeholder content: {label}", needle, "Replace with real content or remove")

    # https / redirects / www
    scheme = urllib.parse.urlparse(final).scheme
    if scheme != "https":
        R.add("high", "security", "Site loads over http, browsers show 'Not secure'", final,
              "Install an SSL certificate and redirect all http traffic to https")
    else:
        days, err = cert_info(fh)
        if err: R.add("critical", "security", "SSL certificate is invalid; visitors see a browser warning", err,
                      "Reissue the certificate for the exact domain")
        elif days is not None and days < 0: R.add("critical", "security", "SSL certificate has expired", f"expired {-days} days ago", "Renew now; automate renewal")
        elif days is not None and days <= 21: R.add("high", "security", f"SSL certificate expires in {days} days", "", "Renew; set up auto-renew and an expiry alert")
        h = http("http://" + fh + "/")
        if h["status"] and urllib.parse.urlparse(h["url"]).scheme == "http" and h["status"] < 400:
            R.add("medium", "security", "http:// version does not redirect to https://", "http://" + fh, "Add a permanent redirect to https")
    other = fh[4:] if fh.startswith("www.") else "www." + fh
    if len(fh.split(".")) <= 3:
        o = http(f"{scheme}://{other}/")
        if o["err"] and "Name or service not known" not in o["err"] and "getaddrinfo" not in (o["err"] or ""):
            R.add("low", "config", f"{other} does not respond (www / non-www mismatch)", o["err"][:120], "Point both versions at the site")
        elif o["err"]:
            R.add("low", "config", f"{other} has no DNS record, so visitors typing it get an error", "", "Add a www CNAME or redirect")

    # performance
    if r["secs"] > 3: R.add("high" if r["secs"] > 6 else "medium", "performance", f"Home page took {r['secs']:.1f}s to respond", "",
                            "Enable caching, a CDN and image compression; check hosting plan")
    size = len(r["body"])
    if size > 1_000_000: R.add("medium", "performance", f"Home page HTML is very large ({size//1024} KB)", "", "Reduce page builder bloat; lazy-load")
    if size > 50_000 and "content-encoding" not in r["headers"]:
        pass  # we request identity encoding, so compression cannot be judged fairly; not reported on purpose

    d = parse(r["body"])
    # mixed content
    if scheme == "https":
        mixed = [s for s in d.scripts + d.styles + [i[0] for i in d.imgs] if s.startswith("http://")]
        if mixed: R.add("medium", "security", f"{len(mixed)} resources load insecurely on an https page", mixed[0][:100],
                        "Update URLs to https; fix in theme/plugin settings")

    # SEO / mobile / basics
    title = d.title.strip()
    if not title: R.add("medium", "seo", "Home page has no <title>", "", "Add a descriptive title (50-60 characters)")
    elif len(title) < 10: R.add("low", "seo", "Home page title is very short", title, "Use a descriptive title")
    if not d.meta.get("description"): R.add("low", "seo", "No meta description on home page", "", "Write a 150-160 character description")
    if not d.viewport: R.add("high", "mobile", "No mobile viewport tag: page likely not mobile friendly", "", "Add viewport meta or use a responsive theme")
    if d.h1 == 0: R.add("low", "seo", "Home page has no H1 heading", "", "Add one clear H1")
    if "noindex" in d.meta.get("robots", "").lower():
        R.add("critical", "seo", "Home page tells search engines NOT to index it (noindex)", d.meta.get("robots", ""),
              "Remove noindex (often left on after a site is built; in WordPress: Settings > Reading)")
    if not d.favicon and "favicon" not in low: R.add("low", "polish", "No favicon (browser tab icon)", "", "Add a favicon")
    if not d.lang: R.add("low", "accessibility", "<html> has no lang attribute", "", "Add lang=\"en\"")
    noalt = [s for s, alt in d.imgs if alt is None]
    if d.imgs and len(noalt) / len(d.imgs) > 0.5 and len(noalt) >= 4:
        R.add("low", "accessibility", f"{len(noalt)} of {len(d.imgs)} images have no alt text", "", "Add alt text (helps accessibility and image search)")

    # stale
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\D{0,12}((?:19|20)\d{2})", html, re.I)]
    if years and max(years) < NOW.year - 1:
        R.add("medium", "content", f"Footer still says © {max(years)}; site looks abandoned", "", "Use a dynamic year; refresh content")

    # outdated software signals
    gen = d.meta.get("generator", "")
    m = re.search(r"WordPress\s+([\d.]+)", gen)
    if m: R.add("low", "security", f"WordPress version {m.group(1)} is publicly visible", gen, "Hide the generator tag; keep core updated")
    for s in d.scripts:
        jm = re.search(r"jquery[-.]?(?:min[-.])?(\d+)\.(\d+)", s, re.I)
        if jm and int(jm.group(1)) < 3:
            R.add("medium", "security", f"Outdated jQuery {jm.group(1)}.{jm.group(2)} detected", s[:100], "Update jQuery/theme/plugins"); break
    if re.search(r"<(embed|object)[^>]+\.swf", low): R.add("medium", "content", "Uses Flash (.swf), which no browser plays", "", "Replace with video/HTML5")

    # security headers (low: only mention if several missing)
    hdr = r["headers"]; missing = [h for h in ("strict-transport-security", "x-content-type-options", "x-frame-options", "content-security-policy", "referrer-policy") if h not in hdr]
    if scheme == "https" and len(missing) >= 4:
        R.add("low", "security", f"Missing {len(missing)} of 5 common security headers", ", ".join(missing), "Add via server/CDN config")

    # robots / sitemap
    nl = urllib.parse.urlparse(final).netloc
    rb = http(f"{scheme}://{nl}/robots.txt")
    if rb["status"] == 200:
        txt = rb["body"].decode("utf-8", "replace")
        if robots_blocks_all(txt):
            R.add("critical", "seo", "robots.txt blocks all search engines (Disallow: /)", "", "Remove the block so the site can appear in Google")
    sm = http(f"{scheme}://{nl}/sitemap.xml")
    if sm["status"] == 404 and rb["status"] and "sitemap" not in (rb["body"].decode("utf-8", "replace").lower()):
        R.add("low", "seo", "No sitemap.xml found", "", "Generate and submit a sitemap")

    # crawl internal pages
    base = final; bh = urllib.parse.urlparse(base).hostname
    queue, seen, pages, broken, slow = [base], {base.rstrip("/")}, 0, [], []
    ext_checked, ext_broken = [], []
    imgs_checked = {}
    while queue and pages < max_pages and time.time() - t_start < BUDGET:
        pu = queue.pop(0)
        pr = r if pu == base else http(pu)
        pages += 1
        if pu != base:
            if pr["status"] in (404, 410) or (pr["status"] and pr["status"] >= 500 and pr["status"] != 503):
                broken.append((pu, pr["status"])); continue
            if pr["err"] or pr["status"] in BLOCKED: continue
            if pr["secs"] > 8: slow.append((pu, pr["secs"]))
        if "text/html" not in pr["headers"].get("content-type", "text/html") and pu != base: continue
        pd = d if pu == base else parse(pr["body"])
        for href in pd.links:
            if href.startswith(("mailto:", "tel:", "#", "javascript:", "sms:")): continue
            u = urllib.parse.urljoin(pu, href).split("#")[0]
            p = urllib.parse.urlparse(u)
            if p.scheme not in ("http", "https"): continue
            if any(x in u for x in SKIP_PATHS): continue
            key = u.rstrip("/")
            if key in seen: continue
            seen.add(key)
            if p.hostname == bh:
                if not re.search(r"\.(jpg|jpeg|png|gif|pdf|zip|webp|svg|css|js|mp4)$", p.path, re.I): queue.append(u)
            elif not any((p.hostname or "").endswith(dm) for dm in SKIP_EXTERNAL) and len(ext_checked) < 10 and time.time() - t_start < BUDGET:
                ext_checked.append(u)
                s, e = head_status(u)  # outbound links, sample; only confirmed 404/410
                if s in (404, 410): ext_broken.append((u, s))
        for src, _ in (pd.imgs[:20] if time.time() - t_start < BUDGET else []):
            if src and not src.startswith("data:"):
                u = urllib.parse.urljoin(pu, src)
                if u not in imgs_checked and len(imgs_checked) < 25 and not any(x in u for x in SKIP_PATHS):
                    s, _e = head_status(u); imgs_checked[u] = s
    if broken:
        R.add("high", "links", f"{len(broken)} broken page link(s) on the site itself", "; ".join(f"{u} -> {s}" for u, s in broken[:5]),
              "Fix or redirect the links; set up monthly link checks")
    if ext_broken:
        R.add("low", "links", f"{len(ext_broken)} outbound link(s) go to missing pages", "; ".join(f"{u} -> {s}" for u, s in ext_broken[:3]),
              "Update or remove the dead links")
    bi = [u for u, s in imgs_checked.items() if s in (404, 410)]
    if bi: R.add("high", "content", f"{len(bi)} broken image(s)", "; ".join(bi[:3]), "Re-upload or fix the image paths")
    if slow: R.add("medium", "performance", f"{len(slow)} page(s) took over 8 seconds", "; ".join(f"{u} ({s:.0f}s)" for u, s in slow[:3]), "Investigate heavy pages, plugins and hosting")
    return R


def robots_blocks_all(txt):
    """True only if the group for 'User-agent: *' disallows '/' and allows nothing."""
    groups, agents, rules, last_rule = [], [], [], False
    for line in txt.splitlines():
        line = line.split("#")[0].strip()
        if ":" not in line: continue
        k, v = [x.strip() for x in line.split(":", 1)]
        k = k.lower()
        if k == "user-agent":
            if last_rule: groups.append((agents, rules)); agents, rules, last_rule = [], [], False
            agents.append(v.lower())
        elif k in ("allow", "disallow"):
            rules.append((k, v)); last_rule = True
    groups.append((agents, rules))
    for ag, ru in groups:
        if "*" in ag:
            dis = any(k == "disallow" and v == "/" for k, v in ru)
            allow = any(k == "allow" and v for k, v in ru)
            if dis and not allow: return True
    return False


def score(rows):
    s = 100
    for x in rows: s -= PENALTY[x["severity"]]
    return max(0, s)


# ---------------------------------------------------------------- io
def load_urls(args):
    out = []
    for a in args:
        if os.path.isfile(a):
            if a.lower().endswith(".csv"):
                for row in csv.DictReader(open(a, newline="", encoding="utf-8-sig")):
                    u = (row.get("url") or row.get("website") or "").strip()
                    if u: out.append((u, row.get("business", "")))
            else:
                out += [(l.strip(), "") for l in open(a) if l.strip() and not l.startswith("#")]
        else:
            out.append((a, ""))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+"); ap.add_argument("--outdir", default="out")
    ap.add_argument("--max-pages", type=int, default=10); ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    items = load_urls(a.inputs)
    os.makedirs(a.outdir, exist_ok=True)
    names = {}
    import threading; lock = threading.Lock()
    def work(it):
        u, b = it
        print(f"scanning {u}", file=sys.stderr)
        try: R = scan(u, a.max_pages)
        except Exception as e:
            R = Report(u); R.add("low", "tool", f"Scanner error: {type(e).__name__}", str(e)[:100])
        names[R.url] = b
        top = sorted(R.rows, key=lambda x: SEV[x["severity"]])
        with lock, open(os.path.join(a.outdir, "progress.csv"), "a", newline="", encoding="utf-8") as pf:
            csv.writer(pf).writerow([R.url, score(R.rows), top[0]["problem"] if top else ""])
        return R
    with ThreadPoolExecutor(a.workers) as ex: reports = list(ex.map(work, items))
    # slow second pass, one at a time, for sites that looked unreachable (removes network blips)
    bad = [i for i, R in enumerate(reports) if any(x["category"] == "availability" for x in R.rows)]
    if bad:
        print(f"Re-checking {len(bad)} unreachable-looking sites one by one after a pause...", file=sys.stderr)
        time.sleep(20)
        for i in bad:
            print(f"recheck {items[i][0]}", file=sys.stderr)
            try: R2 = scan(items[i][0], a.max_pages)
            except Exception: continue
            names[R2.url] = items[i][1]; reports[i] = R2
            time.sleep(2)

    fields = ["site", "severity", "category", "problem", "evidence", "fix"]
    with open(os.path.join(a.outdir, "issues.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for R in reports:
            for x in sorted(R.rows, key=lambda x: SEV[x["severity"]]): w.writerow(x)
    summ = []
    for R in reports:
        c = {k: sum(1 for x in R.rows if x["severity"] == k) for k in SEV}
        top = sorted(R.rows, key=lambda x: SEV[x["severity"]])
        summ.append(dict(business=names.get(R.url, ""), site=R.url, health_score=score(R.rows),
                         critical=c["critical"], high=c["high"], medium=c["medium"], low=c["low"],
                         top_issue=top[0]["problem"] if top else ""))
    summ.sort(key=lambda x: x["health_score"])
    with open(os.path.join(a.outdir, "summary.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summ[0].keys())); w.writeheader(); w.writerows(summ)
    with open(os.path.join(a.outdir, "report.md"), "w", encoding="utf-8") as f:
        f.write(f"# Website health report ({NOW:%Y-%m-%d})\n\n")
        for R in sorted(reports, key=lambda R: score(R.rows)):
            f.write(f"## {names.get(R.url) or R.url}  \n{R.url} - health score {score(R.rows)}/100\n\n")
            if not R.rows: f.write("No problems found in this check.\n\n"); continue
            for x in sorted(R.rows, key=lambda x: SEV[x['severity']]):
                f.write(f"- **{x['severity'].upper()}** ({x['category']}): {x['problem']}" + (f" - {x['evidence']}" if x['evidence'] else "") + (f"  \n  Fix: {x['fix']}" if x['fix'] else "") + "\n")
            f.write("\n")
    print(f"Done: {len(reports)} sites. Worst first in {a.outdir}/summary.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
