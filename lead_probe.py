"""
Measures LEAD = (server's clock when it handles a request) - (this Mac's clock when the request was sent).

Every response carries a whole-second `Date` header, so a request sent at local time `ts` that the
server stamped `d` tells us  d <= ts + LEAD < d + 1.  Probes are timed to land right on the server's
second rollover, so each one splits the remaining range; the answer is the value most probes agree on
(a few stragglers from network jitter are outvoted instead of breaking the result).

sis_register.py runs this from inside a logged-in Chrome window (see measure_lead_in_browser there).
Run standalone, it uses plain Python requests:

    python lead_probe.py [url]

NOTE: sis.jhu.edu is behind Cloudflare, which answers non-browser requests with a 403 challenge page.
Standalone runs against SIS therefore measure Cloudflare's clock, not SIS's, and every request is
challenged, so don't run it repeatedly. The in-browser probe doesn't have that problem.
"""
import email.utils
import http.client
import random
import ssl
import sys
import time
import urllib.parse

DEFAULT_URL = "https://sis.jhu.edu/sswf/"


def best_lead(samples):
    """samples: (ts, d) pairs. Returns (center, half_width, agreeing) of the LEAD most samples agree with."""
    edges = sorted({d - ts for ts, d in samples} | {d + 1 - ts for ts, d in samples})
    best = None
    for lo, hi in zip(edges, edges[1:]):
        mid = (lo + hi) / 2
        agree = sum(d <= ts + mid < d + 1 for ts, d in samples)
        if best is None or agree > best[0]:
            best = (agree, lo, hi)
        elif agree == best[0] and abs(lo - best[2]) < 1e-9:
            best = (agree, best[1], hi)   # extend a tied, touching range
    agree, lo, hi = best
    return (lo + hi) / 2, (hi - lo) / 2, agree


def measure_lead(probe, probes=24, deadline=None, gap_s=0.3):
    """probe(t_send) must send one request at local epoch time t_send (or as soon as possible) and
    return (ts, rtt_s, date_epoch). Returns dict(lead_ms, plus_minus_ms, agree, total, rtt_ms)."""
    raw = [probe(0) for _ in range(3)]                 # warm the connection + rough estimate
    for _ in range(probes):
        if deadline and time.time() > deadline - 1.5:
            break
        mid, _, _ = best_lead([(ts, d) for ts, _, d in raw])
        boundary = int(time.time() + mid) + 1 + random.uniform(-0.04, 0.04)   # a server second rollover
        raw.append(probe(boundary - mid))
        time.sleep(gap_s)
    center, half, agree = best_lead([(ts, d) for ts, _, d in raw])
    rtts = sorted(r for _, r, _ in raw)
    return {"lead_ms": center * 1000, "plus_minus_ms": half * 1000, "agree": agree,
            "total": len(raw), "rtt_ms": rtts[len(rtts) // 2] * 1000}


def http_probe(url):
    u = urllib.parse.urlparse(url)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=5, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=5)
    path = u.path or "/"

    def probe(t_send):
        while time.time() < t_send - 0.002:
            time.sleep(0.001)
        while time.time() < t_send:
            pass
        ts = time.time()
        conn.request("HEAD", path)
        r = conn.getresponse()
        r.read()
        return ts, time.time() - ts, email.utils.parsedate_to_datetime(r.getheader("Date")).timestamp()
    return probe


if __name__ == "__main__":
    r = measure_lead(http_probe(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
    print(f"probes agreeing {r['agree']}/{r['total']}, median RTT {r['rtt_ms']:.0f} ms")
    print(f"LEAD = {r['lead_ms']:+.0f} ms (±{r['plus_minus_ms']:.0f})")
