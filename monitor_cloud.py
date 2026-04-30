"""
Cloud version of the STWDO monitor — runs on GitHub Actions.
Reads credentials from environment variables (GitHub Secrets).
No desktop/sound notifications (email + ntfy only).
"""

import difflib
import hashlib
import logging
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
STATE_DIR = BASE_DIR / "state"
HASH_FILE = STATE_DIR / "last_hash.txt"
TEXT_FILE = STATE_DIR / "last_content.txt"

URL = "https://www.stwdo.de/wohnen/aktuelle-wohnangebote"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Page fetching ──────────────────────────────────────────────────────────────
def fetch_offer_text() -> str:
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta", "link", "svg", "img"]):
        tag.decompose()

    section = (
        soup.find(id="residential-offer-list")
        or soup.find("section", class_=lambda c: c and "offer" in c.lower())
        or soup.find("main")
        or soup.body
    )
    return section.get_text(separator="\n", strip=True) if section else soup.get_text()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> tuple[str | None, str | None]:
    STATE_DIR.mkdir(exist_ok=True)
    prev_hash    = HASH_FILE.read_text().strip() if HASH_FILE.exists() else None
    prev_content = TEXT_FILE.read_text(encoding="utf-8") if TEXT_FILE.exists() else None
    return prev_hash, prev_content


def save_state(text: str, hash_value: str) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    HASH_FILE.write_text(hash_value)
    TEXT_FILE.write_text(text, encoding="utf-8")


# ── Diff ───────────────────────────────────────────────────────────────────────
def build_diff_summary(old: str, new: str) -> str:
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))
    if not diff:
        return "(no line-level diff available)"
    added   = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    parts = []
    if added:
        parts.append("NEW / CHANGED LINES:\n" + "\n".join(f"  + {l}" for l in added[:30]))
    if removed:
        parts.append("REMOVED LINES:\n"       + "\n".join(f"  - {l}" for l in removed[:10]))
    return "\n\n".join(parts) or "(content shifted but no clear diff)"


# ── Notifications ──────────────────────────────────────────────────────────────
def notify_ntfy(title: str, message: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        log.warning("NTFY_TOPIC secret not set — skipping.")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": "urgent",
                "Tags":     "house,bell,rotating_light",
                "Click":    URL,
            },
            timeout=10,
        )
        log.info(f"ntfy.sh notification sent → topic '{topic}'.")
    except Exception as e:
        log.warning(f"ntfy.sh failed: {e}")


def notify_email(subject: str, plain: str, html: str) -> None:
    address  = os.environ.get("EMAIL_ADDRESS", "")
    password = os.environ.get("EMAIL_APP_PASSWORD", "")
    if not address or not password:
        log.warning("EMAIL_ADDRESS or EMAIL_APP_PASSWORD secret not set — skipping.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = address
        msg["To"]      = address
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(address, password)
            srv.send_message(msg)
        log.info(f"Email sent to {address}.")
    except Exception as e:
        log.warning(f"Email failed: {e}")


def notify_all(old_content: str, new_content: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    diff_text = build_diff_summary(old_content, new_content)
    title     = "Dormitory Offer Changed!"
    short_msg = f"New content detected on STWDO offers page.\nChecked at {timestamp}"

    plain = f"{short_msg}\n\nURL: {URL}\n\n--- WHAT CHANGED ---\n{diff_text}"
    html  = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
  <h2 style="color:#e63946">Dormitory Offer Changed!</h2>
  <p>The <strong>STWDO residential offers page</strong> has new content.</p>
  <p><a href="{URL}" style="background:#457b9d;color:white;padding:10px 18px;
     border-radius:5px;text-decoration:none;display:inline-block">
     View Offers Now</a></p>
  <hr>
  <h3>What changed:</h3>
  <pre style="background:#f1f1f1;padding:12px;border-radius:4px;
      white-space:pre-wrap;font-size:13px">{diff_text}</pre>
  <hr>
  <small style="color:#888">Checked at {timestamp}</small>
</body></html>"""

    notify_ntfy(title, short_msg)
    notify_email(f"[STWDO Alert] {title}", plain, html)


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 60)
    log.info("STWDO Monitor — GitHub Actions run")
    log.info(f"  URL : {URL}")
    log.info(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info("=" * 60)

    current_text = fetch_offer_text()
    current_hash = content_hash(current_text)
    prev_hash, prev_content = load_state()

    if prev_hash is None:
        save_state(current_text, current_hash)
        log.info("First run — baseline saved.")
    elif current_hash != prev_hash:
        log.info("*** CHANGE DETECTED — sending alerts ***")
        notify_all(prev_content or "", current_text)
        save_state(current_text, current_hash)
        log.info("State updated.")
    else:
        log.info("No change.")


if __name__ == "__main__":
    main()
