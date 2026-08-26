from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from werkzeug.middleware.proxy_fix import ProxyFix
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError
import csv, io, os, requests, hmac, hashlib, json, secrets
from datetime import datetime

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_env(name):
    return os.getenv(name, "").strip()


# =========================================================
# DATABASE
# =========================================================

class DBWrapper:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):
        query = query.replace("?", "%s")
        cur = self.connection.cursor(cursor_factory=RealDictCursor)
        cur.execute(query, params) if params is not None else cur.execute(query)
        return cur

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def db():
    database_url = get_env("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured in Render Environment.")
    return DBWrapper(psycopg2.connect(database_url, connect_timeout=10))


def init_db():
    c = None
    try:
        c = db()

        c.execute("""
            CREATE TABLE IF NOT EXISTS contacts(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                group_name TEXT DEFAULT 'General'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS campaigns(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                message TEXT NOT NULL,
                group_name TEXT,
                status TEXT DEFAULT 'Draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for column, dtype in [
            ("drive_file_id", "TEXT"),
            ("drive_file_name", "TEXT"),
            ("drive_mime_type", "TEXT")
        ]:
            c.execute(f"ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS {column} {dtype}")

        c.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_messages(
                id SERIAL PRIMARY KEY,
                campaign_id INTEGER,
                contact_id INTEGER,
                phone TEXT,
                message TEXT,
                wa_message_id TEXT,
                direction TEXT DEFAULT 'outgoing',
                status TEXT DEFAULT 'queued',
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for column, dtype in [
            ("attachment_name", "TEXT"),
            ("attachment_type", "TEXT")
        ]:
            c.execute(
                f"ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS {column} {dtype}"
            )

        c.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_incoming(
                id SERIAL PRIMARY KEY,
                wa_message_id TEXT UNIQUE,
                phone TEXT,
                message_type TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events(
                id SERIAL PRIMARY KEY,
                event_type TEXT,
                payload TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS google_drive_tokens(
                id INTEGER PRIMARY KEY,
                token_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.commit()
        print("DATABASE INITIALIZED SUCCESSFULLY")
        return True

    except Exception as e:
        print("DATABASE INITIALIZATION ERROR:", e)
        if c:
            try:
                c.rollback()
            except Exception:
                pass
        return False

    finally:
        if c:
            c.close()


init_db()


# =========================================================
# WHATSAPP
# =========================================================

def whatsapp_configured():
    return bool(get_env("WHATSAPP_ACCESS_TOKEN") and
                get_env("WHATSAPP_PHONE_NUMBER_ID"))


def clean_phone(phone):
    if not phone:
        return ""
    phone = str(phone).strip()
    for ch in ["+", " ", "-", "(", ")"]:
        phone = phone.replace(ch, "")
    if phone.startswith("00"):
        phone = phone[2:]
    return phone


def whatsapp_messages_url():
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")
    return f"https://graph.facebook.com/v23.0/{phone_id}/messages" if phone_id else None


def whatsapp_media_url():
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")
    return f"https://graph.facebook.com/v23.0/{phone_id}/media" if phone_id else None


def whatsapp_post(payload):
    token = get_env("WHATSAPP_ACCESS_TOKEN")
    url = whatsapp_messages_url()
    if not token or not url:
        return False, None, "WhatsApp API credentials missing"

    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        try:
            data = r.json()
        except Exception:
            data = r.text

        if r.ok:
            messages = data.get("messages", []) if isinstance(data, dict) else []
            return True, messages[0].get("id") if messages else None, data

        return False, None, data

    except Exception as e:
        return False, None, str(e)


def send_whatsapp_text(phone, body):
    phone = clean_phone(phone)
    if not phone:
        return False, None, "Invalid phone number"

    return whatsapp_post({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    })


def send_whatsapp_template(phone, template_name, language_code="en_US", parameters=None):
    phone = clean_phone(phone)
    if not phone:
        return False, None, "Invalid phone number"

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code}
        }
    }

    if parameters:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(v)} for v in parameters]
        }]

    return whatsapp_post(payload)


def upload_whatsapp_media(file_obj, filename, mime_type):
    token = get_env("WHATSAPP_ACCESS_TOKEN")
    url = whatsapp_media_url()
    if not token or not url:
        return False, None, "WhatsApp API credentials missing"

    try:
        file_obj.seek(0)
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, file_obj, mime_type or "application/octet-stream")},
            timeout=120
        )

        try:
            data = r.json()
        except Exception:
            data = r.text

        if not r.ok:
            return False, None, data

        media_id = data.get("id") if isinstance(data, dict) else None
        return (True, media_id, data) if media_id else (False, None, data)

    except Exception as e:
        return False, None, str(e)


def whatsapp_media_type(mime_type):
    mime_type = (mime_type or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "document"


def send_whatsapp_media(phone, file_obj, filename, mime_type, caption=""):
    phone = clean_phone(phone)
    if not phone:
        return False, None, "Invalid phone number"

    ok, media_id, response = upload_whatsapp_media(
        file_obj, filename, mime_type
    )
    if not ok:
        return False, None, response

    media_type = whatsapp_media_type(mime_type)
    media = {"id": media_id}

    if media_type in ("image", "video", "document") and caption:
        media["caption"] = caption

    if media_type == "document":
        media["filename"] = filename

    return whatsapp_post({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": media_type,
        media_type: media
    })


# =========================================================
# META WEBHOOK SIGNATURE
# =========================================================

def verify_meta_signature():
    secret = get_env("META_APP_SECRET")
    if not secret:
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode(),
        request.get_data(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature[7:])


# =========================================================
# GOOGLE DRIVE OAUTH
# =========================================================

def google_drive_configured():
    return bool(get_env("GOOGLE_CLIENT_ID") and get_env("GOOGLE_CLIENT_SECRET"))


def google_redirect_uri():
    value = get_env("GOOGLE_REDIRECT_URI")
    if value:
        return value.rstrip("/")

    external = get_env("RENDER_EXTERNAL_URL")
    if external:
        return external.rstrip("/") + "/google/oauth/callback"

    return url_for("google_oauth_callback", _external=True)


def google_client_config():
    return {
        "web": {
            "client_id": get_env("GOOGLE_CLIENT_ID"),
            "client_secret": get_env("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url":
                "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": [google_redirect_uri()]
        }
    }


def create_google_flow(state=None):
    flow = Flow.from_client_config(
        google_client_config(),
        scopes=GOOGLE_DRIVE_SCOPES,
        state=state
    )
    flow.redirect_uri = google_redirect_uri()
    return flow


def save_google_credentials(credentials):
    c = None
    try:
        c = db()
        c.execute("""
            INSERT INTO google_drive_tokens(id, token_json, updated_at)
            VALUES(1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id)
            DO UPDATE SET
                token_json=EXCLUDED.token_json,
                updated_at=CURRENT_TIMESTAMP
        """, (credentials.to_json(),))
        c.commit()
        return True
    except Exception as e:
        print("GOOGLE TOKEN SAVE ERROR:", e)
        if c:
            c.rollback()
        return False
    finally:
        if c:
            c.close()


def load_google_credentials():
    c = None
    try:
        c = db()
        row = c.execute("""
            SELECT token_json FROM google_drive_tokens WHERE id=1
        """).fetchone()

        if not row:
            return None

        credentials = Credentials.from_authorized_user_info(
            json.loads(row["token_json"]),
            GOOGLE_DRIVE_SCOPES
        )

        if credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleRequest())
            save_google_credentials(credentials)

        return credentials

    except Exception as e:
        print("GOOGLE TOKEN LOAD ERROR:", e)
        return None
    finally:
        if c:
            c.close()


def google_drive_service():
    credentials = load_google_credentials()
    if not credentials or not credentials.valid:
        return None

    return build(
        "drive", "v3",
        credentials=credentials,
        cache_discovery=False
    )


@app.route("/google/authorize")
def google_authorize():
    if not google_drive_configured():
        flash("GOOGLE_CLIENT_ID और GOOGLE_CLIENT_SECRET Render में add करें.")
        return redirect(url_for("settings"))

    state = secrets.token_urlsafe(32)
    session["google_oauth_state"] = state

    flow = create_google_flow(state)
    authorization_url, returned_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    session["google_oauth_state"] = returned_state
    return redirect(authorization_url)


@app.route("/google/oauth/callback")
def google_oauth_callback():
    if not google_drive_configured():
        return "Google Drive OAuth is not configured.", 400

    if request.args.get("error"):
        flash("Google authorization cancelled or failed.")
        return redirect(url_for("settings"))

    saved_state = session.get("google_oauth_state")
    returned_state = request.args.get("state")

    if not saved_state or saved_state != returned_state:
        return "Invalid OAuth state.", 400

    try:
        flow = create_google_flow(saved_state)
        flow.fetch_token(authorization_response=request.url)

        if not save_google_credentials(flow.credentials):
            return "Could not save Google Drive credentials.", 500

        session.pop("google_oauth_state", None)
        flash("Google Drive connected successfully.")
        return redirect(url_for("google_drive"))

    except Exception as e:
        print("GOOGLE OAUTH CALLBACK ERROR:", e)
        flash(f"Google authorization error: {e}")
        return redirect(url_for("settings"))


@app.route("/google/disconnect", methods=["POST"])
def google_disconnect():
    c = None
    try:
        c = db()
        c.execute("DELETE FROM google_drive_tokens WHERE id=1")
        c.commit()
        flash("Google Drive disconnected.")
    except Exception as e:
        if c:
            c.rollback()
        flash(f"Disconnect failed: {e}")
    finally:
        if c:
            c.close()

    return redirect(url_for("settings"))


@app.route("/google/drive")
def google_drive():
    if not google_drive_configured():
        flash("Google Drive OAuth पहले configure करें.")
        return redirect(url_for("settings"))

    service = google_drive_service()
    if not service:
        return redirect(url_for("google_authorize"))

    try:
        result = service.files().list(
            q="trashed = false",
            pageSize=100,
            orderBy="modifiedTime desc",
            fields=(
                "files(id,name,mimeType,size,modifiedTime,webViewLink)"
            )
        ).execute()

        return render_template(
            "google_drive.html",
            files=result.get("files", [])
        )

    except HttpError as e:
        print("GOOGLE DRIVE LIST ERROR:", e)
        flash("Google Drive access failed. Reconnect करें.")
        return redirect(url_for("google_authorize"))
    except Exception as e:
        flash(f"Google Drive error: {e}")
        return redirect(url_for("settings"))


@app.route("/google/drive/select/<file_id>")
def google_drive_select(file_id):
    service = google_drive_service()
    if not service:
        return jsonify({"ok": False, "error": "Google Drive not connected"}), 401

    try:
        file = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size,modifiedTime,webViewLink"
        ).execute()
        return jsonify({"ok": True, "file": file})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


def download_drive_file(file_id):
    service = google_drive_service()
    if not service:
        return False, None, None, None, "Google Drive is not connected"

    try:
        meta = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size"
        ).execute()

        name = meta.get("name", "attachment")
        mime_type = meta.get("mimeType", "application/octet-stream")

        export_map = {
            "application/vnd.google-apps.document":
                ("application/pdf", ".pdf"),
            "application/vnd.google-apps.spreadsheet":
                ("application/pdf", ".pdf"),
            "application/vnd.google-apps.presentation":
                ("application/pdf", ".pdf")
        }

        if mime_type in export_map:
            export_mime, ext = export_map[mime_type]
            req = service.files().export_media(
                fileId=file_id,
                mimeType=export_mime
            )
            mime_type = export_mime
            if not name.lower().endswith(".pdf"):
                name += ext
        else:
            req = service.files().get_media(fileId=file_id)

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, req)
        done = False

        while not done:
            _, done = downloader.next_chunk()

        buffer.seek(0)
        return True, buffer.getvalue(), name, mime_type, None

    except Exception as e:
        print("GOOGLE DRIVE DOWNLOAD ERROR:", e)
        return False, None, None, None, str(e)


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():
    c = db()

    contacts = c.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()["n"]
    campaigns = c.execute("SELECT COUNT(*) AS n FROM campaigns").fetchone()["n"]

    sent = c.execute("""
        SELECT COUNT(*) AS n FROM whatsapp_messages
        WHERE status IN ('sent','delivered','read','accepted')
    """).fetchone()["n"]

    delivered = c.execute("""
        SELECT COUNT(*) AS n FROM whatsapp_messages WHERE status='delivered'
    """).fetchone()["n"]

    read = c.execute("""
        SELECT COUNT(*) AS n FROM whatsapp_messages WHERE status='read'
    """).fetchone()["n"]

    failed = c.execute("""
        SELECT COUNT(*) AS n FROM whatsapp_messages WHERE status='failed'
    """).fetchone()["n"]

    recent = c.execute("""
        SELECT * FROM campaigns ORDER BY id DESC LIMIT 10
    """).fetchall()

    c.close()

    return render_template(
        "dashboard.html",
        contacts=contacts,
        campaigns=campaigns,
        sent=sent,
        delivered=delivered,
        read=read,
        failed=failed,
        recent=recent
    )


# =========================================================
# CONTACTS
# =========================================================

@app.route("/contacts", methods=["GET", "POST"])
def contacts():
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("CSV file select करें.")
            return redirect(url_for("contacts"))

        text = f.read().decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        c = db()
        added = 0

        for row in reader:
            name = (row.get("name") or row.get("Name") or "").strip() or "Customer"
            phone = (
                row.get("phone") or row.get("Phone") or
                row.get("mobile") or row.get("Mobile") or ""
            ).strip()
            group = (
                row.get("group") or row.get("Group") or "General"
            ).strip() or "General"

            phone = clean_phone(phone)

            if phone:
                try:
                    c.execute("""
                        INSERT INTO contacts(name, phone, group_name)
                        VALUES(?, ?, ?)
                    """, (name, phone, group))
                    added += 1
                except IntegrityError:
                    c.rollback()

        c.commit()
        c.close()
        flash(f"{added} contacts imported.")
        return redirect(url_for("contacts"))

    c = db()
    rows = c.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall()
    c.close()

    return render_template("contacts.html", rows=rows)


# =========================================================
# CAMPAIGNS
# =========================================================

@app.route("/campaigns", methods=["GET", "POST"])
def campaigns():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        message = request.form.get("message", "").strip()
        group_name = request.form.get("group_name", "").strip()
        drive_file_id = request.form.get("drive_file_id", "").strip()
        drive_file_name = request.form.get("drive_file_name", "").strip()
        drive_mime_type = request.form.get("drive_mime_type", "").strip()

        if not name:
            flash("Campaign name जरूरी है.")
            return redirect(url_for("campaigns"))

        if not message and not drive_file_id:
            flash("Message या attachment में से कम से कम एक जरूरी है.")
            return redirect(url_for("campaigns"))

        c = db()
        c.execute("""
            INSERT INTO campaigns(
                name, message, group_name,
                drive_file_id, drive_file_name, drive_mime_type
            )
            VALUES(?, ?, ?, ?, ?, ?)
        """, (
            name, message, group_name,
            drive_file_id or None,
            drive_file_name or None,
            drive_mime_type or None
        ))
        c.commit()
        c.close()

        flash("Campaign saved as Draft.")
        return redirect(url_for("campaigns"))

    c = db()
    rows = c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()

    groups = [
        r["group_name"]
        for r in c.execute("""
            SELECT DISTINCT group_name FROM contacts
            WHERE group_name IS NOT NULL ORDER BY group_name
        """).fetchall()
    ]

    c.close()

    selected_drive_file = {
        "id": request.args.get("drive_file_id", "").strip(),
        "name": request.args.get("drive_file_name", "").strip(),
        "mimeType": request.args.get("drive_mime_type", "").strip()
    }

    return render_template(
        "campaigns.html",
        rows=rows,
        groups=groups,
        drive_connected=bool(google_drive_service()),
        selected_drive_file=selected_drive_file
    )


# =========================================================
# SEND CAMPAIGN
# =========================================================

@app.route("/campaign/<int:cid>/send", methods=["POST"])
def send_campaign(cid):
    c = db()

    try:
        campaign = c.execute(
            "SELECT * FROM campaigns WHERE id=?", (cid,)
        ).fetchone()

        if not campaign:
            flash("Campaign not found.")
            return redirect(url_for("campaigns"))

        if not whatsapp_configured():
            c.execute(
                "UPDATE campaigns SET status='API Not Configured' WHERE id=?",
                (cid,)
            )
            c.commit()
            flash("WHATSAPP_ACCESS_TOKEN और WHATSAPP_PHONE_NUMBER_ID configure करें.")
            return redirect(url_for("campaigns"))

        contacts_list = c.execute(
            "SELECT * FROM contacts" +
            (" WHERE group_name=?" if campaign["group_name"] else "") +
            " ORDER BY id",
            (campaign["group_name"],) if campaign["group_name"] else ()
        ).fetchall()

        if not contacts_list:
            flash("Campaign के लिए कोई contact नहीं मिला.")
            return redirect(url_for("campaigns"))

        attachment_bytes = None
        attachment_name = campaign["drive_file_name"]
        attachment_mime = campaign["drive_mime_type"]

        if campaign["drive_file_id"]:
            ok, attachment_bytes, attachment_name, attachment_mime, error = \
                download_drive_file(campaign["drive_file_id"])

            if not ok:
                flash(f"Attachment download failed: {error}")
                return redirect(url_for("campaigns"))

        sent = 0
        failed = 0

        for contact in contacts_list:
            body = (campaign["message"] or "").replace(
                "{{name}}", contact["name"]
            )
            phone = clean_phone(contact["phone"])

            if attachment_bytes:
                file_obj = io.BytesIO(attachment_bytes)
                ok, message_id, response = send_whatsapp_media(
                    phone,
                    file_obj,
                    attachment_name or "attachment",
                    attachment_mime or "application/octet-stream",
                    body
                )
            else:
                ok, message_id, response = send_whatsapp_text(phone, body)

            if ok:
                status = "accepted"
                error_text = None
                sent += 1
            else:
                status = "failed"
                error_text = (
                    json.dumps(response, ensure_ascii=False)
                    if isinstance(response, (dict, list))
                    else str(response)
                )
                failed += 1

            try:
                c.execute("""
                    INSERT INTO whatsapp_messages(
                        campaign_id, contact_id, phone, message,
                        wa_message_id, direction, status, error,
                        attachment_name, attachment_type
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cid, contact["id"], phone, body, message_id,
                    "outgoing", status, error_text,
                    attachment_name if attachment_bytes else None,
                    attachment_mime if attachment_bytes else None
                ))
                c.commit()

            except Exception as db_error:
                print("MESSAGE DATABASE ERROR:", db_error)
                c.rollback()
                if ok:
                    sent -= 1
                    failed += 1

        c.execute(
            "UPDATE campaigns SET status=? WHERE id=?",
            (f"Accepted {sent}, Failed {failed}", cid)
        )
        c.commit()

        flash(
            f"Campaign finished: {sent} accepted, {failed} failed."
            + (f" Attachment: {attachment_name}" if attachment_bytes else "")
        )
        return redirect(url_for("campaigns"))

    except Exception as e:
        print("CAMPAIGN SEND ERROR:", e)
        try:
            c.rollback()
        except Exception:
            pass
        flash(f"Campaign error: {e}")
        return redirect(url_for("campaigns"))

    finally:
        c.close()


# =========================================================
# WEBHOOK
# =========================================================

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = get_env("WEBHOOK_VERIFY_TOKEN") or "margdarshak_webhook_2026"

    if mode == "subscribe" and token == verify_token:
        return challenge, 200

    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook_receive():
    if not verify_meta_signature():
        return "Invalid signature", 403

    c = None

    try:
        data = request.get_json(silent=True)
        if not data:
            return "OK", 200

        c = db()
        c.execute("""
            INSERT INTO webhook_events(event_type, payload)
            VALUES(?, ?)
        """, ("whatsapp", json.dumps(data, ensure_ascii=False)))

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                for status in value.get("statuses", []):
                    wa_id = status.get("id")
                    new_status = status.get("status")
                    errors = status.get("errors")
                    error_text = (
                        json.dumps(errors, ensure_ascii=False)
                        if errors else None
                    )

                    if wa_id:
                        c.execute("""
                            UPDATE whatsapp_messages
                            SET status=?, error=?, updated_at=CURRENT_TIMESTAMP
                            WHERE wa_message_id=?
                        """, (new_status, error_text, wa_id))

                for msg in value.get("messages", []):
                    wa_message_id = msg.get("id")
                    sender = msg.get("from")
                    msg_type = msg.get("type")
                    message_text = ""

                    if msg_type == "text":
                        message_text = msg.get("text", {}).get("body", "")
                    elif msg_type == "button":
                        message_text = msg.get("button", {}).get("text", "")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive", {})
                        if interactive.get("type") == "button_reply":
                            message_text = interactive.get(
                                "button_reply", {}
                            ).get("title", "")
                        elif interactive.get("type") == "list_reply":
                            message_text = interactive.get(
                                "list_reply", {}
                            ).get("title", "")
                    elif msg_type == "image":
                        message_text = "[Image received]"
                    elif msg_type == "document":
                        message_text = "[Document received]"
                    elif msg_type == "video":
                        message_text = "[Video received]"
                    elif msg_type == "audio":
                        message_text = "[Audio received]"

                    if wa_message_id:
                        try:
                            c.execute("""
                                INSERT INTO whatsapp_incoming(
                                    wa_message_id, phone, message_type, message
                                )
                                VALUES(?, ?, ?, ?)
                            """, (
                                wa_message_id, sender, msg_type, message_text
                            ))
                        except IntegrityError:
                            c.rollback()

                    if sender:
                        phone = clean_phone(sender)
                        exists = c.execute(
                            "SELECT id FROM contacts WHERE phone=?", (phone,)
                        ).fetchone()

                        if not exists:
                            profile_name = "WhatsApp Customer"
                            contact_data = value.get("contacts", [])
                            if contact_data:
                                profile_name = (
                                    contact_data[0].get("profile", {}).get("name")
                                    or profile_name
                                )

                            try:
                                c.execute("""
                                    INSERT INTO contacts(name, phone, group_name)
                                    VALUES(?, ?, ?)
                                """, (profile_name, phone, "WhatsApp"))
                            except IntegrityError:
                                c.rollback()

        c.commit()
        return "EVENT_RECEIVED", 200

    except Exception as e:
        print("WEBHOOK ERROR:", e)
        if c:
            try:
                c.rollback()
            except Exception:
                pass
        return "EVENT_RECEIVED", 200

    finally:
        if c:
            c.close()


# =========================================================
# OTHER ROUTES
# =========================================================

@app.route("/messages-test")
def messages_test():
    return {"status": "ok", "message": "MESSAGES ROUTING WORKING"}


@app.route("/messages")
def messages():
    c = db()
    rows = c.execute("""
        SELECT wm.*, c.name AS contact_name
        FROM whatsapp_messages wm
        LEFT JOIN contacts c ON c.id=wm.contact_id
        ORDER BY wm.id DESC LIMIT 500
    """).fetchall()
    c.close()
    return render_template("messages.html", rows=rows)


@app.route("/incoming")
def incoming():
    c = db()
    rows = c.execute("""
        SELECT * FROM whatsapp_incoming
        ORDER BY id DESC LIMIT 500
    """).fetchall()
    c.close()
    return render_template("incoming.html", rows=rows)


@app.route("/webhook-test")
def webhook_test():
    return {"status": "ok", "message": "WEBHOOK ROUTING WORKING"}


@app.route("/webhook/logs")
def webhook_logs():
    c = db()
    rows = c.execute("""
        SELECT * FROM webhook_events
        ORDER BY id DESC LIMIT 100
    """).fetchall()
    c.close()
    return render_template("webhook_logs.html", rows=rows)


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        config_ok=whatsapp_configured(),
        webhook_token_ok=bool(get_env("WEBHOOK_VERIFY_TOKEN")),
        app_secret_ok=bool(get_env("META_APP_SECRET")),
        google_drive_configured=google_drive_configured(),
        google_drive_connected=bool(google_drive_service())
    )


@app.route("/api/whatsapp/status")
def whatsapp_status():
    return jsonify({
        "configured": whatsapp_configured(),
        "phone_number_id": bool(get_env("WHATSAPP_PHONE_NUMBER_ID")),
        "access_token": bool(get_env("WHATSAPP_ACCESS_TOKEN")),
        "app_secret": bool(get_env("META_APP_SECRET")),
        "webhook_verify_token": bool(get_env("WEBHOOK_VERIFY_TOKEN")),
        "database_configured": bool(get_env("DATABASE_URL")),
        "google_drive_configured": google_drive_configured(),
        "google_drive_connected": bool(google_drive_service())
    })


@app.route("/health")
def health():
    database_ok = False
    database_error = None

    try:
        c = db()
        c.execute("SELECT 1").fetchone()
        c.close()
        database_ok = True
    except Exception as e:
        database_error = str(e)

    return jsonify({
        "status": "ok",
        "database_configured": bool(get_env("DATABASE_URL")),
        "database_connected": database_ok,
        "whatsapp_configured": whatsapp_configured(),
        "google_drive_configured": google_drive_configured(),
        "google_drive_connected": bool(google_drive_service()),
        "time": datetime.utcnow().isoformat(),
        "database_error": database_error
    })


@app.route("/debug/routes")
def debug_routes():
    return jsonify([
        {
            "rule": str(rule),
            "endpoint": rule.endpoint,
            "methods": sorted(rule.methods)
        }
        for rule in app.url_map.iter_rules()
    ])


@app.route("/debug/test")
def debug_test():
    return {"status": "ok", "message": "DEBUG TEST ROUTE WORKING"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
