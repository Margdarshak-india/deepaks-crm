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
import secrets
import re

from datetime import datetime
from urllib.parse import urlencode


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
    parameters=None
):

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

    template = {
        "name": template_name,
        "language": {
            "code": language_code
        }
    }

    if parameters:

        template["components"] = [
            {
                "type": "body",
                "parameters": [
                    {
                        "type": "text",
                        "text": str(value)
                    }
                    for value in parameters
                ]
            }
        ]

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
# GOOGLE DRIVE HELPERS
# =========================================================

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
        recent=recent,
        whatsapp_ok=whatsapp_configured(),
        google_connected=bool(google_token())
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
        "fields": "name,language,status,category",
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
                "category": item.get("category")
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

def fetch_meta_templates():

    token = get_env("WHATSAPP_ACCESS_TOKEN")
    waba_id = (
        get_env("WHATSAPP_BUSINESS_ACCOUNT_ID")
        or get_env("WHATSAPP_WABA_ID")
    )

    if not token or not waba_id:
        return [], "WHATSAPP_ACCESS_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID are required."

    url = f"https://graph.facebook.com/v23.0/{waba_id}/message_templates"

    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "fields": "name,language,status,category,components",
                "limit": 100
            },
            timeout=30
        )
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        if not r.ok:
            return [], json.dumps(data, ensure_ascii=False)

        result = []
        import re

        for x in data.get("data", []):
            components = x.get("components") or []
            body_text = ""

            for component in components:
                if str(component.get("type", "")).upper() == "BODY":
                    body_text = component.get("text") or ""
                    break

            variable_numbers = []
            for match in re.findall(r"\{\{\s*(\d+)\s*\}\}", body_text):
                number = int(match)
                if number not in variable_numbers:
                    variable_numbers.append(number)
            variable_numbers.sort()

            result.append({
                "name": x.get("name", ""),
                "language": x.get("language", "en_US"),
                "status": x.get("status", "UNKNOWN"),
                "category": x.get("category", ""),
                "components": components,
                "body_text": body_text,
                "variables": variable_numbers,
                "variable_count": len(variable_numbers)
            })

        return result, None

    except Exception as e:
        return [], str(e)


def template_campaign_recipients(campaign):
    if campaign["target_type"] == "single":
        phone = clean_phone(campaign["manual_number"] or "")
        return [{"id": None, "name": "Customer", "phone": phone}] if phone else []

    c = db()
    try:
        if campaign["target_type"] == "all":
            return c.execute("SELECT * FROM contacts ORDER BY id ASC").fetchall()

        return c.execute(
            "SELECT * FROM contacts WHERE group_name = ? ORDER BY id ASC",
            (campaign["group_name"],)
        ).fetchall()
    finally:
        c.close()


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
        parameters = request.form.get("parameters", "").strip()

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

        # Validate that the campaign contains exactly the variables
        # required by the selected Meta template.
        parameter_values = [
            value.strip()
            for value in parameters.split("||")
            if value.strip()
        ]
        required_variables = int(selected.get("variable_count", 0) or 0)
        if len(parameter_values) != required_variables:
            flash(
                f"Template '{template_name}' requires exactly "
                f"{required_variables} variable(s), but "
                f"{len(parameter_values)} value(s) were provided."
            )
            return redirect(url_for("template_campaigns"))

        if target_type == "group" and not group_name:
            flash("Please select a contact group.")
            return redirect(url_for("template_campaigns"))

        if target_type == "single" and not manual_number:
            flash("Please enter a WhatsApp number.")
            return redirect(url_for("template_campaigns"))

        c = db()
        try:
            c.execute("""
                INSERT INTO template_campaigns
                (name, template_name, template_language, target_type,
                 group_name, manual_number, parameters, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, template_name, template_language, target_type,
                group_name or None, manual_number or None,
                parameters or None, "Approved"
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
    finally:
        c.close()

    status_map = {
        (x["name"], x["language"]): x["status"]
        for x in templates
    }

    for row in campaigns_list:
        status = status_map.get(
            (row["template_name"], row["template_language"])
        )
        if status:
            row["status"] = (
                "Approved" if status.upper() == "APPROVED"
                else status.title()
            )

    # Always open Meta's WhatsApp Message Templates manager directly.
    # This intentionally does not fall back to the Business Manager home page.
    meta_template_url = "https://business.facebook.com/latest/whatsapp_manager/message_templates"

    return render_template(
        "template_campaigns.html",
        campaigns=campaigns_list,
        templates=templates,
        groups=groups,
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


@app.route("/template-campaign/<int:cid>/send", methods=["POST"])
def send_template_campaign(cid):

    c = db()

    try:
        campaign = c.execute(
            "SELECT * FROM template_campaigns WHERE id = ?",
            (cid,)
        ).fetchone()

        if not campaign:
            flash("Template campaign not found.")
            return redirect(url_for("template_campaigns"))

        templates, error = fetch_meta_templates()

        if error:
            flash(f"Meta template check failed: {error}")
            return redirect(url_for("template_campaigns"))

        selected = next(
            (
                x for x in templates
                if x["name"] == campaign["template_name"]
                and x["language"] == campaign["template_language"]
            ),
            None
        )

        if not selected or selected["status"].upper() != "APPROVED":
            flash("Template is not approved. Sending is disabled.")
            return redirect(url_for("template_campaigns"))

        recipients = template_campaign_recipients(campaign)

        if not recipients:
            flash("No recipients found.")
            return redirect(url_for("template_campaigns"))

        values = [
            x.strip()
            for x in (campaign["parameters"] or "").split("||")
            if x.strip()
        ]

        expected_count = int(selected.get("variable_count", 0) or 0)

        if len(values) != expected_count:
            flash(
                f"Template '{campaign['template_name']}' requires "
                f"{expected_count} variable(s), but {len(values)} value(s) were provided."
            )
            return redirect(url_for("template_campaigns"))

        sent = 0
        failed = 0

        for contact in recipients:

            phone = clean_phone(contact.get("phone") or "")
            if not phone:
                failed += 1
                continue

            send_values = values[:]

            ok, message_id, response = send_whatsapp_template(
                phone,
                campaign["template_name"],
                campaign["template_language"],
                send_values
            )

            if ok:
                status = "accepted"
                error_text = None
                sent += 1
            else:
                status = "failed"
                failed += 1
                error_text = (
                    json.dumps(response, ensure_ascii=False)
                    if isinstance(response, (dict, list))
                    else str(response)
                )

            c.execute("""
                INSERT INTO whatsapp_messages
                (campaign_id, contact_id, phone, message,
                 wa_message_id, direction, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                None,
                contact.get("id"),
                phone,
                "[Template] " + campaign["template_name"],
                message_id,
                "outgoing",
                status,
                error_text
            ))

        c.execute(
            "UPDATE template_campaigns SET status = ? WHERE id = ?",
            (f"Sent {sent}, Failed {failed}", cid)
        )

        c.commit()

        flash(
            f"Template campaign finished: "
            f"{sent} accepted, {failed} failed."
        )

    except Exception as e:
        c.rollback()
        flash(f"Template campaign error: {e}")

    finally:
        c.close()

    return redirect(url_for("template_campaigns"))


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
