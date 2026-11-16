"""
Clicks the Register button in your normal browser, once, at the scheduled time.

You log in yourself (Cloudflare check, 2FA), open My Cart, tick Select All, and point at Register.
The script then:
  * checks your Mac's clock against time.apple.com and corrects the click time for any drift
    (again ~60 s before, since the clock drifts), so the click lands at true 7:00:00.000
  * keeps the Mac and display awake while it waits
  * makes sure the browser is the front window a few seconds before (otherwise the first click
    would only bring the window forward instead of pressing the button)
  * sends one click straight through macOS (no osascript start-up delay), timed to the millisecond

    python3 manual_click_register.py                 # 07:00, asks to confirm
    python3 manual_click_register.py --time 07:00:00 --offset-ms 0
    python3 manual_click_register.py --practice      # rehearse on a local test page, ~45 s from now
    python3 manual_click_register.py --dry-run       # safe test on real SIS: points at the spot but never clicks

Needs Accessibility permission for the app you run it from (System Settings > Privacy & Security >
Accessibility > Terminal). No packages needed beyond Python 3.
"""
import argparse
import ctypes
import datetime
import os
import re
import subprocess
import sys
import tempfile
import time

DEFAULT_TIME = "07:00"
TIME_SERVERS = ["time.apple.com", "time.nist.gov"]
RECHECK_CLOCK_S = 60      # re-measure the clock this long before the click
FRONT_CHECK_S = 3         # make sure the browser is in front this long before the click
MAX_CLOCK_FIX_S = 2.0     # an "offset" bigger than this is treated as a bad reading, not applied

# ---------------------------------------------------------------- macOS mouse events (CoreGraphics)
_CG = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
_CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_CG.CGEventCreate.restype = ctypes.c_void_p
_CG.CGEventCreate.argtypes = [ctypes.c_void_p]
_CG.CGEventGetLocation.restype = CGPoint
_CG.CGEventGetLocation.argtypes = [ctypes.c_void_p]
_CG.CGEventCreateMouseEvent.restype = ctypes.c_void_p
_CG.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
_CG.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
_CG.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_CG.CGPreflightPostEventAccess.restype = ctypes.c_bool
_CG.CGRequestPostEventAccess.restype = ctypes.c_bool
_CF.CFRelease.argtypes = [ctypes.c_void_p]

MOUSE_DOWN, MOUSE_UP, MOUSE_MOVED = 1, 2, 5
HID_TAP, LEFT_BUTTON, CLICK_STATE_FIELD = 0, 0, 1


def mouse_position():
    ev = _CG.CGEventCreate(None)
    p = _CG.CGEventGetLocation(ev)
    _CF.CFRelease(ev)
    return round(p.x), round(p.y)


def _post(kind, x, y):
    ev = _CG.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), LEFT_BUTTON)
    if kind != MOUSE_MOVED:
        _CG.CGEventSetIntegerValueField(ev, CLICK_STATE_FIELD, 1)
    _CG.CGEventPost(HID_TAP, ev)
    _CF.CFRelease(ev)


def can_post_clicks():
    return bool(_CG.CGPreflightPostEventAccess())


# ---------------------------------------------------------------- helpers
def fmt(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime("%a %b %d %H:%M:%S.%f")[:-3]


def parse_time(text):
    """HH:MM or HH:MM:SS[.fff], today, or tomorrow if that's already past."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?", text.strip())
    if not m:
        sys.exit(f"Couldn't read time {text!r}; use 24-hour HH:MM, e.g. 07:00")
    h, mi, s = int(m[1]), int(m[2]), float(m[3] or 0)
    now = datetime.datetime.now()
    t = now.replace(hour=h, minute=mi, second=int(s), microsecond=int(round((s % 1) * 1e6)))
    if t <= now:
        t += datetime.timedelta(days=1)
    return t.timestamp()


def clock_offset():
    """(offset_s, plus_minus_s, server): true time minus this Mac's time. None if no server answered."""
    for server in TIME_SERVERS:
        try:
            out = subprocess.run(["sntp", "-t", "2", server], capture_output=True, text=True, timeout=8).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        m = re.search(r"^([+-]\d+\.\d+) \+/- (\d+\.\d+)", out, re.M)
        if m:
            return float(m[1]), float(m[2]), server
    return None


def checked_offset(label):
    """Returns the clock offset in seconds, or None if there's no trustworthy reading."""
    r = clock_offset()
    if r is None:
        print(f"⚠️  {label}: couldn't reach a time server.")
        return None
    off, pm, server = r
    if abs(off) > MAX_CLOCK_FIX_S or pm > 0.1:
        print(f"⚠️  {label}: odd reading from {server} ({off*1000:+.0f} ms ±{pm*1000:.0f}) — ignoring it.")
        return None
    behind = "behind" if off > 0 else "ahead of"
    print(f"🕒 {label}: your Mac is {abs(off)*1000:.0f} ms {behind} true time (±{pm*1000:.0f} ms, {server}).")
    return off


def front_app():
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        out = subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r'"(?:LSDisplayName|name)"="([^"]+)"', out)
        return m[1] if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


TERMINAL_APPS = {"Terminal", "iTerm2", "iTerm", "Claude", "Code", "Visual Studio Code", "Warp", "Python"}


def wait_until(epoch):
    """Sleep until ~100 ms before `epoch`, then spin. The spin margin covers macOS waking a
    background process late (App Nap / timer coalescing can delay a sleep by tens of ms)."""
    while (left := epoch - time.time()) > 0.1:
        time.sleep(min(left - 0.1, 0.5))
    while time.time() < epoch:
        pass


PRACTICE_HTML = """<!doctype html><meta charset="utf-8"><title>Register practice</title>
<style>body{font:18px -apple-system,sans-serif;margin:40px;background:#fff;color:#111}
#b{font-size:22px;padding:14px 40px;margin:30px 0}#r{font-size:28px;font-weight:600}</style>
<h2>Practice: fake "Register" button</h2>
<p>Scheduled click (your Mac's clock): <b>%(when)s</b>. Countdown: <b id="c"></b></p>
<p>Click once on blank space here, then rest the pointer on the button and leave it.</p>
<input type="image" id="b" alt="Register" onclick="hit(event)" style="border:2px solid #333;background:#e8e8e8;width:180px;height:50px">
<p id="r"></p>
<script>
const target = %(target)f;
let down = null;
document.getElementById('b').addEventListener('mousedown', () => down = Date.now());
function hit(e) {
  e.preventDefault();
  const t = Date.now();
  document.getElementById('r').textContent =
    `Click registered ${(t - target >= 0 ? '+' : '') + (t - target)} ms vs. scheduled time` +
    (down ? ` (button pressed ${down - target >= 0 ? '+' : ''}${down - target} ms)` : '');
}
setInterval(() => { const s = (target - Date.now()) / 1000;
  document.getElementById('c').textContent = s > 0 ? s.toFixed(1) + ' s' : 'done'; }, 100);
</script>"""


def open_practice_page(click_epoch, browser_app):
    path = os.path.join(tempfile.mkdtemp(), "register_practice.html")
    with open(path, "w") as f:
        f.write(PRACTICE_HTML % {"target": click_epoch * 1000, "when": fmt(click_epoch)})
    subprocess.run(["open", "-a", browser_app, path], check=False)
    return path


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Click Register in your normal browser at the scheduled time.")
    ap.add_argument("--time", help=f"registration time, 24-hour HH:MM or HH:MM:SS (default {DEFAULT_TIME})")
    ap.add_argument("--offset-ms", type=int, default=0,
                    help="shift the click: positive = later, negative = earlier (default 0 = exactly on time)")
    ap.add_argument("--no-clock-check", action="store_true", help="don't correct for your Mac's clock drift")
    ap.add_argument("--dry-run", action="store_true",
                    help="safe test on the real SIS: everything except the click. At the click time it moves the "
                         "pointer onto the captured spot (no press) so you can see it is on Register")
    ap.add_argument("--practice", action="store_true",
                    help="rehearse: opens a local test page in your browser and clicks it ~45 s from now")
    ap.add_argument("--browser", default="Google Chrome", help="browser app name (default Google Chrome)")
    args = ap.parse_args()

    if args.dry_run and not can_post_clicks():
        print("⚠️  No Accessibility permission: the dry run can't move the pointer, and a real run would refuse to start.")
    elif not args.dry_run and not can_post_clicks():
        _CG.CGRequestPostEventAccess()   # shows macOS's permission prompt
        sys.exit("This app needs Accessibility permission to click: System Settings > Privacy & Security > "
                 "Accessibility > turn on Terminal (or whatever you run this from). Then quit and reopen it and run again.")

    if args.practice:
        target = time.time() + 45
    else:
        text = args.time or input(f"⏰ Registration time (24-hour HH:MM) [{DEFAULT_TIME}]: ").strip() or DEFAULT_TIME
        target = parse_time(text)

    # Keep the Mac and display awake (a locked screen would eat the click) until this script exits.
    subprocess.Popen(["caffeinate", "-dims", "-w", str(os.getpid())])

    offset = 0.0 if args.no_clock_check else (checked_offset("Clock check") or 0.0)
    if not args.no_clock_check and offset == 0.0:
        print("   Clicking by your Mac's clock uncorrected.")

    def click_epoch():
        # true target + your shift, converted to this Mac's clock
        return target + args.offset_ms / 1000 - offset

    print(f"🎯 Target (true time): {fmt(target + args.offset_ms / 1000)}"
          f"{f'  [{args.offset_ms:+d} ms offset]' if args.offset_ms else ''}")
    print(f"🖱  Click at (your Mac's clock): {fmt(click_epoch())}")
    if click_epoch() - time.time() > 12 * 3600:
        print("⚠️  That's more than 12 hours away — check the time you entered.")

    if args.practice:
        open_practice_page(click_epoch(), args.browser)
        print(f"🧪 Practice page opened in {args.browser}.")
    else:
        print(f"\nIn {args.browser} (your normal one): log in, pass the Cloudflare check and 2FA,")
        print("open My Cart, and tick Select All. Don't scroll or move the window after this.")

    # Capture the button position with the browser in front (not Terminal).
    while True:
        input("\nPress ENTER, then within 5 s: click blank space on the page and rest the pointer on Register...")
        for i in range(5, 0, -1):
            print(f"   {i}...", end="\r", flush=True)
            time.sleep(1)
        x, y = mouse_position()
        app = front_app()
        print(f"📍 Captured ({x}, {y}) with '{app}' in front.        ")
        if app and app not in TERMINAL_APPS:
            break
        print(f"⚠️  '{app}' was in front, not the browser — the click would just switch windows. Try again.")
    browser_app = app if app and app not in TERMINAL_APPS else args.browser

    if click_epoch() - time.time() < 1:
        sys.exit("❌ The click time has already passed.")
    print(f"⏳ Waiting until {fmt(click_epoch())}. Hands off the mouse and keyboard."
          f"{'  (DRY RUN — no click)' if args.dry_run else ''}")

    def recheck_clock():
        nonlocal offset
        new = checked_offset("Clock re-check")
        if new is None:
            print("   Keeping the earlier correction.")
        else:
            offset = new
        print(f"🖱  Click at (your Mac's clock): {fmt(click_epoch())}")

    def bring_browser_front():
        now_front = front_app()
        if now_front != browser_app:
            print(f"⚠️  '{now_front}' is in front — bringing {browser_app} back.")
            subprocess.run(["open", "-a", browser_app], check=False)

    if not args.no_clock_check and click_epoch() - time.time() > RECHECK_CLOCK_S + 10:
        wait_until(click_epoch() - RECHECK_CLOCK_S)
        recheck_clock()
    wait_until(click_epoch() - FRONT_CHECK_S)
    bring_browser_front()
    moved = mouse_position() != (x, y)

    fire = click_epoch()
    if args.dry_run:
        wait_until(fire)
        t = time.time()
        if can_post_clicks():
            _post(MOUSE_MOVED, x, y)      # point at the spot, no press
        print(f"✅ Dry run: would have clicked at {fmt(t)} ({(t - fire)*1000:+.1f} ms). Nothing was clicked.")
        if moved:
            print("⚠️  The pointer had moved since capture.")
        print("👀 Look now: the pointer is where the real click would land.")
        return
    _post(MOUSE_MOVED, x, y)              # put the pointer back on the button if it drifted
    wait_until(fire - 0.010)
    _post(MOUSE_DOWN, x, y)               # press 10 ms early; the click registers on release
    wait_until(fire)
    _post(MOUSE_UP, x, y)
    t = time.time()
    print(f"🚀 Clicked at {fmt(t)} ({(t - fire)*1000:+.1f} ms vs. schedule).")
    if moved:
        print("⚠️  The pointer had moved since capture; it was moved back to the button first.")
    print("\nIf SIS says registration isn't open yet, click Register again yourself right away.")
    print("Don't let anything click a second time automatically — the button may have moved.")


if __name__ == "__main__":
    main()
