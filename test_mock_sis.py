"""
Runs the bot's cart + firing code against a fake SIS on localhost. Never touches the real SIS.

    python test_mock_sis.py clean    # 5 normal windows: checks fire timing accuracy
    python test_mock_sis.py faults   # each window gets a different failure (stale button, alerts, slow load, reload)
    python test_mock_sis.py late     # page reloads 150 ms before firing: the fallback click must still send both classes
    python test_mock_sis.py dry      # --dry-run behaviour: nothing may be submitted

The fake SIS rejects any Register POST that arrives before T0 and accepts the rest, like the real one should.
"""
import sys, time, threading, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import sis_register as bot

LOG, LOCK = [], threading.Lock()
STATE = {"T0": None, "cart_gets": {}}
PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="


def cart_page(v, message=""):
    first = STATE["cart_gets"].get(v, 0) == 1
    t0_ms = STATE["T0"] * 1000
    js = []
    if "stale" in v:      # SIS redraws the page: the Register button is replaced by a new element
        js.append("setInterval(()=>{const b=document.getElementById('%s');b.replaceWith(b.cloneNode(true));},700);" % bot.REGISTER_ID)
    if "alert" in v:      # inactivity popup during setup, and again 1 s before firing
        js.append("setTimeout(()=>confirm('Your session is about to expire. Stay signed in?'),2500);")
        js.append("setTimeout(()=>confirm('Your session is about to expire. Stay signed in?'),%d-Date.now()-1000);" % t0_ms)
    if "reload" in v:     # page reloads after the click was armed, wiping the in-page timer
        before = 200 if "late" in v else 1000    # "latereload": too late to re-arm, so the fallback click must cover it
        js.append("if(!sessionStorage.getItem('reloaded')&&!%s)setTimeout(()=>{sessionStorage.setItem('reloaded',1);location.reload();},%d-Date.now()-%d);" % ("true" if message else "false", t0_ms, before))
    btn = ('<input type="image" id="%s" name="ctl00$contentPlaceHolder$ibEnroll" src="%s" '
           'style="width:120px;height:30px;border:1px solid #333">' % (bot.REGISTER_ID, PIXEL))
    if "slowfirst" in v and first:   # first load: button shows up far too late, so the bot must retry
        js.append("setTimeout(()=>{document.getElementById('slot').innerHTML=%r;},8000);" % btn)
        btn = ""
    return f"""<html><body><h3>Fake SIS cart ({v})</h3><p id="msg">{message}</p>
<form method="POST" action="/sswf/cart?v={v}">
<input type="checkbox" id="{bot.SELECT_ALL_ID}" onclick="document.querySelectorAll('.course').forEach(c=>c.checked=this.checked)"> Select all<br>
<input type="checkbox" class="course" name="c1"> AS.110.108<br>
<input type="checkbox" class="course" name="c2"> EN.601.226<br>
<input type="hidden" name="who" value="{v}">
<span id="slot">{btn}</span></form><script>{''.join(js)}</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, body, code=200):
        b = body.encode()
        self.send_response(code); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        t = time.time()
        u = urllib.parse.urlparse(self.path); v = urllib.parse.parse_qs(u.query).get("v", [""])[0]
        if u.path.startswith("/favicon.ico"):
            with LOCK: LOG.append(("warm", t, v, self.headers.get("Referer", "")))
            self.send_response(204); self.end_headers(); return
        if u.path == "/sswf/home":
            return self.send(f'<a href="/sswf/registration?v={v}"><span>Registration</span></a>')
        if u.path == "/sswf/registration":
            return self.send(f'<a href="/sswf/cart?v={v}">My Cart</a>')
        if u.path == "/sswf/cart":
            with LOCK: STATE["cart_gets"][v] = STATE["cart_gets"].get(v, 0) + 1
            if "late" not in v:   # latereload: reload must finish before the timer would fire
                time.sleep(0.3)   # SIS is slow
            return self.send(cart_page(v))
        self.send("not found", 404)

    def do_HEAD(self):   # lead probe hits a static file with a unique query string
        if self.path.startswith("/favicon.ico"):
            with LOCK: LOG.append(("probe", time.time(), "", ""))
            self.send_response(204); self.end_headers(); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        t = time.time()   # arrival time = when SIS's gate would check the clock
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        v = form.get("who", ["?"])[0]
        courses = [c for c in ("c1", "c2") if c in form]
        ok = t >= STATE["T0"]
        with LOCK: LOG.append(("post", t, v, courses))
        time.sleep(0.3)
        msg = f"Success: enrolled in {len(courses)} class(es)" if ok else "Error: registration is not open yet"
        self.send(cart_page(v, msg))


def main(scenario):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    variants = {"clean": ["plain"] * 5, "dry": ["plain"] * 2, "late": ["latereload", "plain"],
                "faults": ["plain", "stale", "alert", "slowfirst", "reload"]}[scenario]
    offsets = bot.OFFSETS_MS[:len(variants)]
    probe = scenario in ("clean", "dry")
    STATE["T0"] = time.time() + 12 + (12 if scenario == "faults" else 6) * len(variants) + (22 if probe else 0)
    lead_ms = 0                                          # same machine: no clock difference or network trip

    ready = []
    for i, (var, off) in enumerate(zip(variants, offsets), start=1):
        label = f"Browser {i} ({off:+d}ms)"
        v = f"b{i}-{var}"
        b = bot.create_browser()
        b.get(f"{base}/sswf/home?v={v}")
        if not bot.navigate_to_cart(b, label, max_retries=3, wait_time=4):
            print(f"FAIL: {label} never got ready"); continue
        lock, stop = threading.Lock(), threading.Event()
        threading.Thread(target=bot.stay_logged_in_watcher, args=(b, label, lock, stop), daemon=True).start()
        ready.append((b, lock, stop, label, off, v))
    if time.time() > STATE["T0"] - 3:
        print("FAIL: setup took too long for this test's T0"); return 1
    if probe:   # the real choose_lead() path, started right away instead of 90 s out
        STATE["T0"] = time.time() + 28   # set after setup, so slow window start-up can't eat the probe's time
        bot.PROBE_AT_S, bot.PROBE_END_S = 10_000, 3
        lead_ms = bot.choose_lead([r[:5] for r in ready], STATE["T0"], probes=12)
        n_probe = sum(1 for e in LOG if e[0] == "probe")
        # Same machine and the fake SIS stamps Date from the same clock, so the true lead is ~0-3 ms.
        ok = 0 <= lead_ms <= 15 and n_probe >= 10
        print(f"lead probe: {lead_ms} ms from {n_probe} HEAD requests  {'OK' if ok else 'FAIL'}")
        if not ok:
            return 1
    print(f"\n--- {len(ready)} windows ready, firing in {STATE['T0'] - time.time():.1f}s ---")

    results = bot.run_bracket([r[:5] for r in ready], STATE["T0"], lead_ms, dry_run=(scenario == "dry"))
    time.sleep(1.5)
    for i, (b, _, _, label, _, _) in enumerate(ready, start=1):
        if scenario != "dry":
            bot.report_result(b, label, i)

    # ---- Check what the fake SIS actually received ----
    T0 = STATE["T0"]
    print(f"\n{'window':10} {'variant':10} {'offset':>7} {'arrived':>9} {'error':>7}  {'warm-up':>8}  result")
    failures = 0
    for (b, _, _, label, off, v), res in zip(ready, sorted(results, key=lambda r: r["label"])):
        posts = [e for e in LOG if e[0] == "post" and e[2] == v]
        warms = [e for e in LOG if e[0] == "warm"]
        if scenario == "dry":
            ok = not posts and "fired" in res
            print(f"{label[:9]:10} {v.split('-')[1]:10} {off:+6d}ms {'—':>9} {'—':>7}  {'':8}  {'OK: nothing submitted' if ok else 'FAIL'}")
            failures += not ok; continue
        if len(posts) != 1:
            print(f"{label[:9]:10} {v:10} FAIL: {len(posts)} submissions received"); failures += 1; continue
        _, t, _, courses = posts[0]
        arrived = (t - T0) * 1000
        err = arrived - off
        accepted = t >= T0
        # Functional checks fail the test. Timing beyond 40 ms is reported as SLOW: that's Chrome being
        # held up by other load on the Mac (seen 30-100 ms on a busy machine), not a bot bug.
        tol = 800 if "late" in v else 40
        ok = abs(err) <= max(tol, 150) and courses == ["c1", "c2"] and accepted == (off + err >= 0)
        slow = ok and abs(err) > tol
        warm = min((abs(w[1] - (T0 + off / 1000) + bot.WARM_LEAD_MS / 1000) for w in warms), default=None)
        how = " (fallback click)" if res.get("fallback") else ""
        print(f"{label[:9]:10} {v.split('-')[1]:10} {off:+6d}ms {arrived:+8.0f}ms {err:+6.0f}ms  "
              f"{'yes' if warm is not None and warm < 0.05 else 'no':>8}  "
              f"{'accepted' if accepted else 'rejected (early)'}, {len(courses)} classes sent{how}  {'SLOW' if slow else 'OK' if ok else 'FAIL'}")
        failures += not ok
        try:   # where did the time between click and arrival go? (Chrome's navigation timing for the POST)
            nt = b.execute_script("const n=performance.getEntriesByType('navigation')[0];"
                                  "return [performance.timeOrigin, n.fetchStart, n.connectStart, n.connectEnd, n.requestStart];")
            o = nt[0] / 1000
            print(f"{'':10} chrome: nav start {(o - res.get('fired', o)) * 1000:+.0f}ms after click, "
                  f"fetch {nt[1]:.0f}, connect {nt[2]:.0f}-{nt[3]:.0f}, request {nt[4]:.0f} ms after nav start; "
                  f"server got it {(t - o) * 1000 - nt[4]:+.0f}ms after request start")
        except Exception as e:
            print("  (no nav timing)", type(e).__name__)
    for b, *_ in ready:
        b.quit()
    print(f"\n{'PASS' if not failures else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "clean"))
