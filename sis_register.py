import os
import sys
import time
import datetime
from dateutil import parser
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException, WebDriverException
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
import argparse
import getpass
from email.utils import parsedate_to_datetime
import json
import tempfile
import threading
from lead_probe import measure_lead

# --------------------------
# Config
# --------------------------
# Login is kept out of the code. sis_register.py looks, in order, at the SIS_EMAIL / SIS_PASSWORD
# environment variables, then sis_credentials.py (gitignored; copy sis_credentials.example.py),
# then asks you.
_credentials = None


def get_credentials():
    global _credentials
    if _credentials is None:
        try:
            import sis_credentials as local
        except ImportError:
            local = None
        email = os.environ.get("SIS_EMAIL") or getattr(local, "email", None) or input("JHU email: ")
        password = os.environ.get("SIS_PASSWORD") or getattr(local, "password", None) or getpass.getpass("JHU password: ")
        _credentials = (email, password)
    return _credentials


DEFAULT_TIME = "07:00"

# How many ms BEFORE registration time (by this Mac's clock) a request must be sent so that SIS
# handles it exactly at registration time (by SIS's clock). Covers clock difference + network trip.
# The bot re-measures this from a logged-in window ~90 s before registration (see PROBE_AT_S).
# This is only the fallback if that measurement fails: 2026-11-15 measurements (+94, +120, +97 ms)
# plus ~6 ms Chrome needs between the click and the request leaving.
SERVER_LEAD_MS = 111
PROBE_AT_S = 90           # start measuring the lead this long before registration...
PROBE_END_S = 20          # ...and stop by this long before
MAX_LEAD_SHIFT_MS = 300   # distrust a measurement this far from the fallback

# Bracket around the moment SIS's clock hits registration time (ms, by SIS's clock).
# Skewed late on purpose: a click that lands before registration opens is wasted.
OFFSETS_MS = [-50, -12, 25, 62, 100]
NUM_BROWSERS = len(OFFSETS_MS)

SIS_URL = "https://sis.jhu.edu/sswf/"
SELECT_ALL_ID = "SelectAllCheckBox"
REGISTER_ID = "ctl00_contentPlaceHolder_ibEnroll"
LOGIN_LEAD_MIN = 10       # log in this many minutes before registration
ARM_LEAD_S = 2.0          # hand the click to an in-page timer this long before firing
WARM_LEAD_MS = 1500       # ping SIS this long before firing so the connection is already open
CLOUDFLARE_CLEAR_TIMEOUT = 300
CLOUDFLARE_INITIAL_CHECK = 3


# --------------------------
# Browser helpers
# --------------------------
def create_browser():
    unique_user_data_dir = tempfile.mkdtemp()
    chrome_options = Options()
    chrome_options.add_argument(f"--user-data-dir={unique_user_data_dir}")
    chrome_options.add_argument("profile-directory=Default")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-networking")
    chrome_options.add_argument("--disable-default-apps")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--disable-popup-blocking")
    # Covered/background windows get their timers slowed down by Chrome; with 5 windows most are covered.
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_experimental_option("detach", True)
    browser = webdriver.Chrome(options=chrome_options)
    return browser


def dismiss_alert(browser, label):
    try:
        alert = browser.switch_to.alert
        print(f"⚠️ [{label}] Inactivity alert detected! Dismissing...")
        alert.accept()
        return True
    except NoAlertPresentException:
        return False


def stay_logged_in_watcher(browser, label, lock, stop_event):
    # Shares the driver with the fire thread, so every call goes through `lock`.
    while not stop_event.wait(1):
        try:
            with lock:
                dismiss_alert(browser, label)
        except Exception:
            pass


def page_has_cloudflare_challenge(browser):
    try:
        return bool(browser.execute_script("""
            const title = (document.title || '').toLowerCase();
            const body = (document.body ? document.body.innerText : '').toLowerCase();
            const url = location.href.toLowerCase();
            const selector = [
              'iframe[src*="challenges.cloudflare.com"]',
              'iframe[src*="/cdn-cgi/challenge-platform/"]',
              'input[name="cf-turnstile-response"]',
              '.cf-turnstile',
              '[data-sitekey][data-action]'
            ].join(',');
            return Boolean(
              document.querySelector(selector) ||
              url.includes('/cdn-cgi/challenge-platform/') ||
              title.includes('just a moment') ||
              title.includes('attention required') ||
              body.includes('verify you are human') ||
              body.includes('checking if the site connection is secure') ||
              body.includes('cloudflare')
            );
        """))
    except WebDriverException:
        return False


def wait_for_cloudflare_if_present(browser, label, timeout=CLOUDFLARE_CLEAR_TIMEOUT):
    """Pause when Cloudflare asks for human verification; continue after the user clears it."""
    end = time.time() + CLOUDFLARE_INITIAL_CHECK
    detected = page_has_cloudflare_challenge(browser)
    while not detected and time.time() < end:
        time.sleep(0.25)
        detected = page_has_cloudflare_challenge(browser)
    if not detected:
        return False

    print(f"🛡️ [{label}] Cloudflare verification is open in the browser.")
    input(f"🛡️ [{label}] Complete it manually, then press ENTER here to continue...")

    end = time.time() + timeout
    while time.time() < end:
        if not page_has_cloudflare_challenge(browser):
            print(f"✅ [{label}] Cloudflare verification cleared.")
            return True
        time.sleep(1)

    raise TimeoutException(f"[{label}] Cloudflare verification was not cleared within {timeout} seconds")


def login_browser(browser, label):
    browser.get(SIS_URL)
    wait_for_cloudflare_if_present(browser, label)

    print(f"🔑 [{label}] Logging in...")

    sign_in_button = WebDriverWait(browser, 60).until(
        EC.presence_of_element_located((By.XPATH, "//a[contains(text(),'Sign In')]"))
    )
    sign_in_button.click()
    wait_for_cloudflare_if_present(browser, label)

    WebDriverWait(browser, 69).until(
        EC.visibility_of_element_located((By.NAME, "loginfmt"))
    )
    email, password = get_credentials()
    email_field = browser.find_element(By.NAME, "loginfmt")
    WebDriverWait(browser, 69).until(EC.element_to_be_clickable((By.NAME, "loginfmt")))
    email_field.click()
    email_field.clear()
    email_field.send_keys(email)
    email_field.send_keys(Keys.RETURN)

    time.sleep(2)

    WebDriverWait(browser, 69).until(EC.element_to_be_clickable((By.NAME, "passwd")))
    password_field = browser.find_element(By.NAME, "passwd")
    password_field.send_keys(password)
    password_field.send_keys(Keys.RETURN)
    wait_for_cloudflare_if_present(browser, label)

    input(f"🔐 [{label}] Complete 2FA in the browser, then press ENTER here to continue...")
    wait_for_cloudflare_if_present(browser, label)
    print(f"✅ [{label}] Login complete.")


def open_cart(browser, label, timeout=30):
    wait_for_cloudflare_if_present(browser, label)
    registration_link = WebDriverWait(browser, timeout).until(
        EC.element_to_be_clickable((By.XPATH, "//a[span[text()='Registration']]"))
    )
    registration_link.click()
    wait_for_cloudflare_if_present(browser, label)
    print(f"✅ [{label}] Clicked 'Registration'")

    my_cart_link = WebDriverWait(browser, timeout).until(
        EC.element_to_be_clickable((By.LINK_TEXT, "My Cart"))
    )
    my_cart_link.click()
    wait_for_cloudflare_if_present(browser, label)
    print(f"✅ [{label}] Clicked 'My Cart'")


def navigate_to_cart(browser, label, max_retries=10, wait_time=120):
    """Open the cart, tick Select All, and wait for the Register button. Returns True when ready."""
    cart_url = None
    for attempt in range(max_retries):
        try:
            # Each retry re-opens the cart instead of waiting on the same broken page. (A plain
            # refresh after a postback would pop Chrome's "resubmit form?" dialog.) Use the menu if
            # it's on the page, otherwise load the cart's address directly.
            if cart_url is None:
                open_cart(browser, label)
                cart_url = browser.current_url
            else:
                try:
                    open_cart(browser, label, timeout=10)
                except TimeoutException:
                    print(f"🔁 [{label}] Menu not found; reloading cart page directly.")
                    browser.get(cart_url)
                    wait_for_cloudflare_if_present(browser, label)

            print(f"🔄 [{label}] Attempt {attempt + 1} to load cart elements...")

            select_all = WebDriverWait(browser, wait_time).until(
                EC.element_to_be_clickable((By.ID, SELECT_ALL_ID))
            )
            # Only click when unticked; clicking again on a retry would untick every class.
            if not select_all.is_selected():
                select_all.click()
            # Select All may redraw the page, so look it up fresh to confirm.
            WebDriverWait(browser, wait_time).until(
                lambda d: d.find_element(By.ID, SELECT_ALL_ID).is_selected()
            )
            print(f"✅ [{label}] 'Select All' is ticked.")

            register_button = WebDriverWait(browser, wait_time).until(
                EC.element_to_be_clickable((By.ID, REGISTER_ID))
            )
            browser.execute_script("arguments[0].scrollIntoView({block: 'center'});", register_button)
            print(f"✅ [{label}] Register button is ready.")
            return True

        except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as e:
            print(f"⚠️ [{label}] Attempt {attempt + 1} failed: {type(e).__name__}")

    print(f"❌ [{label}] SIS is too slow or DOM broke.")
    safe_label = label.split(" (")[0].replace(" ", "_")
    browser.save_screenshot(f"load_cart_fail_{safe_label}.png")
    return False


# --------------------------
# Firing
# --------------------------
# Runs inside the page. The page's own timer clicks the button, so the click doesn't wait on a
# Selenium round trip. The button is looked up by ID at click time, so a page redraw since the
# cart loaded doesn't matter. The click time is saved in sessionStorage, which survives the
# page navigation the click causes.
ARM_JS = """
const [target, dryRun, btnId, warmMs, selectAllId] = arguments;
// Re-tick Select All in case the page was reloaded since setup (a reload clears the checkboxes).
const tick = () => { const sa = document.getElementById(selectAllId); if (sa && !sa.checked) sa.click(); };
tick();
sessionStorage.removeItem('sisFire');
window.__sisCancel = false;
window.__sisArmed = true;           // lets Python see whether a reload wiped the timer
const record = (r) => sessionStorage.setItem('sisFire', JSON.stringify(r));
setTimeout(() => {
  fetch(location.origin + '/favicon.ico?warm=' + Date.now(),
        {cache: 'no-store', credentials: 'same-origin'}).catch(() => {});
}, Math.max(0, target - warmMs - Date.now()));
setTimeout(() => {
  if (window.__sisCancel) return;
  while (Date.now() < target) {}          // spin the last few ms for precision
  const btn = document.getElementById(btnId);
  if (!btn) { record({error: 'register button not found', at: Date.now()}); return; }
  tick();
  record({fired: Date.now(), dryRun});
  if (!dryRun) btn.click();
}, Math.max(0, target - 20 - Date.now()));
return Date.now();
"""

FALLBACK_JS = """
const [btnId, dryRun, selectAllId] = arguments;
const prev = JSON.parse(sessionStorage.getItem('sisFire') || 'null');
if (prev && !prev.error) return prev;     // already fired — never click twice
window.__sisCancel = true;                // stop a late in-page timer from also clicking
const btn = document.getElementById(btnId);
if (!btn) return null;
const sa = document.getElementById(selectAllId);
if (sa && !sa.checked) sa.click();
const r = {fired: Date.now(), dryRun, fallback: true};
sessionStorage.setItem('sisFire', JSON.stringify(r));
if (!dryRun) btn.click();
return r;
"""


# Runs inside the page: waits until t_send (local ms), then sends one tiny HEAD request and reports
# the server's Date header. Uses a static file from the page with a unique query string, so
# Cloudflare's cache can't answer for SIS and the cart page itself is never reloaded.
PROBE_JS = """
const tSend = arguments[0], done = arguments[arguments.length - 1];
if (!window.__sisProbeUrl) {
  const same = (u) => { try { return new URL(u, location.href).origin === location.origin; } catch (e) { return false; } };
  const asset = [...document.querySelectorAll('link[rel=stylesheet][href], script[src], img[src]')]
    .map(e => e.href || e.src).find(same);
  window.__sisProbeUrl = asset || (location.origin + '/favicon.ico');
}
const url = window.__sisProbeUrl;
const go = async () => {
  const ts = Date.now();
  try {
    const r = await fetch(url + (url.includes('?') ? '&' : '?') + 'probe=' + ts + Math.random().toString(36).slice(2),
                          {method: 'HEAD', cache: 'no-store', credentials: 'same-origin'});
    done([ts, Date.now() - ts, r.headers.get('date'), r.status, r.headers.get('cf-cache-status'), url]);
  } catch (e) { done({error: String(e)}); }
};
const wait = tSend - Date.now();
if (wait <= 0) go(); else setTimeout(() => { while (Date.now() < tSend) {} go(); }, Math.max(0, wait - 15));
"""


def measure_lead_in_browser(browser, lock, probes=24, deadline=None):
    info = {}

    def probe(t_send):
        with lock:
            browser.set_script_timeout(10)
            r = browser.execute_async_script(PROBE_JS, t_send * 1000)
        if isinstance(r, dict):
            raise RuntimeError(r.get("error"))
        ts_ms, rtt_ms, date, status, cf_cache, url = r
        info.update(status=status, cf_cache=cf_cache, url=url)
        if not date:
            raise RuntimeError("response had no Date header")
        return ts_ms / 1000, rtt_ms / 1000, parsedate_to_datetime(date).timestamp()

    result = measure_lead(probe, probes=probes, deadline=deadline)
    result.update(info)
    return result


def choose_lead(ready, registration_epoch, probes=24):
    """Measure the lead from the first ready window shortly before registration; fall back to SERVER_LEAD_MS."""
    deadline = registration_epoch - PROBE_END_S
    if time.time() > deadline - 12:
        print(f"⚠️ Too close to registration to measure the lead — using {SERVER_LEAD_MS} ms.")
        return SERVER_LEAD_MS
    start = registration_epoch - PROBE_AT_S
    if time.time() < start:
        print(f"📡 Will measure SIS's clock at {datetime.datetime.fromtimestamp(start).strftime('%H:%M:%S')}...")
        while time.time() < start:
            time.sleep(0.5)
    browser, lock, _, label, _ = ready[0]
    print(f"📡 [{label}] Measuring the lead (about {min(probes, int(deadline - time.time()))} s)...")
    try:
        r = measure_lead_in_browser(browser, lock, probes=probes, deadline=deadline)
    except Exception as e:
        print(f"⚠️ Lead measurement failed ({type(e).__name__}: {e}) — using {SERVER_LEAD_MS} ms.")
        return SERVER_LEAD_MS
    print(f"📡 Lead {r['lead_ms']:+.0f} ms (±{r['plus_minus_ms']:.0f}), {r['agree']}/{r['total']} probes agree, "
          f"RTT {r['rtt_ms']:.0f} ms, HTTP {r.get('status')}, cache {r.get('cf_cache')}, via {r.get('url')}")
    if r["total"] < 10 or r["agree"] < 0.75 * r["total"]:
        print(f"⚠️ Too few probes agree — using {SERVER_LEAD_MS} ms.")
        return SERVER_LEAD_MS
    if abs(r["lead_ms"] - SERVER_LEAD_MS) > MAX_LEAD_SHIFT_MS:
        print(f"⚠️ {r['lead_ms']:.0f} ms is more than {MAX_LEAD_SHIFT_MS} ms from the usual {SERVER_LEAD_MS} ms — "
              f"not trusting it, using {SERVER_LEAD_MS} ms.")
        return SERVER_LEAD_MS
    lead = round(r["lead_ms"])
    print(f"✅ Using measured lead {lead} ms.")
    return lead


def read_fire_record(browser):
    raw = browser.execute_script("return sessionStorage.getItem('sisFire');")
    return json.loads(raw) if raw else None


def fire_at_time(browser, lock, stop_event, label, fire_epoch, dry_run=False, results=None):
    outcome = {"label": label, "target": fire_epoch}
    try:
        while time.time() < fire_epoch - ARM_LEAD_S:
            time.sleep(0.05)

        with lock:
            dismiss_alert(browser, label)   # an open alert would freeze the page's timers
            browser.execute_script(ARM_JS, fire_epoch * 1000, dry_run, REGISTER_ID, WARM_LEAD_MS, SELECT_ALL_ID)
        print(f"🎯 [{label}] Armed.")

        # Shortly before firing, make sure a page reload hasn't wiped the timer; re-arm if it has.
        while time.time() < fire_epoch - 0.4:
            time.sleep(0.02)
        with lock:
            try:
                dismiss_alert(browser, label)
                if not browser.execute_script("return window.__sisArmed === true;"):
                    print(f"⚠️ [{label}] Page reloaded since arming — re-arming.")
                    browser.execute_script(ARM_JS, fire_epoch * 1000, dry_run, REGISTER_ID, WARM_LEAD_MS, SELECT_ALL_ID)
            except WebDriverException as e:
                print(f"⚠️ [{label}] Re-arm check failed ({type(e).__name__}); fallback will cover it.")

        while time.time() < fire_epoch + 0.5:
            time.sleep(0.02)

        with lock:
            dismiss_alert(browser, label)   # an alert that popped in the last seconds blocks the timer
            try:
                rec = read_fire_record(browser)
            except WebDriverException:
                rec = None
            if rec is None or "error" in rec:
                # The in-page timer was lost (page reloaded?) — click right now instead.
                print(f"⚠️ [{label}] In-page timer didn't fire ({rec}); clicking directly.")
                rec = browser.execute_script(FALLBACK_JS, REGISTER_ID, dry_run, SELECT_ALL_ID)

        if rec is None:
            print(f"❌ [{label}] Register button not found at fire time!")
            outcome["error"] = "button not found"
        else:
            late_ms = rec["fired"] - fire_epoch * 1000
            verb = "Would have clicked" if dry_run else "Clicked"
            extra = " (fallback)" if rec.get("fallback") else ""
            print(f"🚀 [{label}] {verb} Register {late_ms:+.0f} ms vs. target{extra}")
            outcome.update(fired=rec["fired"] / 1000, fallback=bool(rec.get("fallback")))
    except Exception as e:
        print(f"❌ [{label}] Fire failed: {type(e).__name__}: {e}")
        outcome["error"] = repr(e)
    stop_event.set()
    if results is not None:
        results.append(outcome)
    return outcome


def report_result(browser, label, index):
    try:
        dismiss_alert(browser, label)
        WebDriverWait(browser, 120).until(lambda d: d.execute_script("return document.readyState") == "complete")
        text = browser.execute_script("return document.body ? document.body.innerText : ''") or ""
        snippet = " ".join(text.split())[:200]
        browser.save_screenshot(f"result_browser{index}.png")
        print(f"📄 [{label}] {snippet}")
    except Exception as e:
        print(f"⚠️ [{label}] Couldn't read result page: {type(e).__name__}")


def run_bracket(ready, registration_epoch, lead_ms, dry_run=False):
    """ready: list of (browser, lock, stop_event, label, offset_ms). Fires each at its own time in parallel."""
    results, threads = [], []
    for browser, lock, stop, label, offset_ms in ready:
        fire_epoch = registration_epoch + (offset_ms - lead_ms) / 1000.0
        print(f"⏳ [{label}] Will fire at {datetime.datetime.fromtimestamp(fire_epoch).strftime('%H:%M:%S.%f')[:-3]}")
        t = threading.Thread(target=fire_at_time, args=(browser, lock, stop, label, fire_epoch, dry_run, results))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# --------------------------
# Main
# --------------------------
def parse_registration_time(time_input):
    registration_time = parser.parse(time_input)
    now = datetime.datetime.now()
    registration_time = registration_time.replace(year=now.year, month=now.month, day=now.day)
    if registration_time <= now:
        registration_time += datetime.timedelta(days=1)
    return registration_time


def main():
    ap = argparse.ArgumentParser(description="JHU SIS auto-register")
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything (log in, load cart, tick Select All, arm timers) but never click Register")
    ap.add_argument("--time", help=f"registration time, 24-hour HH:MM (default {DEFAULT_TIME})")
    ap.add_argument("--no-probe", action="store_true",
                    help=f"skip measuring SIS's clock and use the fixed {SERVER_LEAD_MS} ms lead")
    ap.add_argument("--browsers", type=int, default=NUM_BROWSERS,
                    help=f"number of browser instances to launch, 1-{NUM_BROWSERS} (default {NUM_BROWSERS})")
    args = ap.parse_args()
    if not 1 <= args.browsers <= NUM_BROWSERS:
        ap.error(f"--browsers must be between 1 and {NUM_BROWSERS}")
    offsets = OFFSETS_MS[:args.browsers]

    mode = " (DRY RUN — Register will NOT be clicked)" if args.dry_run else ""
    print(f"📝 Setting up SIS bot with {len(offsets)} browser instance(s){mode}...")
    print(f"🎯 Fire offsets (SIS clock): {offsets} ms")
    time_input = args.time or input(f"⏰ Time to register (24-hour HH:MM) [{DEFAULT_TIME}]: ").strip() or DEFAULT_TIME
    registration_time = parse_registration_time(time_input)
    print(f"⏰ Registering at {registration_time.strftime('%a %b %d %H:%M:%S')}")
    registration_epoch = registration_time.timestamp()

    login_time = registration_time - datetime.timedelta(minutes=LOGIN_LEAD_MIN)
    if datetime.datetime.now() >= login_time:
        print(f"⚡ Already within {LOGIN_LEAD_MIN} minutes of registration time — logging in immediately!")
    else:
        print(f"⏳ Waiting until {login_time.strftime('%H:%M:%S')} to begin login...")
        while datetime.datetime.now() < login_time:
            time.sleep(1)

    # Log in sequentially (each needs manual 2FA), then keep each session alive until firing.
    ready = []
    for i, offset_ms in enumerate(offsets):
        label = f"Browser {i+1} ({offset_ms:+d}ms)"
        print(f"\n{'='*50}\n🚀 Setting up {label}\n{'='*50}")
        browser = create_browser()
        try:
            login_browser(browser, label)
            if not navigate_to_cart(browser, label):
                print(f"❌ [{label}] Failed to set up. Skipping this instance.")
                browser.quit()
                continue
        except Exception as e:
            print(f"❌ [{label}] Setup failed: {e}")
            browser.quit()
            continue
        lock, stop = threading.Lock(), threading.Event()
        threading.Thread(target=stay_logged_in_watcher, args=(browser, label, lock, stop), daemon=True).start()
        ready.append((browser, lock, stop, label, offset_ms))

    if not ready:
        print("❌ No browsers set up successfully. Exiting.")
        sys.exit(1)

    print(f"\n{'='*50}\n✅ {len(ready)} browser(s) ready!")
    print(f"⏳ Waiting for registration at {registration_time.strftime('%H:%M:%S')}...\n{'='*50}")

    lead_ms = SERVER_LEAD_MS if args.no_probe else choose_lead(ready, registration_epoch)
    print(f"🕒 Local send times vs. registration time: {[o - lead_ms for o in offsets]} ms")
    run_bracket(ready, registration_epoch, lead_ms, dry_run=args.dry_run)

    if not args.dry_run:
        print("\n📄 Results:")
        for i, (browser, _, _, label, _) in enumerate(ready, start=1):
            report_result(browser, label, i)
    print("\n🏁 All browsers have fired! Check each window for results.")


if __name__ == "__main__":
    main()
