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
import csv
import io
import os
import requests
import hmac
import hashlib
import json
import secrets
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
# DATABASE WRAPPER
# =========================================================

class DBWrapper:

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):

        # Convert SQLite-style ? placeholders
        # to PostgreSQL %s placeholders.
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # -------------------------------------------------
        # ADD NEW CAMPAIGN COLUMNS
        # FOR OLD DATABASES
        # -------------------------------------------------

        c.execute("""
            ALTER TABLE campaigns
            ADD COLUMN IF NOT EXISTS target_type TEXT DEFAULT 'group'
        """)

        c.execute("""
            ALTER TABLE campaigns
            ADD COLUMN IF NOT EXISTS target_phone TEXT
        """)

        c.execute("""
            ALTER TABLE campaigns
            ADD COLUMN IF NOT EXISTS drive_file_id TEXT
        """)

        c.execute("""
            ALTER TABLE campaigns
            ADD COLUMN IF NOT EXISTS drive_file_name TEXT
        """)

        c.execute("""
            ALTER TABLE campaigns
            ADD COLUMN IF NOT EXISTS drive_mime_type TEXT
        """)

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
        # INCOMING WHATSAPP
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
    phone = phone.replace(".", "")

    if phone.startswith("00"):
        phone = phone[2:]

    # Keep digits only
    phone = "".join(
        ch for ch in phone
        if ch.isdigit()
    )

    return phone


# =========================================================
# WHATSAPP CONFIGURATION
# =========================================================

def whatsapp_configured():

    return bool(
        get_env("WHATSAPP_ACCESS_TOKEN")
        and get_env("WHATSAPP_PHONE_NUMBER_ID")
    )


def whatsapp_graph_version():

    return (
        get_env("WHATSAPP_GRAPH_VERSION")
        or "v23.0"
    )


def whatsapp_messages_url():

    phone_id = get_env(
        "WHATSAPP_PHONE_NUMBER_ID"
    )

    if not phone_id:
        return None

    return (
        f"https://graph.facebook.com/"
        f"{whatsapp_graph_version()}/"
        f"{phone_id}/messages"
    )


def whatsapp_media_url():

    phone_id = get_env(
        "WHATSAPP_PHONE_NUMBER_ID"
    )

    if not phone_id:
        return None

    return (
        f"https://graph.facebook.com/"
        f"{whatsapp_graph_version()}/"
        f"{phone_id}/media"
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
# UPLOAD MEDIA TO WHATSAPP
# =========================================================

def upload_media_to_whatsapp(
    file_bytes,
    mime_type,
    filename
):

    token = get_env(
        "WHATSAPP_ACCESS_TOKEN"
    )

    if not token:

        return (
            False,
            None,
            "WhatsApp access token missing"
        )

    url = whatsapp_media_url()

    headers = {
        "Authorization": f"Bearer {token}"
    }

    files = {
        "file": (
            filename or "attachment",
            file_bytes,
            mime_type or "application/octet-stream"
        )
    }

    data = {
        "messaging_product": "whatsapp",
        "type": (
            mime_type
            or "application/octet-stream"
        )
    }

    try:

        r = requests.post(
            url,
            headers=headers,
            files=files,
            data=data,
            timeout=60
        )

        try:
            response = r.json()
        except Exception:
            response = r.text

        if not r.ok:

            return (
                False,
                None,
                response
            )

        media_id = None

        if isinstance(response, dict):

            media_id = response.get(
                "id"
            )

        if not media_id:

            return (
                False,
                None,
                "WhatsApp media ID not returned."
            )

        return (
            True,
            media_id,
            response
        )

    except Exception as e:

        return (
            False,
            None,
            str(e)
        )


# =========================================================
# SEND WHATSAPP MEDIA
# =========================================================

def send_whatsapp_media(
    phone,
    file_bytes,
    mime_type,
    filename,
    caption=""
):

    phone = clean_phone(phone)

    if not phone:

        return (
            False,
            None,
            "Invalid phone number"
        )

    # Upload file first
    uploaded, media_id, upload_result = (
        upload_media_to_whatsapp(
            file_bytes,
            mime_type,
            filename
        )
    )

    if not uploaded:

        return (
            False,
            None,
            upload_result
        )

    mime_type = (
        mime_type
        or "application/octet-stream"
    ).lower()

    # Determine WhatsApp media type
    if mime_type.startswith("image/"):

        media_type = "image"

    elif mime_type.startswith("video/"):

        media_type = "video"

    elif mime_type.startswith("audio/"):

        media_type = "audio"

    else:

        media_type = "document"

    media_object = {
        "id": media_id
    }

    if caption and media_type != "audio":

        media_object["caption"] = caption

    if media_type == "document" and filename:

        media_object["filename"] = filename

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": media_type,
        media_type: media_object
    }

    token = get_env(
        "WHATSAPP_ACCESS_TOKEN"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:

        r = requests.post(
            whatsapp_messages_url(),
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
# WHATSAPP TEMPLATE
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

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:

        r = requests.post(
            whatsapp_messages_url(),
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

    if not signature.startswith(
        "sha256="
    ):

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
# GOOGLE DRIVE CONFIGURATION
# =========================================================

GOOGLE_CLIENT_ID = get_env(
    "GOOGLE_CLIENT_ID"
)

GOOGLE_CLIENT_SECRET = get_env(
    "GOOGLE_CLIENT_SECRET"
)

GOOGLE_REDIRECT_URI = (
    get_env("GOOGLE_REDIRECT_URI")
    or
    "https://deepaks-crm-1.onrender.com/"
    "google/oauth/callback"
)

GOOGLE_OAUTH_SCOPE = (
    "https://www.googleapis.com/auth/"
    "drive.readonly"
)

GOOGLE_AUTH_URL = (
    "https://accounts.google.com/"
    "o/oauth2/v2/auth"
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
        and GOOGLE_CLIENT_SECRET
        and GOOGLE_REDIRECT_URI
    )


def google_token():

    return session.get(
        "google_access_token"
    )


# =========================================================
# GOOGLE DRIVE TOKEN REFRESH
# =========================================================

def refresh_google_token():

    refresh_token = session.get(
        "google_refresh_token"
    )

    if not refresh_token:

        return False

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:

        return False

    payload = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }

    try:

        r = requests.post(
            GOOGLE_TOKEN_URL,
            data=payload,
            timeout=30
        )

        if not r.ok:

            return False

        data = r.json()

        access_token = data.get(
            "access_token"
        )

        if not access_token:

            return False

        session[
            "google_access_token"
        ] = access_token

        return True

    except Exception:

        return False


# =========================================================
# GOOGLE DRIVE HEADERS
# =========================================================

def google_headers():

    token = google_token()

    if not token:

        return None

    return {
        "Authorization": f"Bearer {token}"
    }


# =========================================================
# GOOGLE DRIVE REQUEST
# =========================================================

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

        # Try refresh if expired
        if r.status_code == 401:

            if refresh_google_token():

                headers = google_headers()

                r = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=30
                )

            else:

                session.pop(
                    "google_access_token",
                    None
                )

                return (
                    None,
                    "Google Drive session expired. "
                    "Connect Google Drive again."
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

        return (
            None,
            data
        )

    except Exception as e:

        return (
            None,
            str(e)
        )


# =========================================================
# GOOGLE DRIVE FILE LIST
# =========================================================

def drive_files_list():

    params = {
        "pageSize": 100,
        "orderBy": "modifiedTime desc",
        "fields": (
            "files(id,name,mimeType,size,"
            "modifiedTime,webViewLink)"
        ),
        "q": (
            "trashed = false and "
            "("
            "mimeType = 'text/csv' or "
            "mimeType = 'application/pdf' or "
            "mimeType = 'image/jpeg' or "
            "mimeType = 'image/png' or "
            "mimeType = 'image/webp' or "
            "mimeType = 'video/mp4' or "
            "mimeType = 'application/vnd.google-apps.spreadsheet' or "
            "mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' or "
            "mimeType = 'application/msword' or "
            "mimeType = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'"
            ")"
        )
    }

    return drive_request(
        f"{GOOGLE_DRIVE_API}/files",
        params=params
    )


# =========================================================
# DOWNLOAD GOOGLE DRIVE FILE
# =========================================================

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
                f"{GOOGLE_DRIVE_API}/files/"
                f"{file_id}/export"
            )

            params = {
                "mimeType": "text/csv"
            }

        else:

            url = (
                f"{GOOGLE_DRIVE_API}/files/"
                f"{file_id}"
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

        # Refresh and retry
        if r.status_code == 401:

            if refresh_google_token():

                headers = google_headers()

                r = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=60
                )

            else:

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
# IMPORT CSV TEXT
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

            name = ""

            name_columns = [
                "Student Name",
                "student name",
                "STUDENT NAME",
                "Name",
                "name",
                "Full Name",
                "full name",
                "Student",
                "student",
                "Customer Name",
                "customer name"
            ]

            for column in name_columns:

                value = row.get(column)

                if value and str(value).strip():

                    name = str(
                        value
                    ).strip()

                    break

            if not name:

                name = "Unknown"

            phone = ""

            phone_columns = [
                "Mobile",
                "mobile",
                "MOBILE",
                "Mobile Number",
                "mobile number",
                "Phone",
                "phone",
                "Phone Number",
                "phone number",
                "WhatsApp",
                "whatsapp",
                "WhatsApp Number",
                "whatsapp number"
            ]

            for column in phone_columns:

                value = row.get(column)

                if value and str(value).strip():

                    phone = str(
                        value
                    ).strip()

                    break

            phone = clean_phone(phone)

            group = ""

            group_columns = [
                "Group",
                "group",
                "GROUP",
                "Group Name",
                "group name"
            ]

            for column in group_columns:

                value = row.get(column)

                if value and str(value).strip():

                    group = str(
                        value
                    ).strip()

                    break

            if not group:

                group = "General"

            if not phone:

                skipped += 1
                continue

            # PostgreSQL safe duplicate handling
            cursor = c.execute("""
                INSERT INTO contacts
                (
                    name,
                    phone,
                    group_name
                )
                VALUES (?, ?, ?)
                ON CONFLICT (phone)
                DO NOTHING
            """, (
                name,
                phone,
                group
            ))

            if cursor.rowcount == 1:

                added += 1

            else:

                skipped += 1

        c.commit()

        return (
            added,
            f"{skipped} rows skipped."
        )

    except Exception as e:

        c.rollback()

        return (
            0,
            f"Import error: {str(e)}"
        )

    finally:

        c.close()


# =========================================================
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():

    c = db()

    try:

        contacts = c.execute("""
            SELECT COUNT(*) AS n
            FROM contacts
        """).fetchone()["n"]

        campaigns = c.execute("""
            SELECT COUNT(*) AS n
            FROM campaigns
        """).fetchone()["n"]

        sent = c.execute("""
            SELECT COUNT(*) AS n
            FROM whatsapp_messages
            WHERE status IN
            (
                'sent',
                'delivered',
                'read',
                'accepted'
            )
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

    finally:

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

@app.route(
    "/contacts",
    methods=["GET", "POST"]
)
def contacts():

    if request.method == "POST":

        f = request.files.get("file")

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

        except Exception as e:

            flash(
                f"CSV read error: {str(e)}"
            )

            return redirect(
                url_for("contacts")
            )

        reader = csv.DictReader(
            io.StringIO(text)
        )

        if not reader.fieldnames:

            flash(
                "CSV header not found."
            )

            return redirect(
                url_for("contacts")
            )

        c = db()

        added = 0
        skipped = 0

        try:

            for row in reader:

                # -----------------------------------------
                # NAME
                # -----------------------------------------

                name = ""

                name_columns = [
                    "Student Name",
                    "student name",
                    "STUDENT NAME",
                    "Name",
                    "name",
                    "Full Name",
                    "full name",
                    "Student",
                    "student",
                    "Customer Name",
                    "customer name"
                ]

                for column in name_columns:

                    value = row.get(column)

                    if value and str(value).strip():

                        name = str(
                            value
                        ).strip()

                        break

                if not name:

                    name = "Unknown"

                # -----------------------------------------
                # PHONE
                # -----------------------------------------

                phone = ""

                phone_columns = [
                    "Mobile",
                    "mobile",
                    "MOBILE",
                    "Mobile Number",
                    "mobile number",
                    "Phone",
                    "phone",
                    "Phone Number",
                    "phone number",
                    "WhatsApp",
                    "whatsapp",
                    "WhatsApp Number",
                    "whatsapp number"
                ]

                for column in phone_columns:

                    value = row.get(column)

                    if value and str(value).strip():

                        phone = str(
                            value
                        ).strip()

                        break

                phone = clean_phone(phone)

                # -----------------------------------------
                # GROUP
                # -----------------------------------------

                group = ""

                group_columns = [
                    "Group",
                    "group",
                    "GROUP",
                    "Group Name",
                    "group name"
                ]

                for column in group_columns:

                    value = row.get(column)

                    if value and str(value).strip():

                        group = str(
                            value
                        ).strip()

                        break

                if not group:

                    group = "General"

                # -----------------------------------------
                # EMPTY PHONE
                # -----------------------------------------

                if not phone:

                    skipped += 1
                    continue

                # -----------------------------------------
                # INSERT
                # -----------------------------------------

                cursor = c.execute("""
                    INSERT INTO contacts
                    (
                        name,
                        phone,
                        group_name
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT (phone)
                    DO NOTHING
                """, (
                    name,
                    phone,
                    group
                ))

                if cursor.rowcount == 1:

                    added += 1

                else:

                    skipped += 1

            c.commit()

            flash(
                f"{added} contacts imported successfully. "
                f"{skipped} skipped."
            )

        except Exception as e:

            c.rollback()

            flash(
                f"Import error: {str(e)}"
            )

        finally:

            c.close()

        return redirect(
            url_for("contacts")
        )

    # =====================================================
    # SHOW CONTACTS
    # =====================================================

    c = db()

    try:

        rows = c.execute("""
            SELECT *
            FROM contacts
            ORDER BY id DESC
        """).fetchall()

    finally:

        c.close()

    return render_template(
        "contacts.html",
        rows=rows
    )


# =========================================================
# GOOGLE DRIVE PAGE
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
            connected=False,
            files=[],
            error=error
        )

    files = []

    if isinstance(data, dict):

        files = data.get(
            "files",
            []
        )

    return render_template(
        "google_drive.html",
        connected=True,
        files=files,
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
            "GOOGLE_CLIENT_SECRET Render में add करें."
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
            f"Google authorization error: {error}"
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
        or not saved_state
        or state != saved_state
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

            return jsonify({
                "status": "error",
                "message": (
                    "Google token exchange failed"
                ),
                "details": data
            }), 400

        access_token = data.get(
            "access_token"
        )

        if not access_token:

            return jsonify({
                "status": "error",
                "message": (
                    "Google access token missing."
                )
            }), 400

        session[
            "google_access_token"
        ] = access_token

        refresh_token = data.get(
            "refresh_token"
        )

        if refresh_token:

            session[
                "google_refresh_token"
            ] = refresh_token

        flash(
            "Google Drive connected successfully."
        )

        return redirect(
            url_for("google_drive")
        )

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


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
# GOOGLE DRIVE CSV IMPORT
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
    ).strip()

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
# DELETE SINGLE CONTACT
# =========================================================

@app.route(
    "/contacts/delete/<int:contact_id>",
    methods=["POST"]
)
def delete_contact(contact_id):

    c = db()

    try:

        contact = c.execute("""
            SELECT name
            FROM contacts
            WHERE id=?
        """, (
            contact_id,
        )).fetchone()

        if not contact:

            flash(
                "Contact नहीं मिला."
            )

            return redirect(
                url_for("contacts")
            )

        c.execute("""
            DELETE FROM contacts
            WHERE id=?
        """, (
            contact_id,
        ))

        c.commit()

        flash(
            f"Contact '{contact['name']}' "
            f"deleted successfully."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Contact delete error: {str(e)}"
        )

    finally:

        c.close()

    return redirect(
        url_for("contacts")
    )


# =========================================================
# DELETE SELECTED CONTACTS
# =========================================================

@app.route(
    "/contacts/delete-selected",
    methods=["POST"]
)
def delete_selected_contacts():

    ids = request.form.getlist(
        "contact_ids"
    )

    if not ids:

        flash(
            "No contacts selected."
        )

        return redirect(
            url_for("contacts")
        )

    # Convert IDs safely
    safe_ids = []

    for value in ids:

        try:

            safe_ids.append(
                int(value)
            )

        except Exception:

            pass

    if not safe_ids:

        flash(
            "No valid contacts selected."
        )

        return redirect(
            url_for("contacts")
        )

    c = db()

    try:

        cursor = c.execute("""
            DELETE FROM contacts
            WHERE id = ANY(%s)
        """, (
            safe_ids,
        ))

        deleted = cursor.rowcount

        c.commit()

        flash(
            f"{deleted} selected contacts "
            f"deleted successfully."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Delete error: {str(e)}"
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

        cursor = c.execute(
            "DELETE FROM contacts"
        )

        deleted = cursor.rowcount

        c.commit()

        flash(
            f"{deleted} contacts deleted successfully."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Delete error: {str(e)}"
        )

    finally:

        c.close()

    return redirect(
        url_for("contacts")
    )


# =========================================================
# CAMPAIGNS
# =========================================================

@app.route(
    "/campaigns",
    methods=["GET", "POST"]
)
def campaigns():

    # =====================================================
    # POST - CREATE CAMPAIGN
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

        campaign_type = request.form.get(
            "campaign_type",
            "group"
        ).strip().lower()

        group_name = request.form.get(
            "group_name",
            ""
        ).strip()

        target_mode = request.form.get(
            "target_mode",
            "saved"
        ).strip().lower()

        saved_number = request.form.get(
            "single_number",
            ""
        ).strip()

        manual_number = request.form.get(
            "manual_number",
            ""
        ).strip()

        drive_file_id = request.form.get(
            "drive_file_id",
            ""
        ).strip()

        drive_file_name = request.form.get(
            "drive_file_name",
            ""
        ).strip()

        drive_mime_type = request.form.get(
            "drive_mime_type",
            ""
        ).strip()

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

        if not message and not drive_file_id:

            flash(
                "Message या attachment में से "
                "कम से कम एक देना जरूरी है."
            )

            return redirect(
                url_for("campaigns")
            )

        # -------------------------------------------------
        # SINGLE NUMBER
        # -------------------------------------------------

        if campaign_type == "single":

            target_phone = ""

            # Saved contact
            if target_mode == "saved":

                target_phone = clean_phone(
                    saved_number
                )

            # Manual entry
            elif target_mode == "manual":

                target_phone = clean_phone(
                    manual_number
                )

            else:

                flash(
                    "Invalid single contact selection."
                )

                return redirect(
                    url_for("campaigns")
                )

            if not target_phone:

                flash(
                    "Please select a saved contact "
                    "or enter a valid WhatsApp number."
                )

                return redirect(
                    url_for("campaigns")
                )

            target_type = "single"

            # Keep old field compatible
            group_name = (
                "__SINGLE__:"
                + target_phone
            )

        # -------------------------------------------------
        # GROUP
        # -------------------------------------------------

        else:

            target_type = "group"

            target_phone = None

            if not group_name:

                flash(
                    "Please select a contact group."
                )

                return redirect(
                    url_for("campaigns")
                )

        # -------------------------------------------------
        # SAVE CAMPAIGN
        # -------------------------------------------------

        c = db()

        try:

            c.execute("""
                INSERT INTO campaigns
                (
                    name,
                    message,
                    group_name,
                    status,
                    target_type,
                    target_phone,
                    drive_file_id,
                    drive_file_name,
                    drive_mime_type
                )
                VALUES
                (
                    ?, ?, ?, 'Draft',
                    ?, ?, ?, ?, ?
                )
            """, (
                name,
                message,
                group_name,
                target_type,
                target_phone,
                drive_file_id or None,
                drive_file_name or None,
                drive_mime_type or None
            ))

            c.commit()

            flash(
                "Campaign saved as Draft."
            )

        except Exception as e:

            c.rollback()

            flash(
                f"Campaign error: {str(e)}"
            )

        finally:

            c.close()

        return redirect(
            url_for("campaigns")
        )

    # =====================================================
    # GET - CAMPAIGNS PAGE
    # =====================================================

    c = db()

    try:

        # -------------------------------------------------
        # CAMPAIGNS
        # -------------------------------------------------

        rows = c.execute("""
            SELECT *
            FROM campaigns
            ORDER BY id DESC
        """).fetchall()

        # -------------------------------------------------
        # GROUPS
        # -------------------------------------------------

        groups_rows = c.execute("""
            SELECT DISTINCT group_name
            FROM contacts
            WHERE group_name IS NOT NULL
              AND TRIM(group_name) != ''
            ORDER BY group_name
        """).fetchall()

        groups = [
            row["group_name"]
            for row in groups_rows
        ]

        # -------------------------------------------------
        # CONTACTS
        # -------------------------------------------------

        contacts = c.execute("""
            SELECT
                id,
                name,
                phone
            FROM contacts
            WHERE phone IS NOT NULL
              AND TRIM(phone) != ''
            ORDER BY name
        """).fetchall()

    finally:

        c.close()

    # -----------------------------------------------------
    # GOOGLE DRIVE FILES
    # -----------------------------------------------------

    drive_files = []
    drive_error = None
    google_connected = bool(
        google_token()
    )

    if google_connected:

        data, error = drive_files_list()

        if error:

            drive_error = error

        elif isinstance(data, dict):

            drive_files = data.get(
                "files",
                []
            )

    return render_template(
        "campaigns.html",
        rows=rows,
        groups=groups,
        contacts=contacts,
        drive_files=drive_files,
        drive_error=drive_error,
        google_connected=google_connected
    )


# =========================================================
# SEND CAMPAIGN
# =========================================================

@app.route(
    "/campaign/<int:cid>/send",
    methods=["POST"]
)
def send_campaign(cid):

    c = db()

    try:

        # -------------------------------------------------
        # GET CAMPAIGN
        # -------------------------------------------------

        campaign = c.execute("""
            SELECT *
            FROM campaigns
            WHERE id=?
        """, (
            cid,
        )).fetchone()

        if not campaign:

            flash(
                "Campaign not found."
            )

            return redirect(
                url_for("campaigns")
            )

        message = (
            campaign["message"]
            or
            ""
        )

        target_type = (
            campaign.get(
                "target_type"
            )
            or
            "group"
        )

        target_phone = clean_phone(
            campaign.get(
                "target_phone"
            )
            or
            ""
        )

        group_name = (
            campaign.get(
                "group_name"
            )
            or
            ""
        )

        drive_file_id = (
            campaign.get(
                "drive_file_id"
            )
            or
            ""
        )

        drive_file_name = (
            campaign.get(
                "drive_file_name"
            )
            or
            ""
        )

        drive_mime_type = (
            campaign.get(
                "drive_mime_type"
            )
            or
            ""
        )

        # -------------------------------------------------
        # OLD SINGLE CAMPAIGN COMPATIBILITY
        # -------------------------------------------------

        if (
            target_type == "group"
            and
            group_name.startswith(
                "__SINGLE__:"
            )
        ):

            target_type = "single"

            target_phone = clean_phone(
                group_name.replace(
                    "__SINGLE__:",
                    "",
                    1
                )
            )

        # -------------------------------------------------
        # CHECK WHATSAPP
        # -------------------------------------------------

        if not whatsapp_configured():

            flash(
                "WhatsApp API is not configured. "
                "Render Environment Variables "
                "check करें."
            )

            return redirect(
                url_for("campaigns")
            )

        # -------------------------------------------------
        # FIND CONTACTS
        # -------------------------------------------------

        contacts_to_send = []

        # =================================================
        # SINGLE
        # =================================================

        if target_type == "single":

            if not target_phone:

                flash(
                    "Single WhatsApp number missing."
                )

                return redirect(
                    url_for("campaigns")
                )

            contact = c.execute("""
                SELECT *
                FROM contacts
                WHERE phone=?
                LIMIT 1
            """, (
                target_phone,
            )).fetchone()

            if contact:

                contacts_to_send = [
                    contact
                ]

            else:

                # Manual number not saved in contacts
                contacts_to_send = [{
                    "id": None,
                    "name": "Manual Contact",
                    "phone": target_phone
                }]

        # =================================================
        # GROUP
        # =================================================

        else:

            if not group_name:

                flash(
                    "Campaign group is missing."
                )

                return redirect(
                    url_for("campaigns")
                )

            contacts_to_send = c.execute("""
                SELECT *
                FROM contacts
                WHERE group_name=?
                  AND phone IS NOT NULL
                  AND TRIM(phone) != ''
                ORDER BY name
            """, (
                group_name,
            )).fetchall()

        if not contacts_to_send:

            flash(
                "No valid contacts found "
                "for this campaign."
            )

            return redirect(
                url_for("campaigns")
            )

        # -------------------------------------------------
        # DOWNLOAD ATTACHMENT ONCE
        # -------------------------------------------------

        attachment_bytes = None

        if drive_file_id:

            if not google_token():

                flash(
                    "Google Drive attachment selected "
                    "but Google Drive is not connected."
                )

                return redirect(
                    url_for("campaigns")
                )

            attachment_bytes, attachment_error = (
                download_drive_file(
                    drive_file_id,
                    drive_mime_type
                )
            )

            if attachment_error:

                flash(
                    f"Attachment download error: "
                    f"{attachment_error}"
                )

                return redirect(
                    url_for("campaigns")
                )

            if not attachment_bytes:

                flash(
                    "Attachment file is empty."
                )

                return redirect(
                    url_for("campaigns")
                )

            # Google Sheets exported as CSV
            if (
                drive_mime_type
                ==
                "application/vnd.google-apps.spreadsheet"
            ):

                drive_mime_type = "text/csv"

                if not drive_file_name.lower().endswith(
                    ".csv"
                ):

                    drive_file_name += ".csv"

        # -------------------------------------------------
        # SEND
        # -------------------------------------------------

        sent_count = 0
        failed_count = 0

        for contact in contacts_to_send:

            phone = clean_phone(
                contact["phone"]
            )

            if not phone:

                failed_count += 1

                continue

            success = False
            wa_message_id = None
            result = None

            # ---------------------------------------------
            # ATTACHMENT
            # ---------------------------------------------

            if attachment_bytes:

                success, wa_message_id, result = (
                    send_whatsapp_media(
                        phone=phone,
                        file_bytes=attachment_bytes,
                        mime_type=drive_mime_type,
                        filename=drive_file_name,
                        caption=message
                    )
                )

            # ---------------------------------------------
            # TEXT ONLY
            # ---------------------------------------------

            else:

                success, wa_message_id, result = (
                    send_whatsapp_text(
                        phone,
                        message
                    )
                )

            # ---------------------------------------------
            # STATUS
            # ---------------------------------------------

            if success:

                status = "accepted"

                error_text = None

                sent_count += 1

            else:

                status = "failed"

                error_text = str(
                    result
                )

                failed_count += 1

            # ---------------------------------------------
            # SAVE MESSAGE LOG
            # ---------------------------------------------

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
                VALUES
                (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                cid,
                contact["id"],
                phone,
                message,
                wa_message_id,
                "outgoing",
                status,
                error_text
            ))

        # -------------------------------------------------
        # CAMPAIGN STATUS
        # -------------------------------------------------

        if (
            sent_count > 0
            and
            failed_count == 0
        ):

            campaign_status = "Sent"

        elif (
            sent_count > 0
            and
            failed_count > 0
        ):

            campaign_status = "Partial"

        else:

            campaign_status = "Failed"

        c.execute("""
            UPDATE campaigns
            SET status=?
            WHERE id=?
        """, (
            campaign_status,
            cid
        ))

        c.commit()

        flash(
            f"Campaign completed. "
            f"Success: {sent_count}, "
            f"Failed: {failed_count}."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Send campaign error: {str(e)}"
        )

    finally:

        c.close()

    return redirect(
        url_for("campaigns")
    )


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

        campaign = c.execute("""
            SELECT name
            FROM campaigns
            WHERE id=?
        """, (
            cid,
        )).fetchone()

        if not campaign:

            flash(
                "Campaign not found."
            )

            return redirect(
                url_for("campaigns")
            )

        # Delete WhatsApp logs
        c.execute("""
            DELETE FROM whatsapp_messages
            WHERE campaign_id=?
        """, (
            cid,
        ))

        # Delete campaign
        c.execute("""
            DELETE FROM campaigns
            WHERE id=?
        """, (
            cid,
        ))

        c.commit()

        flash(
            f"Campaign '{campaign['name']}' "
            f"deleted successfully."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Campaign delete error: {str(e)}"
        )

    finally:

        c.close()

    return redirect(
        url_for("campaigns")
    )


# =========================================================
# INCOMING MESSAGES
# =========================================================

@app.route("/incoming")
def incoming():

    c = db()

    try:

        rows = c.execute("""
            SELECT *
            FROM whatsapp_incoming
            ORDER BY id DESC
            LIMIT 500
        """).fetchall()

    finally:

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
# WEBHOOK VERIFY
# =========================================================

@app.route(
    "/webhook",
    methods=["GET", "POST"]
)
def webhook():

    # -----------------------------------------------------
    # META VERIFICATION
    # -----------------------------------------------------

    if request.method == "GET":

        mode = request.args.get(
            "hub.mode"
        )

        token = request.args.get(
            "hub.verify_token"
        )

        challenge = request.args.get(
            "hub.challenge"
        )

        verify_token = get_env(
            "WEBHOOK_VERIFY_TOKEN"
        )

        if (
            mode == "subscribe"
            and token
            and verify_token
            and hmac.compare_digest(
                token,
                verify_token
            )
        ):

            return challenge, 200

        return (
            "Verification failed",
            403
        )

    # -----------------------------------------------------
    # POST
    # -----------------------------------------------------

    if not verify_meta_signature():

        return (
            "Invalid signature",
            403
        )

    try:

        payload = request.get_json(
            silent=True
        )

        if payload is None:

            payload = {}

        c = db()

        try:

            c.execute("""
                INSERT INTO webhook_events
                (
                    event_type,
                    payload
                )
                VALUES (?, ?)
            """, (
                "whatsapp",
                json.dumps(
                    payload,
                    ensure_ascii=False
                )
            ))

            # ---------------------------------------------
            # PROCESS WHATSAPP EVENTS
            # ---------------------------------------------

            if isinstance(
                payload,
                dict
            ):

                entries = payload.get(
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

                        messages = value.get(
                            "messages",
                            []
                        )

                        for msg in messages:

                            wa_message_id = (
                                msg.get("id")
                            )

                            phone = (
                                msg.get("from")
                            )

                            message_type = (
                                msg.get("type")
                            )

                            message_text = ""

                            if message_type == "text":

                                message_text = (
                                    msg.get(
                                        "text",
                                        {}
                                    ).get(
                                        "body",
                                        ""
                                    )
                                )

                            elif message_type:

                                message_text = (
                                    f"[{message_type}]"
                                )

                            if wa_message_id:

                                c.execute("""
                                    INSERT INTO
                                    whatsapp_incoming
                                    (
                                        wa_message_id,
                                        phone,
                                        message_type,
                                        message
                                    )
                                    VALUES
                                    (?, ?, ?, ?)
                                    ON CONFLICT
                                    (wa_message_id)
                                    DO NOTHING
                                """, (
                                    wa_message_id,
                                    phone,
                                    message_type,
                                    message_text
                                ))

            c.commit()

        finally:

            c.close()

        return (
            jsonify({
                "status": "ok"
            }),
            200
        )

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            str(e)
        )

        return (
            jsonify({
                "status": "error",
                "message": str(e)
            }),
            500
        )


# =========================================================
# WEBHOOK LOGS
# =========================================================

@app.route("/webhook/logs")
def webhook_logs():

    c = db()

    try:

        rows = c.execute("""
            SELECT *
            FROM webhook_events
            ORDER BY id DESC
            LIMIT 100
        """).fetchall()

    finally:

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
        google_redirect_uri=GOOGLE_REDIRECT_URI
    )


# =========================================================
# WHATSAPP STATUS API
# =========================================================

@app.route(
    "/api/whatsapp/status"
)
def whatsapp_status():

    return jsonify({

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

    return jsonify({

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

    })


# =========================================================
# DEBUG ROUTES
# =========================================================

@app.route("/debug/routes")
def debug_routes():

    return jsonify([
        {
            "rule": str(rule),
            "endpoint": rule.endpoint,
            "methods": sorted(
                rule.methods
            )
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
# RUN APP
# IMPORTANT:
# app.run() MUST BE AT THE VERY END
# =========================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    # Initialize database BEFORE starting server
    init_db()

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
