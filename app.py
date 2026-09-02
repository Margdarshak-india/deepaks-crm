from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    session
)

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
import mimetypes
import secrets

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import threading
import time
from urllib.parse import urlencode
from werkzeug.utils import secure_filename


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "change-this-secret-key"
)


# =========================================================
# ENVIRONMENT HELPER
# =========================================================

def get_env(name):
    return os.getenv(name, "").strip()

def format_ist(dt):
    if not dt:
        return ""
    try:
        ist = ZoneInfo("Asia/Kolkata")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ist).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return str(dt)

app.jinja_env.filters["ist_datetime"] = format_ist


# =========================================================
# DATABASE
# =========================================================

class DBWrapper:

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):

        query = query.replace("?", "%s")

        cursor = self.connection.cursor(
            cursor_factory=RealDictCursor
        )

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
        raise RuntimeError(
            "DATABASE_URL is not configured in Render Environment."
        )

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

        # -------------------------------------------------
        # CONTACTS
        # -------------------------------------------------

        c.execute("""
            CREATE TABLE IF NOT EXISTS contacts(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                group_name TEXT DEFAULT 'General'
            )
        """)

        # -------------------------------------------------
        # CAMPAIGNS
        # -------------------------------------------------

        c.execute("""
            CREATE TABLE IF NOT EXISTS campaigns(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                message TEXT NOT NULL,
                group_name TEXT,
                status TEXT DEFAULT 'Draft',
                drive_file_id TEXT,
                drive_file_name TEXT,
                drive_mime_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Existing installations: add attachment columns safely.
        c.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS drive_file_id TEXT")
        c.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS drive_file_name TEXT")
        c.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS drive_mime_type TEXT")

        # -------------------------------------------------
        # WHATSAPP MESSAGES
        # -------------------------------------------------

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

        # -------------------------------------------------
        # INCOMING MESSAGES
        # -------------------------------------------------

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

        # -------------------------------------------------
        # WEBHOOK EVENTS
        # -------------------------------------------------

        c.execute("""
            CREATE TABLE IF NOT EXISTS webhook_events(
                id SERIAL PRIMARY KEY,
                event_type TEXT,
                payload TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # -------------------------------------------------
        # TEMPLATE CAMPAIGNS
        # -------------------------------------------------

        c.execute("""
            CREATE TABLE IF NOT EXISTS template_campaigns(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                template_name TEXT NOT NULL,
                template_language TEXT NOT NULL DEFAULT 'en_US',
                target_type TEXT NOT NULL DEFAULT 'group',
                group_name TEXT,
                manual_number TEXT,
                parameters TEXT,
                status TEXT DEFAULT 'Pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Existing installations: support IMAGE header templates.
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS header_image_path TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS header_media_id TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS header_media_path TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS header_media_type TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS selected_contact_ids TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS manual_numbers TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS typed_numbers TEXT")
        c.execute("ALTER TABLE template_campaigns ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ")

        c.commit()

        print("DATABASE INITIALIZED SUCCESSFULLY")

        return True

    except Exception as e:

        print(
            "DATABASE INITIALIZATION ERROR:",
            str(e)
        )

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
        and
        get_env("WHATSAPP_PHONE_NUMBER_ID")
    )


# =========================================================
# GOOGLE DRIVE OAUTH CONFIGURATION
# =========================================================

GOOGLE_CLIENT_ID = get_env("GOOGLE_CLIENT_ID")

GOOGLE_CLIENT_SECRET = get_env(
    "GOOGLE_CLIENT_SECRET"
)

GOOGLE_REDIRECT_URI = (
    get_env("GOOGLE_REDIRECT_URI")
    or
    "https://deepaks-crm-1.onrender.com/google/oauth/callback"
)

GOOGLE_OAUTH_SCOPE = (
    "https://www.googleapis.com/auth/drive.readonly"
)

GOOGLE_AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
)

GOOGLE_TOKEN_URL = (
    "https://oauth2.googleapis.com/token"
)

GOOGLE_DRIVE_API = (
    "https://www.googleapis.com/drive/v3"
)


def google_configured():

    return bool(
        GOOGLE_CLIENT_ID
        and
        GOOGLE_CLIENT_SECRET
        and
        GOOGLE_REDIRECT_URI
    )


def google_token():

    return session.get(
        "google_access_token"
    )


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

    app_secret = get_env(
        "META_APP_SECRET"
    )

    if not app_secret:
        return True

    signature = request.headers.get(
        "X-Hub-Signature-256",
        ""
    )

    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"),
        request.get_data(),
        hashlib.sha256
    ).hexdigest()

    received = signature.replace(
        "sha256=",
        "",
        1
    )

    return hmac.compare_digest(
        expected,
        received
    )


# =========================================================
# WHATSAPP API
# =========================================================

def whatsapp_messages_url():

    phone_id = get_env(
        "WHATSAPP_PHONE_NUMBER_ID"
    )

    if not phone_id:
        return None

    return (
        f"https://graph.facebook.com/v23.0/"
        f"{phone_id}/messages"
    )

# =========================================================
# WHATSAPP 24-HOUR WINDOW
# =========================================================

def get_last_incoming_message(phone):
    phone = clean_phone(phone)

    if not phone:
        return None

    c = db()

    try:
        row = c.execute("""
            SELECT created_at
            FROM whatsapp_incoming
            WHERE phone = ?
            ORDER BY created_at DESC
            LIMIT 1
        """, (phone,)).fetchone()

        return row["created_at"] if row else None

    finally:
        c.close()


def within_whatsapp_24_hours(phone):
    last_message = get_last_incoming_message(phone)

    if not last_message:
        return False

    now = datetime.utcnow()

    # PostgreSQL timestamp may be naive UTC
    if hasattr(last_message, "tzinfo") and last_message.tzinfo:
        last_message = last_message.replace(tzinfo=None)

    hours = (now - last_message).total_seconds() / 3600

    return hours <= 24

# =========================================================
# SEND MESSAGE WITH 24-HOUR POLICY
# =========================================================

def send_whatsapp_with_policy(phone, body, contact_name="Customer"):

    phone = clean_phone(phone)

    if not phone:
        return False, None, "Invalid phone number"

    # -----------------------------------------------------
    # CUSTOMER MESSAGED US WITHIN LAST 24 HOURS
    # -----------------------------------------------------

    if within_whatsapp_24_hours(phone):

        return send_whatsapp_text(
            phone,
            body
        )

    # -----------------------------------------------------
    # OUTSIDE 24-HOUR WINDOW
    # USE APPROVED TEMPLATE
    # -----------------------------------------------------

    template_name = get_env(
        "WHATSAPP_FALLBACK_TEMPLATE_NAME"
    )

    template_language = get_env(
        "WHATSAPP_FALLBACK_TEMPLATE_LANGUAGE"
    ) or "en_US"

    if not template_name:

        return (
            False,
            None,
            "24-hour window expired. "
            "WHATSAPP_FALLBACK_TEMPLATE_NAME is not configured."
        )

    return send_whatsapp_template(
        phone,
        template_name,
        template_language,
        [contact_name]
    )

# =========================================================
# SEND WHATSAPP TEXT
# =========================================================

def send_whatsapp_text(phone, body):

    token = get_env(
        "WHATSAPP_ACCESS_TOKEN"
    )

    phone_id = get_env(
        "WHATSAPP_PHONE_NUMBER_ID"
    )

    if not token or not phone_id:

        return (
            False,
            None,
            "WhatsApp API credentials missing"
        )

    phone = clean_phone(phone)

    if not phone:

        return (
            False,
            None,
            "Invalid phone number"
        )

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

                messages = data.get(
                    "messages",
                    []
                )

                if messages:

                    message_id = messages[0].get(
                        "id"
                    )

            return (
                True,
                message_id,
                data
            )

        return (
            False,
            None,
            data
        )

    except Exception as e:

        return (
            False,
            None,
            str(e)
        )


# =========================================================
# SEND WHATSAPP TEMPLATE
# =========================================================

def send_whatsapp_template(
    phone,
    template_name,
    language_code="en_US",
    parameters=None,
    header_media_id=None,
    header_media_type=None
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
        "language": {"code": language_code}
    }

    components = []

    if header_media_id and header_media_type in ("image", "video", "document"):
        media_payload = {"id": str(header_media_id)}
        components.append({
            "type": "header",
            "parameters": [
                {
                    "type": header_media_type,
                    header_media_type: media_payload
                }
            ]
        })

    if parameters:
        components.append({
            "type": "body",
            "parameters": [
                {
                    "type": "text",
                    "text": str(value)
                }
                for value in parameters
            ]
        })

    if components:
        template["components"] = components

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
                messages = data.get("messages") or []
                if messages:
                    message_id = messages[0].get("id")
            return True, message_id, data

        return False, None, data

    except Exception as e:
        return False, None, str(e)




def google_headers():

    token = google_token()

    if not token:
        return None

    return {
        "Authorization": f"Bearer {token}"
    }


def drive_request(
    url,
    params=None
):

    headers = google_headers()

    if not headers:

        return (
            None,
            "Google Drive is not connected."
        )

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

            return (
                data,
                None
            )

        if r.status_code == 401:

            session.pop(
                "google_access_token",
                None
            )

            session.pop(
                "google_refresh_token",
                None
            )

            return (
                None,
                "Google Drive session expired. "
                "Connect Google Drive again."
            )

        return (
            None,
            data
        )

    except Exception as e:

        return (
            None,
            str(e)
        )


def drive_files_list():

    params = {

        "pageSize": 100,

        "orderBy": "modifiedTime desc",

        "fields": (
            "files("
            "id,"
            "name,"
            "mimeType,"
            "size,"
            "modifiedTime,"
            "webViewLink"
            ")"
        ),

        # Return all non-trashed files (not folders).
        # CSV/Google Sheets can be imported as contacts; other
        # supported files can be selected as campaign attachments.
        "q": (
            "trashed = false and "
            "mimeType != 'application/vnd.google-apps.folder'"
        )
    }

    return drive_request(
        f"{GOOGLE_DRIVE_API}/files",
        params=params
    )


def download_drive_file(
    file_id,
    mime_type
):

    headers = google_headers()

    if not headers:

        return (
            None,
            "Google Drive is not connected."
        )

    try:

        if (
            mime_type
            ==
            "application/vnd.google-apps.spreadsheet"
        ):

            url = (
                f"{GOOGLE_DRIVE_API}/"
                f"files/{file_id}/export"
            )

            params = {
                "mimeType": "text/csv"
            }

        else:

            url = (
                f"{GOOGLE_DRIVE_API}/"
                f"files/{file_id}"
            )

            params = {
                "alt": "media"
            }

        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=60
        )

        if r.status_code == 401:

            session.pop(
                "google_access_token",
                None
            )

            return (
                None,
                "Google Drive session expired. "
                "Connect Google Drive again."
            )

        if not r.ok:

            try:
                return (
                    None,
                    r.json()
                )
            except Exception:

                return (
                    None,
                    r.text
                )

        return (
            r.content,
            None
        )

    except Exception as e:

        return (
            None,
            str(e)
        )


# =========================================================
# IMPORT CSV
# =========================================================

def import_csv_text(text):

    reader = csv.DictReader(
        io.StringIO(text)
    )

    if not reader.fieldnames:

        return (
            0,
            "CSV header not found."
        )

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

                c.execute(
                    """
                    INSERT INTO contacts
                    (name, phone, group_name)
                    VALUES (?, ?, ?)
                    ON CONFLICT (phone) DO NOTHING
                    """,
                    (
                        name,
                        phone,
                        group
                    )
                )

                c.commit()

                added += 1

            except Exception:

                c.rollback()

                skipped += 1

        return (
            added,
            f"{skipped} rows skipped."
        )

    except Exception:

        c.rollback()

        raise

    finally:

        c.close()


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():

    c = db()

    contacts = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM contacts
        """
    ).fetchone()["n"]

    campaigns = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM campaigns
        """
    ).fetchone()["n"]

    sent = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status IN
        ('sent','delivered','read','accepted')
        """
    ).fetchone()["n"]

    delivered = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status='delivered'
        """
    ).fetchone()["n"]

    read = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status='read'
        """
    ).fetchone()["n"]

    failed = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM whatsapp_messages
        WHERE status='failed'
        """
    ).fetchone()["n"]

    recent = c.execute(
        """
        SELECT *
        FROM campaigns
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()

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
# GOOGLE CONTACTS ALIAS
# =========================================================

@app.route("/google/contacts")
def google_contacts():

    return redirect(
        url_for("contacts")
    )


# =========================================================
# CONTACTS
# =========================================================

@app.route(
    "/contacts",
    methods=["GET", "POST"]
)
def contacts():

    if request.method == "POST":

        f = request.files.get(
            "file"
        )

        if not f:

            flash(
                "CSV file select करें."
            )

            return redirect(
                url_for("contacts")
            )

        try:

            text = f.read().decode(
                "utf-8-sig",
                errors="ignore"
            )

            added, extra = import_csv_text(
                text
            )

            flash(
                f"{added} contacts imported. "
                f"{extra}"
            )

        except Exception as e:

            flash(
                f"Import error: {e}"
            )

        return redirect(
            url_for("contacts")
        )

    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM contacts
        ORDER BY id DESC
        """
    ).fetchall()

    c.close()

    return render_template(
        "contacts.html",
        rows=rows
    )


# =========================================================
# DELETE SELECTED CONTACTS
# =========================================================

@app.route(
    "/contacts/delete-selected",
    methods=["POST"]
)
def delete_selected_contacts():

    contact_ids = request.form.getlist(
        "contact_ids"
    )

    if not contact_ids:

        flash(
            "Please select at least one contact."
        )

        return redirect(
            url_for("contacts")
        )

    c = db()

    deleted = 0

    try:

        for contact_id in contact_ids:

            try:

                contact_id = int(
                    contact_id
                )

            except ValueError:

                continue

            result = c.execute(
                """
                DELETE FROM contacts
                WHERE id = ?
                """,
                (contact_id,)
            )

            deleted += result.rowcount

        c.commit()

        flash(
            f"{deleted} contact(s) deleted successfully."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Delete error: {e}"
        )

    finally:

        c.close()

    return redirect(
        url_for("contacts")
    )


# =========================================================
# DELETE SINGLE CONTACT
# =========================================================

@app.route(
    "/contacts/delete/<int:contact_id>",
    methods=["POST"]
)
def delete_contact(contact_id):

    c = db()

    try:

        result = c.execute(
            """
            DELETE FROM contacts
            WHERE id = ?
            """,
            (contact_id,)
        )

        c.commit()

        if result.rowcount > 0:

            flash(
                "Contact deleted successfully."
            )

        else:

            flash(
                "Contact not found."
            )

    except Exception as e:

        c.rollback()

        flash(
            f"Delete error: {e}"
        )

    finally:

        c.close()

    return redirect(
        url_for("contacts")
    )


# =========================================================
# DELETE ALL CONTACTS
# =========================================================

@app.route(
    "/contacts/delete-all",
    methods=["POST"]
)
def delete_all_contacts():

    c = db()

    try:

        result = c.execute(
            """
            DELETE FROM contacts
            """
        )

        deleted = result.rowcount

        c.commit()

        flash(
            f"{deleted} contact(s) deleted successfully."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Delete all error: {e}"
        )

    finally:

        c.close()

    return redirect(
        url_for("contacts")
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
            error=(
                "Google OAuth environment variables "
                "are not configured."
            )
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
            connected=(
                False
                if "session expired"
                in str(error).lower()
                else True
            ),
            files=[],
            error=error
        )

    return render_template(
        "google_drive.html",
        connected=True,
        files=(
            data.get("files", [])
            if isinstance(data, dict)
            else []
        ),
        error=None
    )


# =========================================================
# GOOGLE LOGIN
# =========================================================

@app.route("/google/login")
def google_login():

    if not google_configured():

        flash(
            "Google OAuth configure नहीं है. "
            "GOOGLE_CLIENT_ID और "
            "GOOGLE_CLIENT_SECRET "
            "Render में add करें."
        )

        return redirect(
            url_for("google_drive")
        )

    state = secrets.token_urlsafe(32)

    session[
        "google_oauth_state"
    ] = state

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

    return redirect(
        GOOGLE_AUTH_URL
        + "?"
        + urlencode(params)
    )


# =========================================================
# GOOGLE OAUTH CALLBACK
# =========================================================

@app.route(
    "/google/oauth/callback"
)
def google_oauth_callback():

    error = request.args.get(
        "error"
    )

    if error:

        flash(
            f"Google authorization "
            f"cancelled/error: {error}"
        )

        return redirect(
            url_for("google_drive")
        )

    state = request.args.get(
        "state"
    )

    saved_state = session.pop(
        "google_oauth_state",
        None
    )

    if (
        not state
        or
        not saved_state
        or
        state != saved_state
    ):

        return (
            "Invalid OAuth state.",
            400
        )

    code = request.args.get(
        "code"
    )

    if not code:

        return (
            "Authorization code missing.",
            400
        )

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

            return jsonify(
                {
                    "status": "error",
                    "message": (
                        "Google token exchange failed"
                    ),
                    "details": data
                }
            ), 400

        session[
            "google_access_token"
        ] = data.get(
            "access_token"
        )

        if data.get(
            "refresh_token"
        ):

            session[
                "google_refresh_token"
            ] = data.get(
                "refresh_token"
            )

        flash(
            "Google Drive connected successfully."
        )

        return redirect(
            url_for("google_drive")
        )

    except Exception as e:

        return jsonify(
            {
                "status": "error",
                "message": str(e)
            }
        ), 500


# =========================================================
# GOOGLE LOGOUT
# =========================================================

@app.route("/google/logout")
def google_logout():

    session.pop(
        "google_access_token",
        None
    )

    session.pop(
        "google_refresh_token",
        None
    )

    flash(
        "Google Drive disconnected."
    )

    return redirect(
        url_for("google_drive")
    )


# =========================================================
# GOOGLE DRIVE IMPORT
# =========================================================

@app.route(
    "/google/drive/import/<file_id>",
    methods=["POST"]
)
def google_drive_import(file_id):

    if not google_token():

        flash(
            "पहले Google Drive connect करें."
        )

        return redirect(
            url_for("google_drive")
        )

    mime_type = request.form.get(
        "mime_type",
        "text/csv"
    )

    content, error = download_drive_file(
        file_id,
        mime_type
    )

    if error:

        flash(
            f"Drive import error: {error}"
        )

        return redirect(
            url_for("google_drive")
        )

    try:

        text = content.decode(
            "utf-8-sig",
            errors="ignore"
        )

        added, extra = import_csv_text(
            text
        )

        flash(
            f"Google Drive से {added} "
            f"contacts import हुए. {extra}"
        )

    except Exception as e:

        flash(
            f"CSV import error: {str(e)}"
        )

    return redirect(
        url_for("google_drive")
    )


# =========================================================
# GOOGLE CAMPAIGNS ALIAS
# =========================================================

@app.route(
    "/google/campaigns",
    methods=["GET", "POST"]
)
def google_campaigns():

    return campaigns()


# =========================================================
# CAMPAIGNS
# =========================================================

@app.route(
    "/campaigns",
    methods=["GET", "POST"]
)
def campaigns():

    # =====================================================
    # CREATE CAMPAIGN
    # =====================================================

    if request.method == "POST":

        name = request.form.get(
            "name",
            ""
        ).strip()

        message = request.form.get(
            "message",
            ""
        ).strip()

        group_name = request.form.get(
            "group_name",
            ""
        ).strip()

        campaign_type = request.form.get(
            "campaign_type",
            ""
        ).strip()

        target_mode = request.form.get(
            "target_mode",
            "saved"
        ).strip()

        # Optional Google Drive attachment selected on the form.
        drive_file_id = request.form.get("drive_file_id", "").strip()
        drive_file_name = request.form.get("drive_file_name", "").strip()
        drive_mime_type = request.form.get("drive_mime_type", "").strip()

        # -------------------------------------------------
        # BASIC VALIDATION
        # -------------------------------------------------

        if not name:

            flash(
                "Campaign name is required."
            )

            return redirect(
                url_for("campaigns")
            )

        if not message:

            flash(
                "Message is required."
            )

            return redirect(
                url_for("campaigns")
            )

        # =================================================
        # SINGLE NUMBER CAMPAIGN
        # =================================================

        if campaign_type == "single":

            # ---------------------------------------------
            # MANUAL NUMBER
            # ---------------------------------------------

            if target_mode == "manual":

                manual_number = clean_phone(
                    request.form.get(
                        "manual_number",
                        ""
                    ).strip()
                )

                if not manual_number:

                    flash(
                        "Please enter a WhatsApp number."
                    )

                    return redirect(
                        url_for("campaigns")
                    )

                group_name = (
                    "__SINGLE__:"
                    + manual_number
                )

            # ---------------------------------------------
            # SAVED CONTACT
            # ---------------------------------------------

            else:

                single_number = clean_phone(
                    request.form.get(
                        "single_number",
                        ""
                    ).strip()
                )

                if not single_number:

                    flash(
                        "Please select a saved contact."
                    )

                    return redirect(
                        url_for("campaigns")
                    )

                group_name = (
                    "__SINGLE__:"
                    + single_number
                )

        # =================================================
        # GROUP CAMPAIGN
        # =================================================

        else:

            if not group_name:

                flash(
                    "Please select a contact group."
                )

                return redirect(
                    url_for("campaigns")
                )

        # =================================================
        # SAVE CAMPAIGN
        # =================================================

        c = db()

        try:

            c.execute(
                """
                INSERT INTO campaigns
                (
                    name,
                    message,
                    group_name,
                    status,
                    drive_file_id,
                    drive_file_name,
                    drive_mime_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    message,
                    group_name,
                    "Draft",
                    drive_file_id or None,
                    drive_file_name or None,
                    drive_mime_type or None
                )
            )

            c.commit()

            flash(
                "Campaign saved as Draft."
            )

        except Exception as e:

            c.rollback()

            flash(
                f"Campaign save error: {e}"
            )

        finally:

            c.close()

        return redirect(
            url_for("campaigns")
        )

    # =====================================================
    # CAMPAIGN LIST
    # =====================================================

    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM campaigns
        ORDER BY id DESC
        """
    ).fetchall()

    # =====================================================
    # GROUPS
    # =====================================================

    groups = [

        r["group_name"]

        for r in c.execute(
            """
            SELECT DISTINCT group_name
            FROM contacts
            WHERE group_name IS NOT NULL
            AND group_name != ''
            ORDER BY group_name
            """
        ).fetchall()

    ]

    # =====================================================
    # CONTACTS
    # =====================================================

    contact_rows = c.execute(
        """
        SELECT *
        FROM contacts
        ORDER BY name ASC
        """
    ).fetchall()

    c.close()

    # =====================================================
    # GOOGLE DRIVE FILES
    # =====================================================

    drive_files = []
    drive_error = None

    if google_token():

        data, drive_error = drive_files_list()

        if isinstance(data, dict):

            drive_files = data.get(
                "files",
                []
            )

    return render_template(
        "campaigns.html",

        rows=rows,

        groups=groups,

        contacts=contact_rows,

        google_connected=bool(
            google_token()
        ),

        drive_files=drive_files,

        drive_error=drive_error
    )


# =========================================================
# SEND CAMPAIGN
# =========================================================

@app.route("/campaign/<int:cid>/send", methods=["GET", "POST"])
def send_campaign(cid):

    if request.method == "GET":
        return redirect(url_for("campaigns"))

    c = db()

    try:
        # -------------------------------------------------
        # GET CAMPAIGN
        # -------------------------------------------------

        campaign = c.execute("""
            SELECT *
            FROM campaigns
            WHERE id = ?
        """, (cid,)).fetchone()

        if not campaign:
            flash("Campaign not found.")
            return redirect(url_for("campaigns"))

        # -------------------------------------------------
        # CHECK WHATSAPP CONFIG
        # -------------------------------------------------

        if not whatsapp_configured():

            c.execute("""
                UPDATE campaigns
                SET status = 'API Not Configured'
                WHERE id = ?
            """, (cid,))

            c.commit()

            flash(
                "Please configure WHATSAPP_ACCESS_TOKEN "
                "and WHATSAPP_PHONE_NUMBER_ID."
            )

            return redirect(url_for("campaigns"))

        # =================================================
        # BUILD RECIPIENT LIST
        # =================================================

        contacts_list = []

        group_value = campaign["group_name"] or ""

        # -------------------------------------------------
        # SINGLE NUMBER CAMPAIGN
        # -------------------------------------------------

        if group_value.startswith("__SINGLE__:"):

            manual_number = group_value.replace(
                "__SINGLE__:",
                "",
                1
            ).strip()

            manual_number = clean_phone(manual_number)

            if manual_number:

                contacts_list = [{
                    "id": None,
                    "name": "Customer",
                    "phone": manual_number
                }]

        # -------------------------------------------------
        # GROUP CAMPAIGN
        # -------------------------------------------------

        else:

            q = """
                SELECT *
                FROM contacts
            """

            params = ()

            if group_value:

                q += """
                    WHERE group_name = ?
                """

                params = (group_value,)

            q += """
                ORDER BY id ASC
            """

            contacts_list = c.execute(
                q,
                params
            ).fetchall()

        # -------------------------------------------------
        # NO RECIPIENTS
        # -------------------------------------------------

        if not contacts_list:

            c.execute("""
                UPDATE campaigns
                SET status = 'No Recipients'
                WHERE id = ?
            """, (cid,))

            c.commit()

            flash("No recipients found for this campaign.")

            return redirect(url_for("campaigns"))

        # =================================================
        # TEMPLATE CONFIGURATION
        # =================================================

        template_name = get_env(
            "WHATSAPP_TEMPLATE_NAME"
        )

        template_language = (
            get_env("WHATSAPP_TEMPLATE_LANGUAGE")
            or "en_US"
        )

        # =================================================
        # SEND LOOP
        # =================================================

        sent = 0
        failed = 0
        text_sent = 0
        template_sent = 0

        for contact in contacts_list:

            try:

                # -------------------------------------------------
                # CONTACT DETAILS
                # -------------------------------------------------

                contact_id = contact.get("id")

                contact_name = (
                    contact.get("name")
                    or "Customer"
                )

                phone = clean_phone(
                    contact.get("phone")
                    or ""
                )

                if not phone:

                    failed += 1

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
                            contact_id,
                            "",
                            campaign["message"],
                            None,
                            "outgoing",
                            "failed",
                            "Invalid phone number"
                        ))

                        c.commit()

                    except Exception:
                        c.rollback()

                    continue

                # -------------------------------------------------
                # PERSONALIZED MESSAGE
                # -------------------------------------------------

                body = (
                    campaign["message"]
                    .replace(
                        "{{name}}",
                        str(contact_name)
                    )
                )

                # =================================================
                # CHECK 24-HOUR CUSTOMER WINDOW
                # =================================================

                latest_incoming = c.execute("""
                    SELECT created_at
                    FROM whatsapp_incoming
                    WHERE phone = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (phone,)).fetchone()

                within_24_hours = False

                if latest_incoming:

                    incoming_time = (
                        latest_incoming["created_at"]
                    )

                    if incoming_time:

                        try:

                            age_seconds = (
                                datetime.utcnow()
                                - incoming_time
                            ).total_seconds()

                            if (
                                age_seconds >= 0
                                and age_seconds <= 86400
                            ):
                                within_24_hours = True

                        except Exception as time_error:

                            print(
                                "24 HOUR CHECK ERROR:",
                                str(time_error)
                            )

                # =================================================
                # SEND NORMAL TEXT
                # =================================================

                if within_24_hours:

                    ok, message_id, response = (
                        send_whatsapp_text(
                            phone,
                            body
                        )
                    )

                    send_type = "text"

                    if ok:

                        status = "accepted"
                        sent += 1
                        text_sent += 1
                        error_text = None

                    else:

                        status = "failed"
                        failed += 1

                        if isinstance(
                            response,
                            (dict, list)
                        ):

                            error_text = json.dumps(
                                response,
                                ensure_ascii=False
                            )

                        else:

                            error_text = str(response)

                # =================================================
                # OUTSIDE 24 HOURS
                # USE APPROVED TEMPLATE
                # =================================================

                else:

                    send_type = "template"

                    if not template_name:

                        ok = False
                        message_id = None

                        error_text = (
                            "Outside 24-hour window. "
                            "WHATSAPP_TEMPLATE_NAME is not "
                            "configured in Render Environment."
                        )

                        failed += 1
                        status = "failed"

                    else:

                        # -----------------------------------------
                        # TEMPLATE PARAMETERS
                        #
                        # This sends contact name as {{1}}
                        # if your approved template has a
                        # body variable.
                        # -----------------------------------------

                        template_parameters = [
                            contact_name
                        ]

                        ok, message_id, response = (
                            send_whatsapp_template(
                                phone,
                                template_name,
                                template_language,
                                template_parameters
                            )
                        )

                        if ok:

                            status = "accepted"
                            sent += 1
                            template_sent += 1
                            error_text = None

                        else:

                            status = "failed"
                            failed += 1

                            if isinstance(
                                response,
                                (dict, list)
                            ):

                                error_text = json.dumps(
                                    response,
                                    ensure_ascii=False
                                )

                            else:

                                error_text = str(response)

                # =================================================
                # SAVE MESSAGE
                # =================================================

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
                        contact_id,
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

            except Exception as contact_error:

                print(
                    "CONTACT SEND ERROR:",
                    str(contact_error)
                )

                failed += 1

                try:

                    c.rollback()

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
                        contact.get("id"),
                        clean_phone(
                            contact.get("phone")
                            or ""
                        ),
                        campaign["message"],
                        None,
                        "outgoing",
                        "failed",
                        str(contact_error)
                    ))

                    c.commit()

                except Exception:
                    c.rollback()

        # =================================================
        # FINAL CAMPAIGN STATUS
        # =================================================

        c.execute("""
            UPDATE campaigns
            SET status = ?
            WHERE id = ?
        """, (
            (
                f"Accepted {sent}, "
                f"Failed {failed}"
            ),
            cid
        ))

        c.commit()

        # =================================================
        # RESULT MESSAGE
        # =================================================

        flash(
            f"Campaign finished: "
            f"{sent} accepted, "
            f"{failed} failed. "
            f"Text: {text_sent}, "
            f"Template: {template_sent}"
        )

        return redirect(
            url_for("campaigns")
        )

    except Exception as e:

        print(
            "CAMPAIGN SEND ERROR:",
            str(e)
        )

        try:
            c.rollback()
        except Exception:
            pass

        flash(
            f"Campaign error: {str(e)}"
        )

        return redirect(
            url_for("campaigns")
        )

    finally:

        try:
            c.close()
        except Exception:
            pass

# =========================================================
# WEBHOOK VERIFY
# =========================================================

WEBHOOK_VERIFY_TOKEN = (
    get_env(
        "WEBHOOK_VERIFY_TOKEN"
    )
    or
    "margdarshak_webhook_2026"
)


@app.route(
    "/webhook",
    methods=["GET"]
)
def webhook_verify():

    mode = request.args.get(
        "hub.mode"
    )

    token = request.args.get(
        "hub.verify_token"
    )

    challenge = request.args.get(
        "hub.challenge"
    )

    if (
        mode == "subscribe"
        and
        token == WEBHOOK_VERIFY_TOKEN
    ):

        return challenge, 200

    return (
        "Forbidden",
        403
    )


# =========================================================
# WEBHOOK RECEIVE
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook_receive():
    print("=== WEBHOOK POST RECEIVED ===")
    print("SIGNATURE:", request.headers.get("X-Hub-Signature-256"))
    print("BODY:", request.get_data(as_text=True))

    c = None

    try:

        data = request.get_json(
            silent=True
        )

        if not data:

            return "OK", 200

        c = db()

        # =================================================
        # SAVE WEBHOOK EVENT
        # =================================================

        c.execute(
            """
            INSERT INTO webhook_events
            (
                event_type,
                payload
            )
            VALUES (?, ?)
            """,
            (
                "whatsapp",
                json.dumps(
                    data,
                    ensure_ascii=False
                )
            )
        )

        # =================================================
        # ENTRY
        # =================================================

        entries = data.get(
            "entry",
            []
        )

        for entry in entries:

            changes = entry.get(
                "changes",
                []
            )

            for change in changes:

                value = change.get(
                    "value",
                    {}
                )

                # =================================================
                # MESSAGE STATUS
                # =================================================

                statuses = value.get(
                    "statuses",
                    []
                )

                for status in statuses:

                    wa_id = status.get(
                        "id"
                    )

                    new_status = status.get(
                        "status"
                    )

                    errors = status.get(
                        "errors"
                    )

                    error_text = None

                    if errors:

                        error_text = json.dumps(
                            errors,
                            ensure_ascii=False
                        )

                    if wa_id:

                        c.execute(
                            """
                            UPDATE whatsapp_messages
                            SET
                                status = ?,
                                error = ?,
                                updated_at =
                                    CURRENT_TIMESTAMP
                            WHERE wa_message_id = ?
                            """,
                            (
                                new_status,
                                error_text,
                                wa_id
                            )
                        )

                # =================================================
                # INCOMING MESSAGES
                # =================================================

                messages_data = value.get(
                    "messages",
                    []
                )

                for msg in messages_data:

                    wa_message_id = msg.get(
                        "id"
                    )

                    sender = msg.get(
                        "from"
                    )

                    msg_type = msg.get(
                        "type"
                    )

                    message_text = ""

                    # ------------------------------------------------
                    # TEXT
                    # ------------------------------------------------

                    if msg_type == "text":

                        message_text = (
                            msg.get(
                                "text",
                                {}
                            ).get(
                                "body",
                                ""
                            )
                        )

                    # ------------------------------------------------
                    # BUTTON
                    # ------------------------------------------------

                    elif msg_type == "button":

                        message_text = (
                            msg.get(
                                "button",
                                {}
                            ).get(
                                "text",
                                ""
                            )
                        )

                    # ------------------------------------------------
                    # INTERACTIVE
                    # ------------------------------------------------

                    elif msg_type == "interactive":

                        interactive = msg.get(
                            "interactive",
                            {}
                        )

                        if (
                            interactive.get(
                                "type"
                            )
                            ==
                            "button_reply"
                        ):

                            message_text = (
                                interactive
                                .get(
                                    "button_reply",
                                    {}
                                )
                                .get(
                                    "title",
                                    ""
                                )
                            )

                        elif (
                            interactive.get(
                                "type"
                            )
                            ==
                            "list_reply"
                        ):

                            message_text = (
                                interactive
                                .get(
                                    "list_reply",
                                    {}
                                )
                                .get(
                                    "title",
                                    ""
                                )
                            )

                    # =================================================
                    # SAVE INCOMING
                    # =================================================

                    if wa_message_id:

                        try:

                            c.execute(
                                """
                                INSERT INTO
                                whatsapp_incoming
                                (
                                    wa_message_id,
                                    phone,
                                    message_type,
                                    message
                                )
                                VALUES (?, ?, ?, ?)
                                """,
                                (
                                    wa_message_id,
                                    sender,
                                    msg_type,
                                    message_text
                                )
                            )

                        except IntegrityError:

                            c.rollback()

                            # Re-open transaction
                            c.execute(
                                "SELECT 1"
                            )

                    # =================================================
                    # AUTO SAVE CONTACT
                    # =================================================

                    if sender:

                        cleaned_sender = clean_phone(
                            sender
                        )

                        existing = c.execute(
                            """
                            SELECT id
                            FROM contacts
                            WHERE phone = ?
                            """,
                            (
                                cleaned_sender,
                            )
                        ).fetchone()

                        if not existing:

                            profile_name = (
                                "WhatsApp Customer"
                            )

                            contacts_data = (
                                value.get(
                                    "contacts",
                                    []
                                )
                            )

                            if contacts_data:

                                profile = (
                                    contacts_data[0]
                                    .get(
                                        "profile",
                                        {}
                                    )
                                )

                                profile_name = (
                                    profile.get(
                                        "name"
                                    )
                                    or
                                    "WhatsApp Customer"
                                )

                            try:

                                c.execute(
                                    """
                                    INSERT INTO contacts
                                    (
                                        name,
                                        phone,
                                        group_name
                                    )
                                    VALUES (?, ?, ?)
                                    """,
                                    (
                                        profile_name,
                                        cleaned_sender,
                                        "WhatsApp"
                                    )
                                )

                            except IntegrityError:

                                c.rollback()

        c.commit()

        return (
            "EVENT_RECEIVED",
            200
        )

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            str(e)
        )

        if c:

            try:
                c.rollback()
            except Exception:
                pass

        # WhatsApp webhook should receive 200
        return (
            "EVENT_RECEIVED",
            200
        )

    finally:

        if c:

            try:
                c.close()
            except Exception:
                pass


# =========================================================
# MESSAGE TEST ROUTE
# =========================================================

@app.route("/messages-test")
def messages_test():

    return {
        "status": "ok",
        "message": "MESSAGES ROUTING WORKING"
    }


# =========================================================
# MESSAGES
# =========================================================

@app.route("/messages")
def messages():

    c = db()

    rows = c.execute(
        """
        SELECT
            wm.*,
            c.name AS contact_name
        FROM whatsapp_messages wm
        LEFT JOIN contacts c
            ON c.id = wm.contact_id
        ORDER BY wm.id DESC
        LIMIT 500
        """
    ).fetchall()

    c.close()

    return render_template(
        "messages.html",
        rows=rows
    )


# =========================================================
# INCOMING
# =========================================================

@app.route("/incoming")
def incoming():

    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM whatsapp_incoming
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()

    c.close()

    return render_template(
        "incoming.html",
        rows=rows
    )


# =========================================================
# WEBHOOK TEST
# =========================================================

@app.route("/webhook-test")
def webhook_test():

    return {
        "status": "ok",
        "message": "WEBHOOK ROUTING WORKING"
    }


# =========================================================
# WEBHOOK LOGS
# =========================================================

@app.route("/webhook/logs")
def webhook_logs():

    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM webhook_events
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()

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

    return render_template(
        "privacy.html"
    )


# =========================================================
# SETTINGS
# =========================================================

@app.route("/settings")
def settings():

    return render_template(
        "settings.html",

        config_ok=whatsapp_configured(),

        webhook_token_ok=bool(
            get_env(
                "WEBHOOK_VERIFY_TOKEN"
            )
        ),

        app_secret_ok=bool(
            get_env(
                "META_APP_SECRET"
            )
        ),

        google_configured=google_configured(),

        google_connected=bool(
            google_token()
        ),

        google_redirect_uri=(
            GOOGLE_REDIRECT_URI
        ),

        database_configured=bool(
            get_env("DATABASE_URL")
        )
    )


# =========================================================
# WHATSAPP STATUS API
# =========================================================

@app.route(
    "/api/whatsapp/status"
)
def whatsapp_status():

    return jsonify(
        {
            "configured":
                whatsapp_configured(),

            "phone_number_id":
                bool(
                    get_env(
                        "WHATSAPP_PHONE_NUMBER_ID"
                    )
                ),

            "access_token":
                bool(
                    get_env(
                        "WHATSAPP_ACCESS_TOKEN"
                    )
                ),

            "app_secret":
                bool(
                    get_env(
                        "META_APP_SECRET"
                    )
                ),

            "webhook_verify_token":
                bool(
                    get_env(
                        "WEBHOOK_VERIFY_TOKEN"
                    )
                ),

            "database_configured":
                bool(
                    get_env(
                        "DATABASE_URL"
                    )
                ),

            "google_configured":
                google_configured(),

            "google_connected":
                bool(
                    google_token()
                ),

            "google_redirect_uri":
                GOOGLE_REDIRECT_URI
        }
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    database_ok = False
    database_error = None

    try:

        c = db()

        c.execute(
            "SELECT 1"
        ).fetchone()

        c.close()

        database_ok = True

    except Exception as e:

        database_error = str(e)

        print(
            "DATABASE HEALTH ERROR:",
            database_error
        )

    return jsonify(
        {
            "status": "ok",

            "database_configured":
                bool(
                    get_env(
                        "DATABASE_URL"
                    )
                ),

            "database_connected":
                database_ok,

            "whatsapp_configured":
                whatsapp_configured(),

            "google_configured":
                google_configured(),

            "google_connected":
                bool(
                    google_token()
                ),

            "time":
                datetime.utcnow().isoformat(),

            "database_error":
                database_error
        }
    )


# =========================================================
# DEBUG ROUTES
# =========================================================

@app.route("/debug/routes")
def debug_routes():

    return jsonify(
        [
            {
                "rule": str(rule),
                "endpoint": rule.endpoint,
                "methods": sorted(
                    rule.methods
                )
            }

            for rule
            in app.url_map.iter_rules()
        ]
    )


@app.route("/debug/test")
def debug_test():

    return {
        "status": "ok",
        "message": "DEBUG TEST ROUTE WORKING"
    }


# =========================================================
# DELETE CAMPAIGN
# =========================================================

@app.route(
    "/campaign/<int:cid>/delete",
    methods=["POST"]
)
def delete_campaign(cid):

    c = db()

    try:

        result = c.execute(
            """
            DELETE FROM campaigns
            WHERE id = ?
            """,
            (cid,)
        )

        c.commit()

        if result.rowcount > 0:

            flash(
                "Campaign deleted successfully."
            )

        else:

            flash(
                "Campaign not found."
            )

    except Exception as e:

        c.rollback()

        flash(
            f"Delete campaign error: {e}"
        )

    finally:

        c.close()

    return redirect(
        url_for("campaigns")
    )


# ============================================================
# WHATSAPP TEMPLATE DEBUG
# ============================================================

@app.route("/debug/templates")
def debug_templates():

    import os
    import requests

    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    waba_id = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")

    if not access_token:
        return jsonify({
            "status": "error",
            "message": "WHATSAPP_ACCESS_TOKEN is not configured"
        }), 500

    if not waba_id:
        return jsonify({
            "status": "error",
            "message": "WHATSAPP_BUSINESS_ACCOUNT_ID is not configured"
        }), 500

    url = f"https://graph.facebook.com/v23.0/{waba_id}/message_templates"

    params = {
        "fields": "name,language,status,category,components",
        "limit": 100
    }

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=30
        )

        data = response.json()

        if response.status_code != 200:
            return jsonify({
                "status": "error",
                "http_status": response.status_code,
                "meta_response": data
            }), response.status_code

        templates = []

        for item in data.get("data", []):
            templates.append({
                "name": item.get("name"),
                "language": item.get("language"),
                "status": item.get("status"),
                "category": item.get("category"),
                "components": item.get("components", []) or []
            })

        return jsonify({
            "status": "ok",
            "templates": templates
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# =========================================================
# RUN
# =========================================================


# =========================================================
# TEMPLATE CAMPAIGNS
# =========================================================

def upload_whatsapp_media(file_path, media_type=None):
    """Upload an image, video or document to WhatsApp Cloud API and return its media ID."""
    token = get_env("WHATSAPP_ACCESS_TOKEN")
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")

    if not token or not phone_id:
        return None, "WhatsApp API credentials missing"

    if not file_path or not os.path.exists(file_path):
        return None, f"Header media not found: {file_path}"

    guessed_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    if media_type:
        media_type = str(media_type).lower()
    else:
        if guessed_type.startswith("image/"):
            media_type = "image"
        elif guessed_type.startswith("video/"):
            media_type = "video"
        else:
            media_type = "document"

    expected_prefix = {
        "image": "image/",
        "video": "video/"
    }.get(media_type)

    if expected_prefix and not guessed_type.startswith(expected_prefix):
        return None, f"Selected media type is {media_type}, but file MIME type is {guessed_type}."

    url = f"https://graph.facebook.com/v23.0/{phone_id}/media"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with open(file_path, "rb") as media_file:
            response = requests.post(
                url,
                headers=headers,
                data={"messaging_product": "whatsapp", "type": media_type},
                files={
                    "file": (
                        os.path.basename(file_path),
                        media_file,
                        guessed_type
                    )
                },
                timeout=120
            )

        try:
            data = response.json()
        except Exception:
            data = response.text

        if response.ok and isinstance(data, dict) and data.get("id"):
            return data["id"], None

        return None, data

    except Exception as e:
        return None, str(e)



def fetch_meta_templates():
    import re
    """Fetch all WhatsApp message templates from the configured WABA."""
    token = get_env("WHATSAPP_ACCESS_TOKEN")
    waba_id = (
        get_env("WHATSAPP_BUSINESS_ACCOUNT_ID")
        or get_env("WHATSAPP_WABA_ID")
    )

    if not token or not waba_id:
        return [], "WHATSAPP_ACCESS_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID are required."

    url = f"https://graph.facebook.com/v23.0/{waba_id}/message_templates"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "fields": "id,name,language,status,category,components",
        "limit": 100
    }

    try:
        result = []
        next_url = url

        while next_url:
            r = requests.get(next_url, headers=headers, params=params, timeout=30)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}

            if not r.ok:
                return [], json.dumps(data, ensure_ascii=False)

            for x in data.get("data", []):
                components = x.get("components") or []
                body_text = ""
                header_component = None
                footer_component = None
                buttons_component = None
                header_format = ""

                for component in components:
                    component_type = str(component.get("type", "")).upper()
                    if component_type == "BODY":
                        body_text = component.get("text") or ""
                    elif component_type == "HEADER":
                        header_component = component
                        header_format = str(component.get("format", "")).upper()
                    elif component_type == "FOOTER":
                        footer_component = component
                    elif component_type == "BUTTONS":
                        buttons_component = component

                variable_numbers = []
                for match in re.findall(r"\{\{\s*(\d+)\s*\}\}", body_text):
                    n = int(match)
                    if n not in variable_numbers:
                        variable_numbers.append(n)
                variable_numbers.sort()

                result.append({
                    "id": x.get("id", ""),
                    "name": x.get("name", ""),
                    "language": x.get("language", "en_US"),
                    "status": x.get("status", "UNKNOWN"),
                    "category": x.get("category", ""),
                    "components": components,
                    "header": header_component,
                    "footer": footer_component,
                    "buttons": buttons_component,
                    "header_format": header_format,
                    "has_image_header": header_format == "IMAGE",
                    "has_media_header": header_format in ("IMAGE", "VIDEO", "DOCUMENT"),
                    "header_media_type": {
                        "IMAGE": "image",
                        "VIDEO": "video",
                        "DOCUMENT": "document"
                    }.get(header_format, ""),
                    "body_text": body_text,
                    "variables": variable_numbers,
                    "variable_count": len(variable_numbers)
                })

            next_url = (data.get("paging") or {}).get("next")
            params = None

        unique = []
        seen = set()
        for item in result:
            key = (item.get("name", ""), item.get("language", ""))
            if key not in seen:
                seen.add(key)
                unique.append(item)

        unique.sort(key=lambda x: (
            0 if x.get("name") == "mbbs_admission_alert" else 1,
            x.get("name", "").lower(),
            x.get("language", "")
        ))
        return unique, None

    except Exception as e:
        return [], str(e)


def template_campaign_recipients(campaign):
    target_type = campaign["target_type"]

    # A campaign may contain a mixed send list: numbers typed manually and
    # contacts selected from the CRM. Keep the original target modes for
    # group/all campaigns, while single/selected campaigns use this list.
    if target_type in ("single", "selected"):
        recipients = []
        seen = set()

        raw_numbers = campaign.get("manual_numbers") or ""
        try:
            numbers = json.loads(raw_numbers) if raw_numbers else []
            if not isinstance(numbers, list):
                numbers = []
        except Exception:
            numbers = []

        legacy_number = campaign.get("manual_number") or ""
        if legacy_number and not numbers:
            numbers = [legacy_number]

        for raw in numbers:
            phone = clean_phone(str(raw))
            if phone and phone not in seen:
                seen.add(phone)
                recipients.append({"id": None, "name": "Customer", "phone": phone})

        c = db()
        try:
            raw_ids = campaign.get("selected_contact_ids") or ""
            try:
                ids = [int(x) for x in json.loads(raw_ids)] if raw_ids else []
            except Exception:
                ids = []
            ids = list(dict.fromkeys(ids))
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                rows = c.execute(
                    f"SELECT * FROM contacts WHERE id IN ({placeholders}) ORDER BY id ASC",
                    tuple(ids)
                ).fetchall()
                for row in rows:
                    phone = clean_phone(row.get("phone") or "")
                    if phone and phone not in seen:
                        seen.add(phone)
                        recipients.append(row)
        finally:
            c.close()
        return recipients

    c = db()
    try:
        if target_type == "selected":
            raw_ids = campaign.get("selected_contact_ids") or ""
            try:
                ids = [int(x) for x in json.loads(raw_ids)] if raw_ids else []
            except Exception:
                ids = []
            ids = list(dict.fromkeys(ids))
            if not ids:
                return []
            placeholders = ",".join(["?"] * len(ids))
            return c.execute(
                f"SELECT * FROM contacts WHERE id IN ({placeholders}) ORDER BY id ASC",
                tuple(ids)
            ).fetchall()

        if target_type == "all":
            return c.execute("SELECT * FROM contacts ORDER BY id ASC").fetchall()

        return c.execute(
            "SELECT * FROM contacts WHERE group_name = ? ORDER BY id ASC",
            (campaign["group_name"],)
        ).fetchall()
    finally:
        c.close()


@app.route("/api/meta-template-status")
def meta_template_status():
    templates, error = fetch_meta_templates()
    waba_id = (
        get_env("WHATSAPP_BUSINESS_ACCOUNT_ID")
        or get_env("WHATSAPP_WABA_ID")
    )
    return jsonify({
        "waba_id": waba_id or "",
        "error": error,
        "template_count": len(templates),
        "templates": [
            {
                "name": t.get("name"),
                "language": t.get("language"),
                "status": t.get("status"),
                "category": t.get("category")
            }
            for t in templates
        ],
        "mbbs_admission_alert_found": any(
            t.get("name") == "mbbs_admission_alert"
            for t in templates
        )
    })



@app.route("/template-campaigns", methods=["GET", "POST"])
def template_campaigns():
    templates, template_error = fetch_meta_templates()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        template_name = request.form.get("template_name", "").strip()
        template_language = (
            request.form.get("template_language", "en_US").strip()
            or "en_US"
        )
        target_type = request.form.get("target_type", "group").strip()
        group_name = request.form.get("group_name", "").strip()
        manual_number = clean_phone(request.form.get("manual_number", ""))
        manual_numbers_raw = request.form.get("manual_numbers", "").strip()
        typed_numbers_raw = request.form.get("typed_numbers", "").strip()
        try:
            typed_numbers = [clean_phone(str(x)) for x in json.loads(typed_numbers_raw)] if typed_numbers_raw else []
        except Exception:
            typed_numbers = []
        typed_numbers = list(dict.fromkeys([x for x in typed_numbers if x]))
        try:
            manual_numbers = [clean_phone(str(x)) for x in json.loads(manual_numbers_raw)] if manual_numbers_raw else []
        except Exception:
            manual_numbers = []
        manual_numbers = list(dict.fromkeys([x for x in manual_numbers if x]))
        if manual_number and manual_number not in manual_numbers:
            manual_numbers.insert(0, manual_number)
        parameters = request.form.get("parameters", "").strip()
        selected_contact_ids_raw = request.form.get("selected_contact_ids", "").strip()
        try:
            selected_contact_ids = [int(x) for x in json.loads(selected_contact_ids_raw)] if selected_contact_ids_raw else []
        except Exception:
            selected_contact_ids = []
        selected_contact_ids = list(dict.fromkeys(selected_contact_ids))

        selected = next(
            (
                x for x in templates
                if x["name"] == template_name
                and x["language"] == template_language
            ),
            None
        )

        if not name:
            flash("Campaign name is required.")
            return redirect(url_for("template_campaigns"))

        if not selected:
            flash("Please select a valid Meta template.")
            return redirect(url_for("template_campaigns"))

        if selected["status"].upper() != "APPROVED":
            flash("Only APPROVED templates can be used.")
            return redirect(url_for("template_campaigns"))

        # Optional template-header media upload.
        header_media_path = ""
        header_media_type = selected.get("header_media_type", "")

        uploaded_media = request.files.get("header_media")
        if selected.get("has_media_header") and uploaded_media and uploaded_media.filename:
            filename = secure_filename(uploaded_media.filename)
            if not filename:
                flash("Invalid media filename.")
                return redirect(url_for("template_campaigns"))

            ext = os.path.splitext(filename)[1].lower()
            allowed = {
                "image": {".jpg", ".jpeg", ".png"},
                "video": {".mp4", ".3gp"},
                "document": {
                    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
                    ".ppt", ".pptx", ".txt"
                }
            }

            if ext not in allowed.get(header_media_type, set()):
                flash(
                    f"This template requires a {header_media_type.upper()} header. "
                    f"Please upload a compatible file."
                )
                return redirect(url_for("template_campaigns"))

            upload_dir = os.path.join("static", "uploads", "template_headers")
            os.makedirs(upload_dir, exist_ok=True)
            safe_name = f"{secrets.token_hex(8)}_{filename}"
            header_media_path = os.path.join(upload_dir, safe_name)
            uploaded_media.save(header_media_path)

        # Media is intentionally optional while saving. If it is not supplied,
        # the Send dialog will request it later when the template requires it.

        # Variables are OPTIONAL while creating/saving the campaign.
        # If values are provided, they must match the number of Meta variables.
        parameter_values = [
            value.strip()
            for value in parameters.split("||")
        ] if parameters else []

        expected_variables = int(selected.get("variable_count", 0) or 0)

        # Variables are optional while saving. If some values are supplied,
        # preserve them; completeness is checked when Send is pressed.

        if target_type == "group" and not group_name:
            flash("Please select a contact group.")
            return redirect(url_for("template_campaigns"))

        if target_type == "single" and not manual_numbers and not selected_contact_ids:
            flash("Please add at least one WhatsApp number or select a contact.")
            return redirect(url_for("template_campaigns"))

        if target_type == "selected" and not manual_numbers and not selected_contact_ids:
            flash("Please add at least one number or select at least one contact.")
            return redirect(url_for("template_campaigns"))

        c = db()
        try:
            c.execute("""
                INSERT INTO template_campaigns
                (name, template_name, template_language, target_type,
                 group_name, manual_number, parameters, status,
                 header_image_path, header_media_path, header_media_type,
                 selected_contact_ids, manual_numbers, typed_numbers)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                template_name,
                template_language,
                target_type,
                group_name or None,
                manual_number or None,
                parameters or None,
                "Approved",
                header_media_path if header_media_type == "image" else None,
                header_media_path or None,
                header_media_type or None,
                json.dumps(selected_contact_ids) if selected_contact_ids else None,
                json.dumps(manual_numbers) if manual_numbers else None,
                json.dumps(typed_numbers) if typed_numbers else None
            ))
            c.commit()
            flash("Template campaign saved. Send Campaign is enabled.")
        except Exception as e:
            c.rollback()
            flash(f"Template campaign save error: {e}")
        finally:
            c.close()

        return redirect(url_for("template_campaigns"))

    c = db()
    try:
        campaigns_list = c.execute(
            "SELECT * FROM template_campaigns ORDER BY id DESC"
        ).fetchall()

        groups = [
            x["group_name"]
            for x in c.execute("""
                SELECT DISTINCT group_name
                FROM contacts
                WHERE group_name IS NOT NULL AND group_name != ''
                ORDER BY group_name
            """).fetchall()
        ]

        contacts = c.execute(
            "SELECT id, name, phone, group_name FROM contacts ORDER BY name ASC, id ASC"
        ).fetchall()
    finally:
        c.close()

    # Build history for the manual-number suggestions only.
    # Contact-directory numbers are deliberately excluded from this list.
    typed_number_history = []
    for row in campaigns_list:
        raw = row["typed_numbers"] if "typed_numbers" in row.keys() else None
        if raw:
            try:
                vals = json.loads(raw)
                if isinstance(vals, list):
                    for v in vals:
                        n = clean_phone(str(v))
                        if n and n not in typed_number_history:
                            typed_number_history.append(n)
            except Exception:
                pass
        legacy = row["manual_number"] if "manual_number" in row.keys() else None
        if legacy:
            n = clean_phone(str(legacy))
            if n and n not in typed_number_history:
                typed_number_history.append(n)
    typed_number_history = typed_number_history[:100]

    status_map = {
        (x["name"], x["language"]): x["status"]
        for x in templates
    }

    for row in campaigns_list:
        status = status_map.get(
            (row["template_name"], row["template_language"])
        )
        if status and row.get("status") not in ("Scheduled", "Sending", "Completed"):
            row["status"] = (
                "Approved" if status.upper() == "APPROVED"
                else status.title()
            )

    meta_template_url = "https://business.facebook.com/latest/whatsapp_manager/message_templates"

    return render_template(
        "template_campaigns.html",
        campaigns=campaigns_list,
        templates=templates,
        groups=groups,
        contacts=contacts,
        typed_number_history=typed_number_history,
        template_error=template_error,
        meta_template_url=meta_template_url
    )


@app.route("/template-campaign/<int:cid>/delete", methods=["POST"])
def delete_template_campaign(cid):
    c = db()
    try:
        result = c.execute(
            "DELETE FROM template_campaigns WHERE id = ?",
            (cid,)
        )
        c.commit()
        if result.rowcount > 0:
            flash("Template campaign deleted successfully.")
        else:
            flash("Template campaign not found.")
    except Exception as e:
        c.rollback()
        flash(f"Delete template campaign error: {e}")
    finally:
        c.close()
    return redirect(url_for("template_campaigns"))


def _prepare_template_campaign_send(cid, request_form=None, request_files=None):
    """Validate and persist send-time variables/media. Returns (campaign, selected, values, media_id, media_type, error)."""
    request_form = request_form or {}
    request_files = request_files or {}
    c = db()
    try:
        campaign = c.execute("SELECT * FROM template_campaigns WHERE id = ?", (cid,)).fetchone()
        if not campaign:
            return None, None, None, None, None, "Template campaign not found."

        templates, error = fetch_meta_templates()
        if error:
            return campaign, None, None, None, None, f"Meta template check failed: {error}"

        selected = next((x for x in templates if x["name"] == campaign["template_name"] and x["language"] == campaign["template_language"]), None)
        if not selected or selected["status"].upper() != "APPROVED":
            return campaign, None, None, None, None, "Template is not approved. Sending is disabled."

        send_parameters = (request_form.get("parameters") or "").strip()
        if send_parameters:
            values = [x.strip() for x in send_parameters.split("||")]
        else:
            raw_parameters = campaign["parameters"] or ""
            values = raw_parameters.split("||") if raw_parameters else []

        expected_count = int(selected.get("variable_count", 0) or 0)
        if expected_count and (len(values) != expected_count or any(not x.strip() for x in values)):
            return campaign, selected, None, None, None, f"Before sending, please fill all {expected_count} template variable(s)."

        header_media_id = None
        header_media_type = selected.get("header_media_type", "")
        if selected.get("has_media_header"):
            uploaded_media = request_files.get("header_media")
            media_path = None
            if not uploaded_media and campaign.get("header_media_id"):
                header_media_id = str(campaign["header_media_id"])
                return campaign, selected, values, header_media_id, header_media_type, None
            if uploaded_media and uploaded_media.filename:
                filename = secure_filename(uploaded_media.filename)
                ext = os.path.splitext(filename)[1].lower()
                allowed = {
                    "image": {".jpg", ".jpeg", ".png"},
                    "video": {".mp4", ".3gp"},
                    "document": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"}
                }
                if not filename or ext not in allowed.get(header_media_type, set()):
                    return campaign, selected, None, None, None, f"Please upload a valid {header_media_type.upper()} file before sending."
                upload_dir = os.path.join("static", "uploads", "template_headers")
                os.makedirs(upload_dir, exist_ok=True)
                media_path = os.path.join(upload_dir, f"{secrets.token_hex(8)}_{filename}")
                uploaded_media.save(media_path)
            else:
                media_path = campaign["header_media_path"] or campaign["header_image_path"]
                if not media_path or not os.path.exists(media_path):
                    return campaign, selected, None, None, None, f"Please upload the required {header_media_type.upper()} header media before sending."

            header_media_id, media_error = upload_whatsapp_media(media_path, header_media_type)
            if not header_media_id:
                return campaign, selected, None, None, None, f"Template header media upload failed: {media_error}"

            c.execute(
                "UPDATE template_campaigns SET header_media_path = ?, header_image_path = ?, header_media_type = ?, header_media_id = ?, parameters = ? WHERE id = ?",
                (media_path, media_path if header_media_type == "image" else campaign["header_image_path"], header_media_type, header_media_id, send_parameters or campaign["parameters"], cid)
            )
        elif send_parameters:
            c.execute("UPDATE template_campaigns SET parameters = ? WHERE id = ?", (send_parameters, cid))

        c.commit()
        campaign = c.execute("SELECT * FROM template_campaigns WHERE id = ?", (cid,)).fetchone()
        return campaign, selected, values, header_media_id, header_media_type, None
    except Exception as e:
        c.rollback()
        return None, None, None, None, None, f"Template campaign error: {e}"
    finally:
        c.close()


def perform_template_campaign_send(cid):
    """Send a saved campaign. Used by both Send Now and the persistent scheduler."""
    campaign, selected, values, header_media_id, header_media_type, error = _prepare_template_campaign_send(cid)
    if error:
        c = db()
        try:
            c.execute("UPDATE template_campaigns SET status = ? WHERE id = ?", ("Failed", cid))
            c.commit()
        finally:
            c.close()
        return False, error

    c = db()
    try:
        # A campaign can be claimed by only one scheduler worker.
        c.execute("UPDATE template_campaigns SET status = ? WHERE id = ?", ("Sending", cid))
        c.commit()

        recipients = template_campaign_recipients(campaign)
        if not recipients:
            c.execute("UPDATE template_campaigns SET status = ? WHERE id = ?", ("Failed", cid))
            c.commit()
            return False, "No recipients found."

        expected_count = int(selected.get("variable_count", 0) or 0)
        sent = 0
        failed = 0
        for contact in recipients:
            phone = clean_phone(contact.get("phone") or "")
            if not phone:
                failed += 1
                continue
            ok, message_id, response = send_whatsapp_template(
                phone,
                campaign["template_name"],
                campaign["template_language"],
                values if expected_count else None,
                header_media_id=header_media_id,
                header_media_type=header_media_type
            )
            if ok:
                status = "accepted"
                error_text = None
                sent += 1
            else:
                status = "failed"
                failed += 1
                error_text = json.dumps(response, ensure_ascii=False) if isinstance(response, (dict, list)) else str(response)

            c.execute("""
                INSERT INTO whatsapp_messages
                (campaign_id, contact_id, phone, message, wa_message_id, direction, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (None, contact.get("id"), phone, "[Template] " + campaign["template_name"], message_id, "outgoing", status, error_text))

        c.execute("UPDATE template_campaigns SET status = ?, scheduled_at = scheduled_at WHERE id = ?", ("Completed", cid))
        c.commit()
        return True, f"Template campaign completed: {sent} accepted, {failed} failed."
    except Exception as e:
        c.rollback()
        try:
            c.execute("UPDATE template_campaigns SET status = ? WHERE id = ?", ("Failed", cid))
            c.commit()
        except Exception:
            c.rollback()
        return False, f"Template campaign error: {e}"
    finally:
        c.close()


@app.route("/template-campaign/<int:cid>/send", methods=["POST"])
def send_template_campaign(cid):
    mode = (request.form.get("send_mode") or "now").strip().lower()

    if mode == "schedule":
        scheduled_raw = (request.form.get("scheduled_at") or "").strip()
        if not scheduled_raw:
            flash("Please select a date and time for scheduled send.")
            return redirect(url_for("template_campaigns"))
        try:
            # datetime-local is interpreted as India Standard Time (IST).
            ist = ZoneInfo("Asia/Kolkata")
            local_dt = datetime.strptime(scheduled_raw, "%Y-%m-%dT%H:%M").replace(tzinfo=ist)
            if local_dt <= datetime.now(ist):
                flash("Scheduled date and time must be in the future.")
                return redirect(url_for("template_campaigns"))
            scheduled_utc = local_dt.astimezone(timezone.utc)
        except Exception:
            flash("Invalid scheduled date/time.")
            return redirect(url_for("template_campaigns"))

        campaign, selected, values, media_id, media_type, error = _prepare_template_campaign_send(
            cid, request.form, request.files
        )
        if error:
            flash(error)
            return redirect(url_for("template_campaigns"))

        c = db()
        try:
            c.execute("UPDATE template_campaigns SET status = ?, scheduled_at = ? WHERE id = ?", ("Scheduled", scheduled_utc, cid))
            c.commit()
            flash("Campaign scheduled successfully. It will send automatically at the selected IST time.")
        except Exception as e:
            c.rollback()
            flash(f"Schedule error: {e}")
        finally:
            c.close()
        return redirect(url_for("template_campaigns"))

    ok, message = perform_template_campaign_send(cid)
    flash(message)
    return redirect(url_for("template_campaigns"))


# ---------------------------------------------------------
# PERSISTENT TEMPLATE CAMPAIGN SCHEDULER
# ---------------------------------------------------------

def process_due_template_campaigns():
    """Claim and send due campaigns. DB-backed so a restart does not lose schedules."""
    while True:
        try:
            c = db()
            try:
                row = c.execute("""
                    SELECT id FROM template_campaigns
                    WHERE status = 'Scheduled'
                      AND scheduled_at IS NOT NULL
                      AND scheduled_at <= NOW()
                    ORDER BY scheduled_at ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """).fetchone()
                if row:
                    c.execute("UPDATE template_campaigns SET status = ? WHERE id = ?", ("Sending", row["id"]))
                    c.commit()
                    cid = row["id"]
                else:
                    c.commit()
                    cid = None
            finally:
                c.close()

            if cid is not None:
                # perform_template_campaign_send will keep the status Sending and finish it.
                perform_template_campaign_send(cid)
            else:
                time.sleep(20)
        except Exception as e:
            print(f"Template scheduler error: {e}")
            time.sleep(30)


def process_due_template_campaigns_once():
    """Process all campaigns currently due. Intended for Render Cron Jobs."""
    processed = []
    while True:
        c = None
        try:
            c = db()
            row = c.execute("""
                SELECT id FROM template_campaigns
                WHERE status = 'Scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= NOW()
                ORDER BY scheduled_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """).fetchone()
            if not row:
                c.commit(); break
            cid = row["id"]
            c.execute("UPDATE template_campaigns SET status = ? WHERE id = ? AND status = 'Scheduled'", ("Sending", cid))
            c.commit()
        except Exception as e:
            if c:
                try: c.rollback()
                except Exception: pass
            print(f"Scheduled campaign claim error: {e}"); break
        finally:
            if c:
                c.close()
        ok, message = perform_template_campaign_send(cid)
        processed.append({"id": cid, "ok": ok, "message": message})
    return processed


def start_template_scheduler():
    t = threading.Thread(target=process_due_template_campaigns, name="template-campaign-scheduler", daemon=True)
    t.start()


@app.route("/api/template-campaigns/scheduled")
def scheduled_template_campaigns_api():
    c = db()
    try:
        rows = c.execute("""
            SELECT id, name, status, scheduled_at
            FROM template_campaigns
            WHERE scheduled_at IS NOT NULL
            ORDER BY scheduled_at DESC
            LIMIT 100
        """).fetchall()
        return jsonify({"campaigns": [dict(r) for r in rows]})
    finally:
        c.close()


@app.route("/internal/run-scheduled-campaigns", methods=["POST", "GET"])
def run_scheduled_campaigns_internal():
    """Authenticated endpoint for an external scheduler such as Render Cron."""
    supplied = request.headers.get("X-Scheduler-Secret", "") or request.args.get("secret", "")
    expected = os.getenv("SCHEDULER_SECRET", "").strip()
    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    results = process_due_template_campaigns_once()
    return jsonify({"ok": True, "processed": results})


start_template_scheduler()


if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
