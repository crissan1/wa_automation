"""WhatsApp worker: owns the neonize connection, polls the queue, sends messages.

First run: this process prints a QR code to the log. Scan it from your phone
(WhatsApp -> Settings -> Linked Devices -> Link a device) to pair the Pi. The
session is stored in `wa_session.db` and persists across restarts.

NOTE: neonize wraps the Go `whatsmeow` library. Method names below match the
current neonize API; if your installed version differs, the WhatsApp calls are
all isolated in `send_one()` and `refresh_groups()` so adjustments stay local.
"""
import threading
import time
import traceback

from neonize.client import NewClient
from neonize.events import ConnectedEv
from neonize.utils import build_jid

import db

SESSION_DB = str(db.BASE_DIR / "wa_session.db")
POLL_INTERVAL = 30          # seconds between queue checks
GROUP_REFRESH = 600         # seconds between group-list refreshes
SEND_DELAY = (3, 8)         # min/max random-ish pause before each send (human-like)

client = NewClient(SESSION_DB)
_last_group_refresh = 0.0


def jid_from_string(s: str):
    """'5511999@s.whatsapp.net' -> JID object via build_jid(user, server)."""
    user, _, server = s.partition("@")
    return build_jid(user, server or "s.whatsapp.net")


def refresh_groups():
    """Cache joined groups so the web UI can offer a picker."""
    try:
        groups = client.get_joined_groups()
        rows = []
        for g in groups:
            jid = f"{g.JID.User}@{g.JID.Server}"
            name = getattr(getattr(g, "GroupName", None), "Name", "") or jid
            rows.append((jid, name))
        if rows:
            db.upsert_groups(rows)
            print(f"[worker] cached {len(rows)} groups")
    except Exception:
        print("[worker] group refresh failed:\n" + traceback.format_exc())


def resolve_number(target_jid: str):
    """Resolve a phone-number JID via the server before sending.

    This populates whatsmeow's LID mapping (avoids the "no LID found" error)
    and lets us fail clearly if the number isn't actually on WhatsApp.
    """
    number = target_jid.split("@", 1)[0]
    results = client.is_on_whatsapp(number)
    for r in results:
        if getattr(r, "IsIn", False):
            jid = r.JID
            return build_jid(jid.User, jid.Server)
    raise ValueError(f"{number} is not a WhatsApp user (check the number/format)")


def send_one(msg: dict):
    """Send a single queued message (text and/or attachments) to its target."""
    if msg["target_type"] == "number":
        to = resolve_number(msg["target_jid"])
    else:
        to = jid_from_string(msg["target_jid"])
    body = msg.get("body") or ""
    attachments = msg.get("attachments", [])

    # Text first (so document captions, which WhatsApp hides, aren't lost)
    if body:
        client.send_message(to, body)

    for att in attachments:
        path = att["path"]
        mime = (att.get("mimetype") or "").lower()
        if mime.startswith("image/"):
            client.send_image(to, path)
        elif mime.startswith("video/"):
            client.send_video(to, path)
        elif mime.startswith("audio/"):
            client.send_audio(to, path)
        else:
            client.send_document(to, path, filename=att["filename"])


def poll_loop():
    global _last_group_refresh
    print("[worker] poll loop started")
    while True:
        try:
            now = time.time()
            if now - _last_group_refresh > GROUP_REFRESH:
                refresh_groups()
                _last_group_refresh = now

            for msg in db.due_messages():
                try:
                    # small human-like pause; spreads out bursts to lower ban risk
                    time.sleep(SEND_DELAY[0] + (msg["id"] % (SEND_DELAY[1] - SEND_DELAY[0] + 1)))
                    send_one(msg)
                    db.mark_sent(msg["id"])
                    print(f"[worker] sent message {msg['id']} -> {msg['display_name']}")
                except Exception as e:
                    db.mark_failed(msg["id"], e)
                    print(f"[worker] FAILED message {msg['id']}: {e}")
        except Exception:
            print("[worker] poll loop error:\n" + traceback.format_exc())
        time.sleep(POLL_INTERVAL)


@client.event(ConnectedEv)
def on_connected(_, __):
    print("[worker] connected to WhatsApp")
    refresh_groups()
    threading.Thread(target=poll_loop, daemon=True).start()


def main():
    db.init_db()
    print("[worker] connecting... (first run: scan the QR code below)")
    client.connect()  # blocks; prints QR to stdout if not yet paired


if __name__ == "__main__":
    main()
