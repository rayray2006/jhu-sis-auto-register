# JHU SIS Registration Click

`manual_click_register.py` clicks SIS's **Register** button once, at exactly the registration time.

You log in to [JHU SIS](https://sis.jhu.edu/sswf/) yourself in your normal browser and open your cart. The script
records where the Register button is on screen, then clicks it with a real OS-level mouse click at the scheduled
time. The time is corrected against an internet time server (`sntp`), so your Mac's clock drift doesn't matter.

Logging in yourself avoids SIS's Cloudflare check, which blocks automated (Selenium) browsers.

---

## Requirements

- **macOS**
- **Python 3** (no packages to install)
- Terminal allowed to control the mouse: **System Settings → Privacy & Security → Accessibility**, turn on
  Terminal (or whichever terminal app you use). The script tells you if it's missing.

---

## Usage

```bash
python3 manual_click_register.py --practice   # rehearse on a local test page, 45 s out
python3 manual_click_register.py --dry-run    # moves the mouse there at 07:00 but never clicks
python3 manual_click_register.py              # real run: clicks at 07:00
python3 manual_click_register.py --time 07:30 # another time (HH:MM or HH:MM:SS.fff)
```

1. About 10 minutes before registration, log in to SIS and open **My Cart**. Tick **Select All**.
2. Run the script and press Enter. You get 5 seconds to put the mouse over the **Register** button.
3. Leave the mouse and the window alone. The script keeps the Mac awake, brings the browser to the front
   3 seconds before, and clicks.
4. If SIS says registration isn't open yet, click Register again yourself.

Other options: `--offset-ms N` shifts the click N ms later (or earlier, if negative), `--browser NAME` sets which
app to bring to the front (default "Google Chrome"), `--no-clock-check` skips the time-server correction.

---

## Tips

- Turn on Do Not Disturb so a notification can't cover the button.
- Keep the Mac plugged in with the lid open.
- Don't scroll, zoom, or resize the browser window after recording the position.
- You **must** have already added your classes to the cart.

---

## License

[MIT License](LICENSE). Based on Charissa's original Selenium bot,
[charissa-luk/jhu-sis-auto-register](https://github.com/charissa-luk/jhu-sis-auto-register).
