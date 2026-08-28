from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import IntegrityError
import csv
import io
import os
import requests
import hmac
import hashlib
import json
import secrets
from datetime import datetime

# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

# =========================================================
# ENVIRONMENT HELPER
# =========================================================

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
        cursor = self.connection.cursor(cursor_factory=RealDictCursor)
        if params is None:
            cursor.execute(query)
        else:
            cursor.execute(query, params)
        return cursor

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

    connection = psycopg2.connect(
        database_url,
        connect_timeout=10
    )
    return DBWrapper(connection)


# =========================================================
# DATABASE INITIALIZATION
# =========================================================

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

        c.commit()
        print("DATABASE INITIALIZED SUCCESSFULLY")
        return True

    except Exception as e:
        print("DATABASE INITIALIZATION ERROR:", str(e))
        if c:
            try:
                c.rollback()
            except Exception:
                pass
        return False

    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass


init_db()

# =========================================================
# WHATSAPP CONFIGURATION
# =========================================================

def whatsapp_configured():
    return bool(
        get_env("WHATSAPP_ACCESS_TOKEN")
        and get_env("WHATSAPP_PHONE_NUMBER_ID")
    )


# =========================================================
# GOOGLE DRIVE OAUTH CONFIGURATION
# =========================================================

GOOGLE_CLIENT_ID = get_env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = get_env("GOOGLE_CLIENT_SECRET")

GOOGLE_REDIRECT_URI = (
    get_env("GOOGLE_REDIRECT_URI")
    or "https://deepaks-crm-1.onrender.com/google/oauth/callback"
)

GOOGLE_OAUTH_SCOPE = (
    "https://www.googleapis.com/auth/drive.readonly"
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"


def google_configured():
    return bool(
        GOOGLE_CLIENT_ID and
        GOOGLE_CLIENT_SECRET and
        GOOGLE_REDIRECT_URI
    )


def google_token():
    return session.get("google_access_token")


# =========================================================
# PHONE CLEANING
# =========================================================

def clean_phone(phone):
    if not phone:
        return ""

    phone = str(phone).strip()
    phone = phone.replace("+", "")
    phone = phone.replace(" ", "")
    phone = phone.replace("-", "")
    phone = phone.replace("(", "")
    phone = phone.replace(")", "")

    if phone.startswith("00"):
        phone = phone[2:]

    return phone


# =========================================================
# META SIGNATURE
# =========================================================

def verify_meta_signature():
    app_secret = get_env("META_APP_SECRET")

    if not app_secret:
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")

    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"),
        request.get_data(),
        hashlib.sha256
    ).hexdigest()

    received = signature.replace("sha256=", "", 1)

    return hmac.compare_digest(expected, received)


# =========================================================
# WHATSAPP API
# =========================================================

def whatsapp_messages_url():
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")

    if not phone_id:
        return None

    return f"https://graph.facebook.com/v23.0/{phone_id}/messages"


def send_whatsapp_text(phone, body):
    token = get_env("WHATSAPP_ACCESS_TOKEN")
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")

    if not token or not phone_id:
        return False, None, "WhatsApp API credentials missing"

    phone = clean_phone(phone)

    if not phone:
        return False, None, "Invalid phone number"

    url = whatsapp_messages_url()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": body
        }
    }

    try:
        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        try:
            data = r.json()
        except Exception:
            data = r.text

        if r.ok:
            message_id = None
            if isinstance(data, dict):
                messages = data.get("messages", [])
                if messages:
                    message_id = messages[0].get("id")

            return True, message_id, data

        return False, None, data

    except Exception as e:
        return False, None, str(e)


def send_whatsapp_template(
    phone,
    template_name,
    language_code="en_US",
    parameters=None
):
    token = get_env("WHATSAPP_ACCESS_TOKEN")
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")

    if not token or not phone_id:
        return False, None, "WhatsApp API credentials missing"

    phone = clean_phone(phone)

    if not phone:
        return False, None, "Invalid phone number"

    url = whatsapp_messages_url()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    template = {
        "name": template_name,
        "language": {
            "code": language_code
        }
    }

    if parameters:
        template["components"] = [{
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    "text": str(value)
                }
                for value in parameters
            ]
        }]

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": template
    }

    try:
        r = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        try:
            data = r.json()
        except Exception:
            data = r.text

        if r.ok:
            message_id = None
            if isinstance(data, dict):
                messages = data.get("messages", [])
                if messages:
                    message_id = messages[0].get("id")

            return True, message_id, data

        return False, None, data

    except Exception as e:
        return False, None, str(e)


# =========================================================
# GOOGLE DRIVE HELPERS
# =========================================================

def google_headers():
    token = google_token()
    if not token:
        return None

    return {
        "Authorization": f"Bearer {token}"
    }


def drive_request(url, params=None):
    headers = google_headers()

    if not headers:
        return None, "Google Drive is not connected."

    try:
        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        try:
            data = r.json()
        except Exception:
            data = r.text

        if r.ok:
            return data, None

        # Access token may have expired.
        if r.status_code == 401:
            session.pop("google_access_token", None)
            session.pop("google_refresh_token", None)
            return None, "Google Drive session expired. Connect Google Drive again."

        return None, data

    except Exception as e:
        return None, str(e)


def drive_files_list():
    # CSV files + Google Sheets
    params = {
        "pageSize": 100,
        "orderBy": "modifiedTime desc",
        "fields": "files(id,name,mimeType,size,modifiedTime,webViewLink)",
        "q": (
            "trashed = false and "
            "("
            "mimeType = 'text/csv' or "
            "mimeType = 'application/vnd.google-apps.spreadsheet'"
            ")"
        )
    }

    return drive_request(
        f"{GOOGLE_DRIVE_API}/files",
        params=params
    )


def download_drive_file(file_id, mime_type):
    headers = google_headers()

    if not headers:
        return None, "Google Drive is not connected."

    try:
        if mime_type == "application/vnd.google-apps.spreadsheet":
            url = f"{GOOGLE_DRIVE_API}/files/{file_id}/export"
            params = {"mimeType": "text/csv"}
        else:
            url = f"{GOOGLE_DRIVE_API}/files/{file_id}"
            params = {"alt": "media"}

        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=60
        )

        if r.status_code == 401:
            session.pop("google_access_token", None)
            return None, "Google Drive session expired. Connect Google Drive again."

        if not r.ok:
            try:
                return None, r.json()
            except Exception:
                return None, r.text

        return r.content, None

    except Exception as e:
        return None, str(e)


def import_csv_text(text):
    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return 0, "CSV header not found."

    c = db()
    added = 0
    skipped = 0

    try:
        for row in reader:
            name = (
                row.get("name")
                or row.get("Name")
                or row.get("full_name")
                or row.get("Full Name")
                or ""
            ).strip()

            name = name or "Customer"

            phone = (
                row.get("phone")
                or row.get("Phone")
                or row.get("mobile")
                or row.get("Mobile")
                or row.get("phone_number")
                or row.get("Phone Number")
                or ""
            ).strip()

            phone = clean_phone(phone)

            group = (
                row.get("group")
                or row.get("Group")
                or row.get("group_name")
                or row.get("Group Name")
                or "General"
            ).strip()

            group = group or "General"

            if not phone:
                skipped += 1
                continue

            try:
                c.execute("""
                    INSERT INTO contacts
                    (name, phone, group_name)
                    VALUES (?, ?, ?)
                    ON CONFLICT (phone) DO NOTHING
                """, (name, phone, group))

                if c.connection.info.transaction_status:
                    pass

                added += 1

            except Exception:
                c.rollback()
                skipped += 1

        c.commit()

    except Exception:
        c.rollback()
        raise

    finally:
        c.close()

    return added, f"{skipped} rows skipped."


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():
    c = db()

    contacts = c.execute(
        "SELECT COUNT(*) AS n FROM contacts"
    ).fetchone()["n"]

    campaigns = c.execute(
        "SELECT COUNT(*) AS n FROM campaigns"
    ).fetchone()["n"]

    sent = c.execute("""
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status IN ('sent','delivered','read','accepted')
    """).fetchone()["n"]

    delivered = c.execute("""
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status='delivered'
    """).fetchone()["n"]

    read = c.execute("""
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status='read'
    """).fetchone()["n"]

    failed = c.execute("""
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status='failed'
    """).fetchone()["n"]

    recent = c.execute("""
        SELECT *
        FROM campaigns
        ORDER BY id DESC
        LIMIT 10
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

        text = f.read().decode(
            "utf-8-sig",
            errors="ignore"
        )

        added, extra = import_csv_text(text)

        flash(f"{added} contacts imported. {extra}")

        return redirect(url_for("contacts"))

    c = db()

    rows = c.execute("""
        SELECT *
        FROM contacts
        ORDER BY id DESC
    """).fetchall()

    c.close()

    return render_template(
        "contacts.html",
        rows=rows
    )


# =========================================================
# GOOGLE DRIVE
# =========================================================

@app.route("/google/drive")
def google_drive():
    if not google_configured():
        return render_template(
            "google_drive.html",
            connected=False,
            files=[],
            error="Google OAuth environment variables are not configured."
        )

    if not google_token():
        return render_template(
            "google_drive.html",
            connected=False,
            files=[],
            error=None
        )

    data, error = drive_files_list()

    if error:
        return render_template(
            "google_drive.html",
            connected=False if "session expired" in str(error).lower() else True,
            files=[],
            error=error
        )

    return render_template(
        "google_drive.html",
        connected=True,
        files=data.get("files", []) if isinstance(data, dict) else [],
        error=None
    )


@app.route("/google/login")
def google_login():
    if not google_configured():
        flash(
            "Google OAuth configure नहीं है. "
            "GOOGLE_CLIENT_ID और GOOGLE_CLIENT_SECRET Render में add करें."
        )
        return redirect(url_for("google_drive"))

    state = secrets.token_urlsafe(32)
    session["google_oauth_state"] = state

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state
    }

    from urllib.parse import urlencode

    return redirect(
        GOOGLE_AUTH_URL + "?" + urlencode(params)
    )


@app.route("/google/oauth/callback")
def google_oauth_callback():
    error = request.args.get("error")

    if error:
        flash(f"Google authorization cancelled/error: {error}")
        return redirect(url_for("google_drive"))

    state = request.args.get("state")
    saved_state = session.pop("google_oauth_state", None)

    if not state or not saved_state or state != saved_state:
        return "Invalid OAuth state.", 400

    code = request.args.get("code")

    if not code:
        return "Authorization code missing.", 400

    token_payload = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    try:
        r = requests.post(
            GOOGLE_TOKEN_URL,
            data=token_payload,
            timeout=30
        )

        try:
            data = r.json()
        except Exception:
            data = {}

        if not r.ok:
            return jsonify({
                "status": "error",
                "message": "Google token exchange failed",
                "details": data
            }), 400

        session["google_access_token"] = data.get("access_token")

        if data.get("refresh_token"):
            session["google_refresh_token"] = data.get("refresh_token")

        flash("Google Drive connected successfully.")
        return redirect(url_for("google_drive"))

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/google/logout")
def google_logout():
    session.pop("google_access_token", None)
    session.pop("google_refresh_token", None)
    flash("Google Drive disconnected.")
    return redirect(url_for("google_drive"))


@app.route("/google/drive/import/<file_id>", methods=["POST"])
def google_drive_import(file_id):
    if not google_token():
        flash("पहले Google Drive connect करें.")
        return redirect(url_for("google_drive"))

    mime_type = request.form.get("mime_type", "text/csv")

    content, error = download_drive_file(
        file_id,
        mime_type
    )

    if error:
        flash(f"Drive import error: {error}")
        return redirect(url_for("google_drive"))

    try:
        text = content.decode(
            "utf-8-sig",
            errors="ignore"
        )

        added, extra = import_csv_text(text)

        flash(
            f"Google Drive से {added} contacts import हुए. {extra}"
        )

    except Exception as e:
        flash(f"CSV import error: {str(e)}")

    return redirect(url_for("google_drive"))


# =========================================================
# CAMPAIGNS
# =========================================================

@app.route("/campaigns", methods=["GET", "POST"])
def campaigns():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        message = request.form.get("message", "").strip()
        group_name = request.form.get("group_name", "").strip()

        if not name or not message:
            flash("Campaign name और message जरूरी है.")
            return redirect(url_for("campaigns"))

        c = db()

        c.execute("""
            INSERT INTO campaigns
            (name, message, group_name)
            VALUES (?, ?, ?)
        """, (name, message, group_name))

        c.commit()
        c.close()

        flash("Campaign saved as Draft.")
        return redirect(url_for("campaigns"))

    c = db()

    rows = c.execute("""
        SELECT *
        FROM campaigns
        ORDER BY id DESC
    """).fetchall()

    groups = [
        r["group_name"]
        for r in c.execute("""
            SELECT DISTINCT group_name
            FROM contacts
            WHERE group_name IS NOT NULL
            ORDER BY group_name
        """).fetchall()
    ]

    c.close()

    return render_template(
        "campaigns.html",
        rows=rows,
        groups=groups
    )


# =========================================================
# SEND CAMPAIGN
# =========================================================

@app.route("/campaign/<int:cid>/send", methods=["POST"])
def send_campaign(cid):
    c = db()

    try:
        campaign = c.execute("""
            SELECT *
            FROM campaigns
            WHERE id=?
        """, (cid,)).fetchone()

        if not campaign:
            flash("Campaign not found.")
            return redirect(url_for("campaigns"))

        if not whatsapp_configured():
            c.execute("""
                UPDATE campaigns
                SET status='API Not Configured'
                WHERE id=?
            """, (cid,))

            c.commit()

            flash(
                "WHATSAPP_ACCESS_TOKEN और "
                "WHATSAPP_PHONE_NUMBER_ID configure करें."
            )

            return redirect(url_for("campaigns"))

        q = "SELECT * FROM contacts"
        params = ()

        if campaign["group_name"]:
            q += " WHERE group_name=?"
            params = (campaign["group_name"],)

        contacts_list = c.execute(q, params).fetchall()

        sent = 0
        failed = 0

        for contact in contacts_list:
            body = campaign["message"].replace(
                "{{name}}",
                contact["name"]
            )

            phone = clean_phone(contact["phone"])

            ok, message_id, response = send_whatsapp_text(
                phone,
                body
            )

            if ok:
                status = "accepted"
                sent += 1
                error_text = None
            else:
                status = "failed"
                failed += 1

                if isinstance(response, (dict, list)):
                    error_text = json.dumps(
                        response,
                        ensure_ascii=False
                    )
                else:
                    error_text = str(response)

            try:
                c.execute("""
                    INSERT INTO whatsapp_messages
                    (
                        campaign_id,
                        contact_id,
                        phone,
                        message,
                        wa_message_id,
                        direction,
                        status,
                        error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cid,
                    contact["id"],
                    phone,
                    body,
                    message_id,
                    "outgoing",
                    status,
                    error_text
                ))

                c.commit()

            except Exception as db_error:
                print(
                    "MESSAGE DATABASE ERROR:",
                    str(db_error)
                )

                c.rollback()

                if ok:
                    sent -= 1
                    failed += 1

        c.execute("""
            UPDATE campaigns
            SET status=?
            WHERE id=?
        """, (
            f"Accepted {sent}, Failed {failed}",
            cid
        ))

        c.commit()

        flash(
            f"Campaign finished: {sent} accepted, "
            f"{failed} failed."
        )

        return redirect(url_for("campaigns"))

    except Exception as e:
        print("CAMPAIGN SEND ERROR:", str(e))

        try:
            c.rollback()
        except Exception:
            pass

        flash(f"Campaign error: {str(e)}")

        return redirect(url_for("campaigns"))

    finally:
        try:
            c.close()
        except Exception:
            pass


# =========================================================
# WEBHOOK VERIFY
# =========================================================

WEBHOOK_VERIFY_TOKEN = (
    get_env("WEBHOOK_VERIFY_TOKEN")
    or "margdarshak_webhook_2026"
)


@app.route("/webhook", methods=["GET"])
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        return challenge, 200

    return "Forbidden", 403


# =========================================================
# WEBHOOK RECEIVE
# =========================================================

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
            INSERT INTO webhook_events
            (event_type, payload)
            VALUES (?, ?)
        """, (
            "whatsapp",
            json.dumps(data, ensure_ascii=False)
        ))

        entries = data.get("entry", [])

        for entry in entries:
            changes = entry.get("changes", [])

            for change in changes:
                value = change.get("value", {})

                statuses = value.get("statuses", [])

                for status in statuses:
                    wa_id = status.get("id")
                    new_status = status.get("status")
                    errors = status.get("errors")

                    error_text = None

                    if errors:
                        error_text = json.dumps(
                            errors,
                            ensure_ascii=False
                        )

                    if wa_id:
                        c.execute("""
                            UPDATE whatsapp_messages
                            SET
                                status=?,
                                error=?,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE wa_message_id=?
                        """, (
                            new_status,
                            error_text,
                            wa_id
                        ))

                messages_data = value.get("messages", [])

                for msg in messages_data:
                    wa_message_id = msg.get("id")
                    sender = msg.get("from")
                    msg_type = msg.get("type")
                    message_text = ""

                    if msg_type == "text":
                        message_text = (
                            msg.get("text", {})
                            .get("body", "")
                        )

                    elif msg_type == "button":
                        message_text = (
                            msg.get("button", {})
                            .get("text", "")
                        )

                    elif msg_type == "interactive":
                        interactive = msg.get(
                            "interactive",
                            {}
                        )

                        if interactive.get("type") == "button_reply":
                            message_text = (
                                interactive
                                .get("button_reply", {})
                                .get("title", "")
                            )

                        elif interactive.get("type") == "list_reply":
                            message_text = (
                                interactive
                                .get("list_reply", {})
                                .get("title", "")
                            )

                    if wa_message_id:
                        try:
                            c.execute("""
                                INSERT INTO whatsapp_incoming
                                (
                                    wa_message_id,
                                    phone,
                                    message_type,
                                    message
                                )
                                VALUES (?, ?, ?, ?)
                            """, (
                                wa_message_id,
                                sender,
                                msg_type,
                                message_text
                            ))

                        except IntegrityError:
                            c.rollback()

                    if sender:
                        cleaned_sender = clean_phone(sender)

                        existing = c.execute("""
                            SELECT id
                            FROM contacts
                            WHERE phone=?
                        """, (cleaned_sender,)).fetchone()

                        if not existing:
                            profile_name = "WhatsApp Customer"

                            contacts_data = value.get(
                                "contacts",
                                []
                            )

                            if contacts_data:
                                profile = (
                                    contacts_data[0]
                                    .get("profile", {})
                                )

                                profile_name = (
                                    profile.get("name")
                                    or "WhatsApp Customer"
                                )

                            try:
                                c.execute("""
                                    INSERT INTO contacts
                                    (
                                        name,
                                        phone,
                                        group_name
                                    )
                                    VALUES (?, ?, ?)
                                """, (
                                    profile_name,
                                    cleaned_sender,
                                    "WhatsApp"
                                ))

                            except IntegrityError:
                                c.rollback()

        c.commit()

        return "EVENT_RECEIVED", 200

    except Exception as e:
        print("WEBHOOK ERROR:", str(e))

        if c:
            try:
                c.rollback()
            except Exception:
                pass

        return "EVENT_RECEIVED", 200

    finally:
        if c:
            try:
                c.close()
            except Exception:
                pass


# =========================================================
# MESSAGE ROUTES
# =========================================================

@app.route("/messages-test")
def messages_test():
    return {
        "status": "ok",
        "message": "MESSAGES ROUTING WORKING"
    }


@app.route("/messages")
def messages():
    c = db()

    rows = c.execute("""
        SELECT
            wm.*,
            c.name AS contact_name
        FROM whatsapp_messages wm
        LEFT JOIN contacts c
            ON c.id = wm.contact_id
        ORDER BY wm.id DESC
        LIMIT 500
    """).fetchall()

    c.close()

    return render_template(
        "messages.html",
        rows=rows
    )


@app.route("/incoming")
def incoming():
    c = db()

    rows = c.execute("""
        SELECT *
        FROM whatsapp_incoming
        ORDER BY id DESC
        LIMIT 500
    """).fetchall()

    c.close()

    return render_template(
        "incoming.html",
        rows=rows
    )


@app.route("/webhook-test")
def webhook_test():
    return {
        "status": "ok",
        "message": "WEBHOOK ROUTING WORKING"
    }


@app.route("/webhook/logs")
def webhook_logs():
    c = db()

    rows = c.execute("""
        SELECT *
        FROM webhook_events
        ORDER BY id DESC
        LIMIT 100
    """).fetchall()

    c.close()

    return render_template(
        "webhook_logs.html",
        rows=rows
    )


# =========================================================
# PRIVACY
# =========================================================

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


# =========================================================
# SETTINGS
# =========================================================

@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        config_ok=whatsapp_configured(),
        webhook_token_ok=bool(
            get_env("WEBHOOK_VERIFY_TOKEN")
        ),
        app_secret_ok=bool(
            get_env("META_APP_SECRET")
        ),
        google_configured=google_configured(),
        google_connected=bool(google_token()),
        google_redirect_uri=GOOGLE_REDIRECT_URI
    )


# =========================================================
# WHATSAPP STATUS API
# =========================================================

@app.route("/api/whatsapp/status")
def whatsapp_status():
    return jsonify({
        "configured": whatsapp_configured(),
        "phone_number_id": bool(
            get_env("WHATSAPP_PHONE_NUMBER_ID")
        ),
        "access_token": bool(
            get_env("WHATSAPP_ACCESS_TOKEN")
        ),
        "app_secret": bool(
            get_env("META_APP_SECRET")
        ),
        "webhook_verify_token": bool(
            get_env("WEBHOOK_VERIFY_TOKEN")
        ),
        "database_configured": bool(
            get_env("DATABASE_URL")
        ),
        "google_configured": google_configured(),
        "google_connected": bool(google_token()),
        "google_redirect_uri": GOOGLE_REDIRECT_URI
    })


# =========================================================
# HEALTH CHECK
# =========================================================

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
        print("DATABASE HEALTH ERROR:", database_error)

    return jsonify({
        "status": "ok",
        "database_configured": bool(
            get_env("DATABASE_URL")
        ),
        "database_connected": database_ok,
        "whatsapp_configured": whatsapp_configured(),
        "google_configured": google_configured(),
        "google_connected": bool(google_token()),
        "time": datetime.utcnow().isoformat(),
        "database_error": database_error
    })


# =========================================================
# DEBUG
# =========================================================

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
    return {
        "status": "ok",
        "message": "DEBUG TEST ROUTE WORKING"
    }


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
