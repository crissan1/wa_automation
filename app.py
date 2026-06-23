"""FastAPI web app: compose + schedule WhatsApp messages, view/cancel the queue.

This process NEVER talks to WhatsApp. It only reads/writes the SQLite queue.
The worker (worker.py) does the actual sending.
"""
import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, File, UploadFile, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db

app = FastAPI(title="WA Scheduler")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Format epoch seconds as readable Pi-local time in templates: {{ ts | datetime }}
templates.env.filters["datetime"] = lambda ts: datetime.fromtimestamp(ts).strftime("%a %d %b, %H:%M")


@app.on_event("startup")
def _startup():
    db.init_db()


def number_to_jid(raw: str) -> str:
    """Normalize a phone number to a WhatsApp user JID.

    Strip everything but digits; the number must include the country code
    (e.g. 55 for Brazil) but no '+' or spaces.
    """
    digits = re.sub(r"\D", "", raw)
    return f"{digits}@s.whatsapp.net"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "messages": db.list_messages(),
            "groups": db.list_groups(),
            "now_local": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        },
    )


@app.post("/schedule")
async def schedule(
    target_kind: str = Form(...),          # 'number' or 'group'
    number: str = Form(""),
    group_jid: str = Form(""),
    body: str = Form(""),
    scheduled_at: str = Form(...),         # from <input type="datetime-local">
    files: list[UploadFile] = File(default=[]),
):
    # Resolve target
    if target_kind == "group":
        target_jid = group_jid
        display_name = next(
            (g["name"] for g in db.list_groups() if g["jid"] == group_jid), group_jid
        )
    else:
        target_jid = number_to_jid(number)
        display_name = number.strip()

    # datetime-local has no timezone -> interpret as the Pi's local time
    sched_epoch = int(datetime.fromisoformat(scheduled_at).timestamp())

    msg_id = db.create_message(target_kind, target_jid, display_name, body.strip(), sched_epoch)

    # Save attachments to disk
    for f in files:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix
        stored = db.UPLOAD_DIR / f"{msg_id}_{uuid.uuid4().hex}{ext}"
        data = await f.read()
        stored.write_bytes(data)
        mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
        db.add_attachment(msg_id, stored, f.filename, mime)

    return RedirectResponse("/", status_code=303)


@app.post("/cancel/{message_id}")
def cancel(message_id: int):
    db.cancel_message(message_id)
    return RedirectResponse("/", status_code=303)
