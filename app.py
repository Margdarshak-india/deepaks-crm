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
            group_name TEXT DEFAULT 'General'
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

    # Incoming messages
    c.execute("""
        CREATE TABLE IF NOT EXISTS whatsapp_incoming(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wa_message_id TEXT UNIQUE,
            phone TEXT,
            message_type TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Webhook event log
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
# ENVIRONMENT VARIABLES
# =========================================================

def get_env(name):
    return os.getenv(name, "").strip()


WHATSAPP_ACCESS_TOKEN = get_env("WHATSAPP_ACCESS_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = get_env("WHATSAPP_PHONE_NUMBER_ID")
META_APP_SECRET = get_env("META_APP_SECRET")
WEBHOOK_VERIFY_TOKEN = get_env(
    "WEBHOOK_VERIFY_TOKEN"
) or "margdarshak_webhook_2026"


def whatsapp_configured():
    return bool(
        get_env("WHATSAPP_ACCESS_TOKEN")
        and get_env("WHATSAPP_PHONE_NUMBER_ID")
    )


# =========================================================
# PHONE NUMBER CLEANING
# =========================================================

def clean_phone(phone):
    """
    Meta WhatsApp API के लिए number:
    country code सहित होना चाहिए.

    Example:
    919977483335
    """

    if not phone:
        return ""

    phone = str(phone).strip()

    # +, spaces, -, brackets हटाएँ
    phone = phone.replace("+", "")
    phone = phone.replace(" ", "")
    phone = phone.replace("-", "")
    phone = phone.replace("(", "")
    phone = phone.replace(")", "")

    # शुरुआत में 00 हो तो हटाएँ
    if phone.startswith("00"):
        phone = phone[2:]

    return phone


# =========================================================
# META SIGNATURE VERIFICATION
# =========================================================

def verify_meta_signature():
    """
    Meta webhook POST request की signature verify करता है.
    """

    if not META_APP_SECRET:
        # Development में secret नहीं है तो allow करें.
        # Production में META_APP_SECRET जरूर रखें.
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")

    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        request.get_data(),
        hashlib.sha256
    ).hexdigest()

    received = signature.replace("sha256=", "", 1)

    return hmac.compare_digest(expected, received)


# =========================================================
# WHATSAPP TEXT MESSAGE
# =========================================================

def send_whatsapp_text(phone, body):

    token = get_env("WHATSAPP_ACCESS_TOKEN")
    phone_id = get_env("WHATSAPP_PHONE_NUMBER_ID")

    if not token or not phone_id:
        return False, None, "WhatsApp API credentials missing"

    phone = clean_phone(phone)

    if not phone:
        return False, None, "Invalid phone number"

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

            return True, message_id, data

        return False, None, data

    except Exception as e:
        return False, None, str(e)


# =========================================================
# WHATSAPP TEMPLATE MESSAGE
# =========================================================

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

    url = f"https://graph.facebook.com/v23.0/{phone_id}/messages"

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
                messages = data.get("messages", [])

                if messages:
                    message_id = messages[0].get("id")

            return True, message_id, data

        return False, None, data

    except Exception as e:
        return False, None, str(e)


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
        WHERE status IN ('sent','delivered','read')
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
                        (name, phone, group_name)
                        VALUES (?, ?, ?)
                    """, (
                        name,
                        phone,
                        group
                    ))

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

@app.route("/campaigns", methods=["GET", "POST"])
def campaigns():

    if request.method == "POST":

        name = request.form.get(
            "name", ""
        ).strip()

        message = request.form.get(
            "message", ""
        ).strip()

        group_name = request.form.get(
            "group_name", ""
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
            (name, message, group_name)
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
    """, (cid,)).fetchone()

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
        """, (cid,))

        c.commit()
        c.close()

        flash(
            "WHATSAPP_ACCESS_TOKEN और "
            "WHATSAPP_PHONE_NUMBER_ID configure करें."
        )

        return redirect(
            url_for("campaigns")
        )

    # Contacts select करें
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

        body = campaign["message"].replace(
            "{{name}}",
            contact["name"]
        )

        phone = clean_phone(
            contact["phone"]
        )

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

            error_text = str(response)

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
        f"{sent} accepted, {failed} failed."
    )

    return redirect(
        url_for("campaigns")
    )


# =========================================================
# WEBHOOK VERIFY - META GET REQUEST
# =========================================================

@app.route("/webhook", methods=["GET"])
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
        and token == WEBHOOK_VERIFY_TOKEN
    ):
        return challenge, 200

    return "Forbidden", 403


# =========================================================
# WEBHOOK RECEIVE - META POST REQUEST
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook_receive():

    # Signature check
    if not verify_meta_signature():

        return "Invalid signature", 403

    try:

        data = request.get_json(
            silent=True
        )

        if not data:
            return "OK", 200

        # पूरा event save करें
        c = db()

        c.execute("""
            INSERT INTO webhook_events
            (event_type, payload)
            VALUES (?, ?)
        """, (
            "whatsapp",
            json.dumps(
                data,
                ensure_ascii=False
            )
        ))

        c.commit()

        # -------------------------------------------------
        # Meta WhatsApp structure
        # -------------------------------------------------

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

                # =========================================
                # MESSAGE STATUS
                # =========================================

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

                    recipient = status.get(
                        "recipient_id"
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

                    # अगर message database में नहीं मिला
                    else:
                        pass

                # =========================================
                # INCOMING MESSAGE
                # =========================================

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
           
