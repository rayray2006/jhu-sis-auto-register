# 💾 JHU SIS Auto Registration Bot

This Python script automates course registration on [JHU SIS](https://sis.jhu.edu/sswf/) using Selenium. It logs in via Microsoft SSO, navigates to the registration cart, selects all classes, and clicks register at your specified time with a 10-minute pre-login buffer since SIS is slow.

---

## Features
- Logs in with your JHU Microsoft credentials
- Navigates to the registration cart
- Waits for the exact time to register
- Selects all classes and clicks "Register"
- Handles SIS slowness with retries and waits
- Interactive terminal prompts for credentials and time

---

## Requirements

- **macOS** (tested; Windows not guaranteed)
- **Python 3.7+**
- **Google Chrome** (latest version)
- **ChromeDriver** matching your Chrome version  
  Download: [https://sites.google.com/chromium.org/driver/](https://sites.google.com/chromium.org/driver/)

---

## Installation & Setup

### 1. Clone the Repo
```bash
git clone https://github.com/charissa-luk/jhu-sis-auto-register.git
cd jhu-sis-auto-register
```

### 2. Create & Activate a Virtual Environment (Recommended)
```bash
python3 -m venv sis-bot-venv
source sis-bot-venv/bin/activate
```

### 3. Install Required Packages
```bash
pip install selenium python-dateutil
```

---

## ⏲Sync Your Clock (macOS Only)

SIS uses the US Naval Observatory (NIST) clock. You must sync your Mac to match this time source:

1. Open **System Preferences** → **Date & Time**
2. In the **Time Server** field, click **Set** and enter your Mac password
3. Change the server from `time.apple.com` ➜ `tick.usno.navy.mil`
4. Click the lock again to save your changes

---

## Prevent Mac From Sleeping (Optional but Recommended)

To prevent your Mac from sleeping before registration time, install and use **Caffeine** or the built-in `caffeinate` command:

### Option A: Using Terminal (Recommended)
If it’s midnight and you want to keep the system awake until 7:30 AM (7.5 hours = 27000 seconds):
```bash
caffeinate -t 27000
```
Make sure your computer is plugged in!

### Option B: Install Caffeine App
Install with Homebrew:
```bash
brew install --cask caffeine
```
Website: [https://intelliscapesolutions.com/apps/caffeine](https://intelliscapesolutions.com/apps/caffeine)

If you're on an M1/M2/M3 Mac, note that **Rosetta 2** may be required:
```bash
softwareupdate --install-rosetta --agree-to-license
```

## Run the Bot
From the repo folder:
```bash
python sis_register.py
```

The script asks for your **JHU email** and **password** unless it finds them in the `SIS_EMAIL` / `SIS_PASSWORD`
environment variables or in `sis_credentials.py` (copy `sis_credentials.example.py`; it's gitignored, so it
never gets committed). Registration time defaults to 07:00; change it with `--time HH:MM`.

### Single-click alternative (manual login)
SIS's Cloudflare check blocks automated Chrome windows. `manual_click_register.py` lets you log in yourself in
your normal browser, records where the Register button is on screen, and clicks it once at exactly the
registration time (clock-corrected with `sntp`). No dependencies beyond Python 3:
```bash
python3 manual_click_register.py --practice   # rehearse on a local test page
python3 manual_click_register.py --dry-run    # moves the mouse there at 7:00 but never clicks
python3 manual_click_register.py              # real run, clicks at 07:00
```
It needs Terminal to be allowed under System Settings > Privacy & Security > Accessibility.

> The bot logs in **10 minutes before** the registration time and waits.

---

## Notes

- SIS is very slow — this bot uses long wait times (up to 2 mins per element)
- You **must** have already added classes to your Enrollment Cart
- If you use **2-Factor Authentication (2FA)**:
  - The script will **not work** through the 2FA page
  - **Workaround**: log into your Microsoft account **manually** earlier that day, so 2FA won’t be triggered
 - If you encounter a **"Continue to SIS"** button after login, it's likely due to an active SIS survey.  
  → You can safely **comment out the "Continue to SIS" code block** if you don't see that page.

---

## Troubleshooting

- 🧳 Make sure **Google Chrome** and **ChromeDriver** are:
  - Fully up to date
  - Exactly the same version

---

## License

[MIT License](LICENSE)

---

## Credits

Created by Charissa. Feel free to fork and improve.
