from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
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
from datetime import datetime


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
# DATABASE - POSTGRESQL
# =========================================================

class DBWrapper:

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, params=None):

        # Convert old SQLite-style ? placeholders
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

        # =====================================================
        # CONTACTS
        # =====================================================

        c.execute("""
            CREATE TABLE IF NOT EXISTS contacts(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                group_name TEXT DEFAULT 'General'
            )
        """)

        # =====================================================
        # CAMPAIGNS
        # =====================================================

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

        # =====================================================
        # WHATSAPP MESSAGES
        # =====================================================

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

        # =====================================================
        # INCOMING WHATSAPP MESSAGES
        # =====================================================

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

        # =====================================================
        # WEBHOOK EVENTS
        # =====================================================

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
# IMPORTANT FOR RENDER / GUNICORN
# =========================================================
#
# Render normally starts Flask with:
#
# gunicorn app:app
#
# Therefore __main__ block does not execute.
#
# So database initialization must happen during
# application import/startup.
#
# =========================================================

init_db()


# =========================================================
# WHATSAPP ENVIRONMENT VARIABLES
# =========================================================

WHATSAPP_ACCESS_TOKEN = get_env(
    "WHATSAPP_ACCESS_TOKEN"
)

WHATSAPP_PHONE_NUMBER_ID = get_env(
    "WHATSAPP_PHONE_NUMBER_ID"
)

META_APP_SECRET = get_env(
    "META_APP_SECRET"
)

WEBHOOK_VERIFY_TOKEN = (
    get_env("WEBHOOK_VERIFY_TOKEN")
    or "margdarshak_webhook_2026"
)


def whatsapp_configured():

    return bool(
        get_env("WHATSAPP_ACCESS_TOKEN")
        and
        get_env("WHATSAPP_PHONE_NUMBER_ID")
    )


# =========================================================
# PHONE NUMBER CLEANING
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
# META SIGNATURE VERIFICATION
# =========================================================

def verify_meta_signature():

    if not META_APP_SECRET:
        return True

    signature = request.headers.get(
        "X-Hub-Signature-256",
        ""
    )

    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
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
# WHATSAPP TEXT MESSAGE
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

    url = (
        f"https://graph.facebook.com/v23.0/"
        f"{phone_id}/messages"
    )

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
# WHATSAPP TEMPLATE MESSAGE
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

    url = (
        f"https://graph.facebook.com/v23.0/"
        f"{phone_id}/messages"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    components = []

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

    payload = {

        "messaging_product": "whatsapp",

        "to": phone,

        "type": "template",

        "template": {

            "name": template_name,

            "language": {
                "code": language_code
            }
        }
    }

    if components:

        payload["template"]["components"] = components

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
# DASHBOARD
# =========================================================

@app.route("/")
def dashboard():

    c = db()

    contacts = c.execute(
        "SELECT COUNT(*) n FROM contacts"
    ).fetchone()["n"]

    campaigns = c.execute(
        "SELECT COUNT(*) n FROM campaigns"
    ).fetchone()["n"]

    sent = c.execute("""
        SELECT COUNT(*) n
        FROM whatsapp_messages
        WHERE status IN
        ('sent','delivered','read','accepted')
    """).fetchone()["n"]

    delivered = c.execute("""
        SELECT COUNT(*) n
        FROM whatsapp_messages
        WHERE status='delivered'
    """).fetchone()["n"]

    read = c.execute("""
        SELECT COUNT(*) n
        FROM whatsapp_messages
        WHERE status='read'
    """).fetchone()["n"]

    failed = c.execute("""
        SELECT COUNT(*) n
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

        text = f.read().decode(
            "utf-8-sig",
            errors="ignore"
        )

        reader = csv.DictReader(
            io.StringIO(text)
        )

        c = db()

        added = 0

        for row in reader:

            name = (
                row.get("name")
                or row.get("Name")
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

            if phone:

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

        c.commit()
        c.close()

        flash(
            f"{added} contacts imported."
        )

        return redirect(
            url_for("contacts")
        )

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
# CAMPAIGNS
# =========================================================

@app.route(
    "/campaigns",
    methods=["GET", "POST"]
)
def campaigns():

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

        if not name or not message:

            flash(
                "Campaign name और message जरूरी है."
            )

            return redirect(
                url_for("campaigns")
            )

        c = db()

        c.execute("""
            INSERT INTO campaigns
            (
                name,
                message,
                group_name
            )
            VALUES (?, ?, ?)
        """, (
            name,
            message,
            group_name
        ))

        c.commit()
        c.close()

        flash(
            "Campaign saved as Draft."
        )

        return redirect(
            url_for("campaigns")
        )

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

@app.route(
    "/campaign/<int:cid>/send",
    methods=["POST"]
)
def send_campaign(cid):

    c = db()

    campaign = c.execute("""
        SELECT *
        FROM campaigns
        WHERE id=?
    """, (
        cid,
    )).fetchone()

    if not campaign:

        c.close()

        flash(
            "Campaign not found."
        )

        return redirect(
            url_for("campaigns")
        )

    if not whatsapp_configured():

        c.execute("""
            UPDATE campaigns
            SET status='API Not Configured'
            WHERE id=?
        """, (
            cid,
        ))

        c.commit()
        c.close()

        flash(
            "WHATSAPP_ACCESS_TOKEN और "
            "WHATSAPP_PHONE_NUMBER_ID configure करें."
        )

        return redirect(
            url_for("campaigns")
        )

    q = "SELECT * FROM contacts"

    params = ()

    if campaign["group_name"]:

        q += " WHERE group_name=?"

        params = (
            campaign["group_name"],
        )

    contacts = c.execute(
        q,
        params
    ).fetchall()

    sent = 0
    failed = 0

    for contact in contacts:

        body = campaign[
            "message"
        ].replace(
            "{{name}}",
            contact["name"]
        )

        phone = clean_phone(
            contact["phone"]
        )

        ok, message_id, response = (
            send_whatsapp_text(
                phone,
                body
            )
        )

        if ok:

            status = "accepted"

            sent += 1

            error_text = None

        else:

            status = "failed"

            failed += 1

            error_text = str(
                response
            )

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

    c.execute("""
        UPDATE campaigns
        SET status=?
        WHERE id=?
    """, (
        f"Accepted {sent}, Failed {failed}",
        cid
    ))

    c.commit()
    c.close()

    flash(
        f"Campaign finished: "
        f"{sent} accepted, "
        f"{failed} failed."
    )

    return redirect(
        url_for("campaigns")
    )


# =========================================================
# WEBHOOK VERIFY
# =========================================================

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

    return "Forbidden", 403


# =========================================================
# WEBHOOK RECEIVE
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook_receive():

    if not verify_meta_signature():

        return (
            "Invalid signature",
            403
        )

    c = None

    try:

        data = request.get_json(
            silent=True
        )

        if not data:

            return "OK", 200

        c = db()

        # =====================================================
        # SAVE COMPLETE WEBHOOK EVENT
        # =====================================================

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
                data,
                ensure_ascii=False
            )
        ))

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

                # =================================================
                # INCOMING MESSAGE
                # =================================================

                messages = value.get(
                    "messages",
                    []
                )

                for msg in messages:

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

                    elif msg_type == "interactive":

                        interactive = msg.get(
                            "interactive",
                            {}
                        )

                        if interactive.get(
                            "type"
                        ) == "button_reply":

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

                        elif interactive.get(
                            "type"
                        ) == "list_reply":

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
                    # SAVE INCOMING MESSAGE
                    # =================================================

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

                    # =================================================
                    # AUTO CREATE CONTACT
                    # =================================================

                    if sender:

                        cleaned_sender = clean_phone(
                            sender
                        )

                        existing = c.execute("""
                            SELECT id
                            FROM contacts
                            WHERE phone=?
                        """, (
                            cleaned_sender,
                        )).fetchone()

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
        # so it does not repeatedly retry.
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
# MESSAGE HISTORY
# =========================================================

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


# =========================================================
# INCOMING MESSAGES
# =========================================================

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


# =========================================================
# WEBHOOK LOGS
# =========================================================

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
        )
    )


# =========================================================
# API - WHATSAPP STATUS
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
            )
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

        "time":
            datetime.utcnow().isoformat(),

        "database_error":
            database_error
    })


# =========================================================
# RUN LOCAL
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
