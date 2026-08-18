from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import sqlite3
import csv
import io
import os
import requests
import hmac
import hashlib
import json
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

DB = "deepaks_crm.db"


# =========================================================
# DATABASE
# =========================================================

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()

    # Contacts
    c.execute("""
        CREATE TABLE IF NOT EXISTS contacts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            group_name TEXT DEFAULT 'General',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Campaigns
    c.execute("""
        CREATE TABLE IF NOT EXISTS campaigns(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            group_name TEXT,
            status TEXT DEFAULT 'Draft',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # WhatsApp messages
    c.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wa_message_id TEXT UNIQUE,
            phone TEXT,
            contact_name TEXT,
            direction TEXT,
            message_type TEXT,
            message_text TEXT,
            status TEXT DEFAULT 'received',
            error_code TEXT,
            error_message TEXT,
            timestamp TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Webhook events
    c.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.commit()
    c.close()


init_db()


# =========================================================
# WHATSAPP CONFIGURATION
# =========================================================

def whatsapp_configured():
    return bool(
        os.getenv("WHATSAPP_ACCESS_TOKEN")
        and os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    )


def get_verify_token():
    return os.getenv(
        "WHATSAPP_VERIFY_TOKEN",
        "margdarshak_webhook_2026"
    )


# =========================================================
# WHATSAPP API SEND
# =========================================================

def send_whatsapp_text(phone, body):

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

    if not token or not phone_id:
        return False, "API credentials missing", None

    # Remove spaces and common symbols
    phone = str(phone).strip()
    phone = phone.replace("+", "")
    phone = phone.replace(" ", "")
    phone = phone.replace("-", "")
    phone = phone.replace("(", "")
    phone = phone.replace(")", "")

    url = f"https://graph.facebook.com/v23.0/{phone_id}/messages"

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

            return True, data, message_id

        return False, data, None

    except Exception as e:
        return False, str(e), None


# =========================================================
# WEBHOOK SIGNATURE VALIDATION
# =========================================================

def verify_meta_signature():

    app_secret = os.getenv("META_APP_SECRET")

    # During initial development, if App Secret is not configured,
    # allow webhook POST so testing is easier.
    if not app_secret:
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")

    if not signature:
        return False

    raw_body = request.get_data()

    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


# =========================================================
# META WEBHOOK VERIFICATION
# =========================================================

@app.route("/webhook", methods=["GET"])
def webhook_verify():

    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    verify_token = get_verify_token()

    print("Webhook verification request received")

    if mode == "subscribe" and token == verify_token:

        print("WEBHOOK VERIFIED")

        return challenge or "", 200

    print("Webhook verification failed")

    return "Forbidden", 403


# =========================================================
# META WHATSAPP WEBHOOK
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook_receive():

    # Verify Meta signature if App Secret is configured
    if not verify_meta_signature():
        print("Invalid Meta webhook signature")
        return "Invalid signature", 403

    raw_body = request.get_data()

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return "Invalid JSON", 400

    # Store raw webhook event
    try:

        c = db()

        c.execute(
            """
            INSERT INTO webhook_events(event_type, payload)
            VALUES(?, ?)
            """,
            (
                data.get("object", "unknown"),
                json.dumps(data)
            )
        )

        c.commit()
        c.close()

    except Exception as e:
        print("Webhook log error:", e)

    # Only WhatsApp Business Account events
    if data.get("object") != "whatsapp_business_account":
        return "OK", 200

    # =====================================================
    # ENTRIES
    # =====================================================

    for entry in data.get("entry", []):

        for change in entry.get("changes", []):

            value = change.get("value", {})

            # =================================================
            # INCOMING MESSAGES
            # =================================================

            for message in value.get("messages", []):

                try:

                    wa_message_id = message.get("id")
                    sender = message.get("from")
                    message_type = message.get("type")
                    timestamp = message.get("timestamp")

                    message_text = ""

                    if message_type == "text":

                        message_text = (
                            message.get("text", {})
                            .get("body", "")
                        )

                    elif message_type == "button":

                        message_text = (
                            message.get("button", {})
                            .get("text", "")
                        )

                    elif message_type == "interactive":

                        interactive = message.get(
                            "interactive", {}
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

                    else:

                        message_text = (
                            f"[{message_type} message]"
                        )

                    # Find contact
                    c = db()

                    contact = c.execute(
                        """
                        SELECT * FROM contacts
                        WHERE phone=?
                        """,
                        (sender,)
                    ).fetchone()

                    contact_name = (
                        contact["name"]
                        if contact
                        else "WhatsApp Customer"
                    )

                    # Add contact automatically if not exists
                    if not contact:

                        try:

                            c.execute(
                                """
                                INSERT INTO contacts
                                (name, phone, group_name)
                                VALUES (?, ?, ?)
                                """,
                                (
                                    contact_name,
                                    sender,
                                    "WhatsApp"
                                )
                            )

                        except sqlite3.IntegrityError:
                            pass

                    # Save incoming message
                    try:

                        c.execute(
                            """
                            INSERT INTO whatsapp_messages
                            (
                                wa_message_id,
                                phone,
                                contact_name,
                                direction,
                                message_type,
                                message_text,
                                status,
                                timestamp
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                wa_message_id,
                                sender,
                                contact_name,
                                "incoming",
                                message_type,
                                message_text,
                                "received",
                                timestamp
                            )
                        )

                    except sqlite3.IntegrityError:
                        pass

                    c.commit()
                    c.close()

                    print(
                        "INCOMING:",
                        sender,
                        message_type,
                        message_text
                    )

                except Exception as e:

                    print(
                        "Incoming message error:",
                        e
                    )

            # =================================================
            # MESSAGE STATUS
            # sent / delivered / read / failed
            # =================================================

            for status in value.get("statuses", []):

                try:

                    message_id = status.get("id")
                    status_value = status.get("status")
                    recipient_id = status.get(
                        "recipient_id",
                        ""
                    )

                    timestamp = status.get(
                        "timestamp",
                        ""
                    )

                    errors = status.get(
                        "errors",
                        []
                    )

                    error_code = ""
                    error_message = ""

                    if errors:

                        error = errors[0]

                        error_code = str(
                            error.get("code", "")
                        )

                        error_message = (
                            error.get("title")
                            or error.get("message")
                            or error.get(
                                "error_data",
                                {}
                            ).get(
                                "details",
                                ""
                            )
                        )

                    c = db()

                    # Update existing outgoing message
                    updated = c.execute(
                        """
                        UPDATE whatsapp_messages
                        SET
                            status=?,
                            error_code=?,
                            error_message=?
                        WHERE wa_message_id=?
                        """,
                        (
                            status_value,
                            error_code,
                            error_message,
                            message_id
                        )
                    ).rowcount

                    # If not found, save status event
                    if updated == 0:

                        try:

                            c.execute(
                                """
                                INSERT INTO whatsapp_messages
                                (
                                    wa_message_id,
                                    phone,
                                    contact_name,
                                    direction,
                                    message_type,
                                    message_text,
                                    status,
                                    error_code,
                                    error_message,
                                    timestamp
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    message_id,
                                    recipient_id,
                                    "Customer",
                                    "outgoing",
                                    "text",
                                    "",
                                    status_value,
                                    error_code,
                                    error_message,
                                    timestamp
                                )
                            )

                        except sqlite3.IntegrityError:
                            pass

                    c.commit()
                    c.close()

                    print(
                        "WHATSAPP STATUS:",
                        message_id,
                        status_value,
                        error_code,
                        error_message
                    )

                except Exception as e:

                    print(
                        "Status webhook error:",
                        e
                    )

    # IMPORTANT:
    # Meta expects HTTP 200
    return "EVENT_RECEIVED", 200


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

    incoming = c.execute(
        """
        SELECT COUNT(*) n
        FROM whatsapp_messages
        WHERE direction='incoming'
        """
    ).fetchone()["n"]

    delivered = c.execute(
        """
        SELECT COUNT(*) n
        FROM whatsapp_messages
        WHERE status='delivered'
        """
    ).fetchone()["n"]

    read = c.execute(
        """
        SELECT COUNT(*) n
        FROM whatsapp_messages
        WHERE status='read'
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
        incoming=incoming,
        delivered=delivered,
        read=read,
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

            flash("CSV file select करें.")

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

            group = (
                row.get("group")
                or row.get("Group")
                or "General"
            ).strip()

            group = group or "General"

            if phone:

                phone = (
                    phone
                    .replace("+", "")
                    .replace(" ", "")
                    .replace("-", "")
                    .replace("(", "")
                    .replace(")", "")
                )

                try:

                    c.execute(
                        """
                        INSERT INTO contacts
                        (name, phone, group_name)
                        VALUES (?, ?, ?)
                        """,
                        (
                            name,
                            phone,
                            group
                        )
                    )

                    added += 1

                except sqlite3.IntegrityError:

                    pass

        c.commit()
        c.close()

        flash(
            f"{added} contacts imported."
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
                "Campaign name और message required है."
            )

            return redirect(
                url_for("campaigns")
            )

        c = db()

        c.execute(
            """
            INSERT INTO campaigns
            (name, message, group_name)
            VALUES (?, ?, ?)
            """,
            (
                name,
                message,
                group_name
            )
        )

        c.commit()
        c.close()

        flash(
            "Campaign saved as Draft."
        )

        return redirect(
            url_for("campaigns")
        )

    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM campaigns
        ORDER BY id DESC
        """
    ).fetchall()

    groups = [
        r["group_name"]
        for r in c.execute(
            """
            SELECT DISTINCT group_name
            FROM contacts
            ORDER BY group_name
            """
        ).fetchall()
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

    campaign = c.execute(
        """
        SELECT *
        FROM campaigns
        WHERE id=?
        """,
        (cid,)
    ).fetchone()

    if not campaign:

        c.close()

        flash(
            "Campaign not found."
        )

        return redirect(
            url_for("campaigns")
        )

    if not whatsapp_configured():

        c.execute(
            """
            UPDATE campaigns
            SET status='API Not Configured'
            WHERE id=?
            """,
            (cid,)
        )

        c.commit()
        c.close()

        flash(
            "पहले WhatsApp API credentials configure करें."
        )

        return redirect(
            url_for("campaigns")
        )

    query = "SELECT * FROM contacts"

    params = ()

    if campaign["group_name"]:

        query += """
            WHERE group_name=?
        """

        params = (
            campaign["group_name"],
        )

    contacts = c.execute(
        query,
        params
    ).fetchall()

    c.close()

    sent = 0
    failed = 0

    failure_messages = []

    for contact in contacts:

        body = campaign["message"].replace(
            "{{name}}",
            contact["name"]
        )

        ok, response, message_id = (
            send_whatsapp_text(
                contact["phone"],
                body
            )
        )

        c = db()

        if ok:

            sent += 1

            # Save outgoing message
            try:

                c.execute(
                    """
                    INSERT INTO whatsapp_messages
                    (
                        wa_message_id,
                        phone,
                        contact_name,
                        direction,
                        message_type,
                        message_text,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        contact["phone"],
                        contact["name"],
                        "outgoing",
                        "text",
                        body,
                        "sent"
                    )
                )

            except sqlite3.IntegrityError:

                pass

        else:

            failed += 1

            error_text = str(response)

            if isinstance(response, dict):

                error_text = json.dumps(
                    response,
                    ensure_ascii=False
                )

            failure_messages.append(
                f"FAILED: {contact['name']} "
                f"({contact['phone']}): {error_text}"
            )

        c.commit()
        c.close()

    # Campaign status
    c = db()

    c.execute(
        """
        UPDATE campaigns
        SET status=?
        WHERE id=?
        """,
        (
            f"Sent {sent}, Failed {failed}",
            cid
        )
    )

    c.commit()
    c.close()

    flash(
        f"Campaign finished: {sent} sent, {failed} failed."
    )

    # Store failure details in flash
    for error in failure_messages:

        flash(error)

    return redirect(
        url_for("campaigns")
    )


# =========================================================
# WHATSAPP MESSAGES PAGE
# =========================================================

@app.route("/messages")
def messages():

    c = db()

    rows = c.execute(
        """
        SELECT *
        FROM whatsapp_messages
        ORDER BY id DESC
        LIMIT 200
        """
    ).fetchall()

    c.close()

    return render_template(
        "messages.html",
        rows=rows
    )


# =========================================================
# WEBHOOK LOGS
# =========================================================

@app.route("/webhook-logs")
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
# SETTINGS
# =========================================================

@app.route("/settings")
def settings():

    return render_template(
        "settings.html",
        config_ok=whatsapp_configured(),
        webhook_token_ok=bool(
            get_verify_token()
        ),
        app_secret_ok=bool(
            os.getenv("META_APP_SECRET")
        )
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "whatsapp_configured": whatsapp_configured(),
        "webhook": "active"
    })


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    init_db()

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
