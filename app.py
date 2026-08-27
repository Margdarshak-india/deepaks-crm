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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
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


# Initialize database
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

        "fields":
            "files(id,name,mimeType,size,"
            "modifiedTime,webViewLink)",

        "q":
            "trashed = false and "
            "("
            "mimeType = 'text/csv' or "
            "mimeType = "
            "'application/vnd.google-apps.spreadsheet'"
            ")"
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
                or row.get("student name")
                or row.get("Student Name")
                or row.get("student_name")
                or row.get("Student_Name")
                or ""
            ).strip()

            name = name or "Customer"

            phone = (
                row.get("phone")
                or row.get("Phone")
                or row.get("mobile")
                or row.get("Mobile")
                or ""
            ).strip()

            phone = clean_phone(phone)

            group = (
                row.get("group")
                or row.get("Group")
                or "General"
            ).strip()

            group = group or "General"

            if not phone:

                skipped += 1
                continue

            try:

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

            except Exception:

                c.rollback()

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

        contacts = c.execute(
            "SELECT COUNT(*) AS n FROM contacts"
        ).fetchone()["n"]

        campaigns = c.execute(
            "SELECT COUNT(*) AS n FROM campaigns"
        ).fetchone()["n"]

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

        text = f.read().decode(
            "utf-8-sig",
            errors="ignore"
        )

        reader = csv.DictReader(
            io.StringIO(text)
        )

        c = db()

        added = 0
        skipped = 0

        try:

            for row in reader:

                # -------------------------------------------------
                # NAME
                # -------------------------------------------------

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

                    value = row.get(
                        column
                    )

                    if (
                        value
                        and
                        str(value).strip()
                    ):

                        name = str(
                            value
                        ).strip()

                        break

                # -------------------------------------------------
                # PHONE
                # -------------------------------------------------

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

                    value = row.get(
                        column
                    )

                    if (
                        value
                        and
                        str(value).strip()
                    ):

                        phone = str(
                            value
                        ).strip()

                        break

                phone = clean_phone(
                    phone
                )

                # -------------------------------------------------
                # GROUP
                # -------------------------------------------------

                group = ""

                group_columns = [

                    "Group",
                    "group",
                    "GROUP",
                    "Group Name",
                    "group name"

                ]

                for column in group_columns:

                    value = row.get(
                        column
                    )

                    if (
                        value
                        and
                        str(value).strip()
                    ):

                        group = str(
                            value
                        ).strip()

                        break

                if not group:
                    group = "General"

                # -------------------------------------------------
                # PHONE REQUIRED
                # -------------------------------------------------

                if not phone:

                    skipped += 1
                    continue

                # -------------------------------------------------
                # NAME FALLBACK
                # -------------------------------------------------

                if not name:
                    name = "Unknown"

                # -------------------------------------------------
                # INSERT
                # -------------------------------------------------

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
                        name,
                        phone,
                        group
                    ))

                    added += 1

                except IntegrityError:

                    c.rollback()

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

    # -------------------------------------------------
    # SHOW CONTACTS
    # -------------------------------------------------

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
# DELETE CONTACT
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

    try:

        ids = [
            int(x)
            for x in ids
        ]

    except ValueError:

        flash(
            "Invalid contact selection."
        )

        return redirect(
            url_for("contacts")
        )

    c = db()

    try:

        cursor = c.execute("""
            DELETE FROM contacts
            WHERE id = ANY(%s::integer[])
        """, (
            ids,
        ))

        deleted = cursor.rowcount

        c.commit()

        flash(
            f"{deleted} selected contacts deleted successfully."
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
                if
                "session expired"
                in
                str(error).lower()
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

        "client_id":
            GOOGLE_CLIENT_ID,

        "redirect_uri":
            GOOGLE_REDIRECT_URI,

        "response_type":
            "code",

        "scope":
            GOOGLE_OAUTH_SCOPE,

        "access_type":
            "offline",

        "prompt":
            "consent",

        "include_granted_scopes":
            "true",

        "state":
            state
    }

    return redirect(
        GOOGLE_AUTH_URL
        + "?"
        + urlencode(params)
    )


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

        "code":
            code,

        "client_id":
            GOOGLE_CLIENT_ID,

        "client_secret":
            GOOGLE_CLIENT_SECRET,

        "redirect_uri":
            GOOGLE_REDIRECT_URI,

        "grant_type":
            "authorization_code"
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
                "message":
                    "Google token exchange failed",
                "details":
                    data
            }), 400

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

        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


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

    if not content:

        flash(
            "Google Drive file empty है."
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
            f"Google Drive से "
            f"{added} contacts import हुए. "
            f"{extra}"
        )

    except Exception as e:

        flash(
            f"CSV import error: {str(e)}"
        )

    return redirect(
        url_for("google_drive")
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
        ).strip()

        group_name = request.form.get(
            "group_name",
            ""
        ).strip()

        # -------------------------------------------------
        # MANUALLY SELECTED CONTACT
        # -------------------------------------------------

        single_contact_id = request.form.get(
            "single_contact_id",
            ""
        ).strip()

        # Backward compatibility
        single_number = request.form.get(
            "single_number",
            ""
        ).strip()

        # -------------------------------------------------
        # BASIC VALIDATION
        # -------------------------------------------------

        if not name or not message:

            flash(
                "Campaign name and message are required."
            )

            return redirect(
                url_for("campaigns")
            )

        # =================================================
        # SINGLE CONTACT CAMPAIGN
        # =================================================

        if campaign_type == "single":

            phone = ""

            # -------------------------------------------------
            # FIRST: CONTACT ID
            # -------------------------------------------------

            if single_contact_id:

                try:

                    contact_id = int(
                        single_contact_id
                    )

                except ValueError:

                    flash(
                        "Invalid contact selected."
                    )

                    return redirect(
                        url_for("campaigns")
                    )

                c = db()

                try:

                    contact = c.execute("""
                        SELECT id, name, phone
                        FROM contacts
                        WHERE id=?
                    """, (
                        contact_id,
                    )).fetchone()

                finally:

                    c.close()

                if not contact:

                    flash(
                        "Selected contact not found."
                    )

                    return redirect(
                        url_for("campaigns")
                    )

                phone = clean_phone(
                    contact["phone"]
                )

            # -------------------------------------------------
            # BACKWARD COMPATIBILITY
            # -------------------------------------------------

            elif single_number:

                phone = clean_phone(
                    single_number
                )

            # -------------------------------------------------
            # VALIDATE PHONE
            # -------------------------------------------------

            if not phone:

                flash(
                    "Please select a valid WhatsApp contact."
                )

                return redirect(
                    url_for("campaigns")
                )

            # -------------------------------------------------
            # STORE SINGLE NUMBER
            # -------------------------------------------------

            group_name = (
                "__SINGLE__:"
                + phone
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

            c.execute("""
                INSERT INTO campaigns
                (
                    name,
                    message,
                    group_name,
                    status
                )
                VALUES (?, ?, ?, 'Draft')
            """, (
                name,
                message,
                group_name
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
    # GET CAMPAIGNS
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
            r["group_name"]
            for r in groups_rows
        ]

        # -------------------------------------------------
        # CONTACTS FOR MANUAL SELECTION
        # IMPORTANT:
        # PostgreSQL DOES NOT use COLLATE NOCASE
        # -------------------------------------------------

        contacts_rows = c.execute("""
            SELECT
                id,
                name,
                phone,
                group_name
            FROM contacts
            WHERE phone IS NOT NULL
              AND TRIM(phone) != ''
            ORDER BY name ASC, id DESC
        """).fetchall()

        contacts = contacts_rows

    finally:

        c.close()

    return render_template(
        "campaigns.html",
        rows=rows,
        groups=groups,
        contacts=contacts
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

        message = campaign["message"]
        group_name = campaign["group_name"]

        # -------------------------------------------------
        # FIND TARGET CONTACTS
        # -------------------------------------------------

        targets = []

        # =================================================
        # SINGLE CONTACT
        # =================================================

        if (
            group_name
            and
            group_name.startswith(
                "__SINGLE__:"
            )
        ):

            phone = group_name.replace(
                "__SINGLE__:",
                "",
                1
            )

            phone = clean_phone(
                phone
            )

            contact = c.execute("""
                SELECT id, name, phone
                FROM contacts
                WHERE phone=?
                LIMIT 1
            """, (
                phone,
            )).fetchone()

            if contact:

                targets = [contact]

            else:

                # Allow sending even if contact
                # was deleted after campaign creation.

                targets = [{
                    "id": None,
                    "name": "Customer",
                    "phone": phone
                }]

        # =================================================
        # GROUP
        # =================================================

        else:

            targets = c.execute("""
                SELECT id, name, phone
                FROM contacts
                WHERE group_name=?
                  AND phone IS NOT NULL
                  AND TRIM(phone) != ''
                ORDER BY id ASC
            """, (
                group_name,
            )).fetchall()

        # -------------------------------------------------
        # NO TARGETS
        # -------------------------------------------------

        if not targets:

            c.execute("""
                UPDATE campaigns
                SET status='Failed'
                WHERE id=?
            """, (
                cid,
            ))

            c.commit()

            flash(
                "No contacts found for this campaign."
            )

            return redirect(
                url_for("campaigns")
            )

        # -------------------------------------------------
        # CHECK WHATSAPP CONFIG
        # -------------------------------------------------

        if not whatsapp_configured():

            flash(
                "WhatsApp API credentials are not configured."
            )

            return redirect(
                url_for("campaigns")
            )

        sent_count = 0
        failed_count = 0

        # -------------------------------------------------
        # SEND TO EACH CONTACT
        # -------------------------------------------------

        for contact in targets:

            phone = clean_phone(
                contact["phone"]
            )

            if not phone:

                failed_count += 1

                continue

            success, wa_message_id, result = (
                send_whatsapp_text(
                    phone,
                    message
                )
            )

            if success:

                status = "accepted"

                sent_count += 1

                error_text = None

            else:

                status = "failed"

                failed_count += 1

                error_text = str(result)

            # -------------------------------------------------
            # SAVE MESSAGE RECORD
            # -------------------------------------------------

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
                    error,
                    created_at,
                    updated_at
                )
                VALUES
                (
                    ?, ?, ?, ?, ?, 'outgoing',
                    ?, ?, CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
            """, (
                cid,
                contact["id"],
                phone,
                message,
                wa_message_id,
                status,
                error_text
            ))

        # -------------------------------------------------
        # CAMPAIGN STATUS
        # -------------------------------------------------

        if sent_count > 0 and failed_count == 0:

            campaign_status = "Accepted"

        elif sent_count > 0 and failed_count > 0:

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
            f"Campaign sent. "
            f"Accepted: {sent_count}, "
            f"Failed: {failed_count}."
        )

    except Exception as e:

        c.rollback()

        flash(
            f"Campaign send error: {str(e)}"
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

        # -------------------------------------------------
        # DELETE WHATSAPP MESSAGE RECORDS FIRST
        # -------------------------------------------------

        c.execute("""
            DELETE FROM whatsapp_messages
            WHERE campaign_id=?
        """, (
            cid,
        ))

        # -------------------------------------------------
        # DELETE CAMPAIGN
        # -------------------------------------------------

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

        config_ok=
            whatsapp_configured(),

        webhook_token_ok=
            bool(
                get_env(
                    "WEBHOOK_VERIFY_TOKEN"
                )
            ),

        app_secret_ok=
            bool(
                get_env(
                    "META_APP_SECRET"
                )
            ),

        google_configured=
            google_configured(),

        google_connected=
            bool(
                google_token()
            ),

        google_redirect_uri=
            GOOGLE_REDIRECT_URI
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
            "rule":
                str(rule),

            "endpoint":
                rule.endpoint,

            "methods":
                sorted(
                    rule.methods
                )
        }

        for rule
        in app.url_map.iter_rules()

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
# IMPORTANT:
# app.run() MUST remain at the VERY END.
# =========================================================

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
