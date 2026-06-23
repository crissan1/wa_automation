"""Diagnostic: check whether a phone number is reachable on WhatsApp.

Usage (STOP the worker first — they share the same session DB):
    .venv/bin/python3 check_number.py 5511999999999

Prints whether the number is registered and the JID the server resolves it to.
"""
import os
import sys
import traceback

from neonize.client import NewClient
from neonize.events import ConnectedEv

import db

if len(sys.argv) < 2:
    print("usage: python3 check_number.py <number-with-country-code-digits-only>")
    raise SystemExit(1)

number = "".join(c for c in sys.argv[1] if c.isdigit())
client = NewClient(str(db.BASE_DIR / "wa_session.db"))


@client.event(ConnectedEv)
def on_connected(_, __):
    try:
        results = client.is_on_whatsapp(number)
        if not results:
            print(f"No result for {number} (server returned nothing).")
        for r in results:
            jid = getattr(r, "JID", None)
            jid_str = f"{jid.User}@{jid.Server}" if jid and jid.User else "(none)"
            print(f"query={getattr(r, 'Query', number)}  on_whatsapp={getattr(r, 'IsIn', '?')}  jid={jid_str}")
    except Exception:
        traceback.print_exc()
    finally:
        os._exit(0)


print(f"[check] connecting to verify {number} ...")
client.connect()
