from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
import csv
import io
import os
import requests

app = Flask(__name__)
app.secret_key = "change-this-secret-key"

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

    c.execute("""
        CREATE TABLE IF NOT EXISTS contacts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            group_name TEXT DEFAULT 'General'
        )
    """)

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

    c.commit()
    c.close()


init_db()


# =========================================================
# WHATSAPP CONFIGURATION
# =========================================================

def whatsapp_configured():
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

    return bool(token and phone_id)


# =========================================================
# PHONE NUMBER CLEANING
# =========================================================

def clean_phone(phone):
    """
    WhatsApp ke liye phone number ko clean karta hai.

    Example:
    +91 98765 43210 -> 919876543210
    91-9876543210   -> 919876543210
    """

    if not phone:
        return ""

    phone = str(phone).strip()

    # + remove
    phone = phone.replace("+", "")

    # spaces remove
    phone = phone.replace(" ", "")

    # hyphen remove
    phone = phone.replace("-", "")

    # brackets remove
    phone = phone.replace("(", "")
    phone = phone.replace(")", "")

    # dots remove
    phone = phone.replace(".", "")

    return phone


# =========================================================
# SEND WHATSAPP TEXT
# =========================================================

def send_whatsapp_text(phone, body):

    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")

    if not token:
        return False, "WHATSAPP_ACCESS_TOKEN missing"

    if not phone_id:
        return False, "WHATSAPP_PHONE_NUMBER_ID missing"

    phone = clean_phone(phone)

    if not phone:
        return False, "Phone number is empty"

    # Meta WhatsApp Cloud API
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

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        # JSON response read
        try:
            data = response.json()
        except Exception:
            data = {
                "raw_response": response.text
            }

        # =================================================
        # SUCCESS
        # =================================================

        if response.ok:

            message_id = ""

            try:
                messages = data.get("messages", [])

                if messages:
                    message_id = messages[0].get("id", "")

            except Exception:
                pass

            if message_id:
                return True, f"Message sent. ID: {message_id}"

            return True, "Message sent successfully"

        # =================================================
        # META ERROR
        # =================================================

        error = data.get("error", {})

        error_code = error.get(
            "code",
            "Unknown"
        )

        error_message = error.get(
            "message",
            "Unknown WhatsApp API error"
        )

        error_type = error.get(
            "type",
            ""
        )

        error_subcode = error.get(
            "error_subcode",
            ""
        )

        error_details = error.get(
            "error_data",
            {}
        )

        result = (
            f"Code: {error_code} | "
            f"Type: {error_type} | "
            f"Subcode: {error_subcode} | "
            f"Message: {error_message}"
        )

        if error_details:
            result += f" | Details: {error_details}"

        return False, result

    except requests.exceptions.Timeout:

        return False, "WhatsApp API request timed out"

    except requests.exceptions.ConnectionError:

        return False, "Could not connect to WhatsApp API"

    except Exception as e:

        return False, f"Request error: {str(e)}"


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
        recent=recent
    )


# =========================================================
# CONTACTS
# =========================================================

@app.route("/contacts", methods=["GET", "POST"])
def contacts():

    # =====================================================
    # CSV IMPORT
    # =====================================================

    if request.method == "POST":

        file = request.files.get("file")

        if not file:

            flash("CSV file select करें.")

            return redirect(
                url_for("contacts")
            )

        try:

            text = file.read().decode(
                "utf-8-sig",
                errors="ignore"
            )

            reader = csv.DictReader(
                io.StringIO(text)
            )

            # CSV header check
            if not reader.fieldnames:

                flash(
                    "CSV file में header नहीं मिला."
                )

                return redirect(
                    url_for("contacts")
                )

            c = db()

            added = 0
            skipped = 0

            for row in reader:

                # -----------------------------------------
                # NAME
                # -----------------------------------------

                name = (
                    row.get("name")
                    or row.get("Name")
                    or row.get("NAME")
                    or ""
                ).strip()

                if not name:
                    name = "Customer"

                # -----------------------------------------
                # PHONE
                # -----------------------------------------

                phone = (
                    row.get("phone")
                    or row.get("Phone")
                    or row.get("PHONE")
                    or row.get("mobile")
                    or row.get("Mobile")
                    or row.get("MOBILE")
                    or ""
                ).strip()

                phone = clean_phone(phone)

                # -----------------------------------------
                # GROUP
                # -----------------------------------------

                group = (
                    row.get("group")
                    or row.get("Group")
                    or row.get("GROUP")
                    or row.get("group_name")
                    or row.get("Group Name")
                    or "General"
                ).strip()

                if not group:
                    group = "General"

                # -----------------------------------------
                # INSERT
                # -----------------------------------------

                if phone:

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

                        # Duplicate phone
                        skipped += 1

                else:

                    skipped += 1

            c.commit()
            c.close()

            flash(
                f"{added} contacts imported. "
                f"{skipped} skipped."
            )

        except Exception as e:

            flash(
                f"CSV import error: {str(e)}"
            )

        return redirect(
            url_for("contacts")
        )

    # =====================================================
    # CONTACT LIST
    # =====================================================

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

@app.route("/campaigns", methods=["GET", "POST"])
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

        if not name:

            flash(
                "Campaign name required."
            )

            return redirect(
                url_for("campaigns")
            )

        if not message:

            flash(
                "Message required."
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

    # Groups
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

    # =====================================================
    # GET CAMPAIGN
    # =====================================================

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

    # =====================================================
    # CHECK API CONFIGURATION
    # =====================================================

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
            "पहले WHATSAPP_ACCESS_TOKEN और "
            "WHATSAPP_PHONE_NUMBER_ID configure करें."
        )

        return redirect(
            url_for("campaigns")
        )

    # =====================================================
    # GET CONTACTS
    # =====================================================

    query = """
        SELECT *
        FROM contacts
    """

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

    # =====================================================
    # NO CONTACTS
    # =====================================================

    if not contacts:

        c.close()

        flash(
            "इस campaign के लिए कोई contact नहीं मिला."
        )

        return redirect(
            url_for("campaigns")
        )

    # =====================================================
    # SEND
    # =====================================================

    sent = 0
    failed = 0

    errors = []

    for contact in contacts:

        # -----------------------------------------------
        # PERSONALIZATION
        # -----------------------------------------------

        body = campaign["message"].replace(
            "{{name}}",
            contact["name"]
        )

        # -----------------------------------------------
        # SEND
        # -----------------------------------------------

        ok, result = send_whatsapp_text(
            contact["phone"],
            body
        )

        # -----------------------------------------------
        # SUCCESS
        # -----------------------------------------------

        if ok:

            sent += 1

        # -----------------------------------------------
        # FAILED
        # -----------------------------------------------

        else:

            failed += 1

            errors.append(
                {
                    "name": contact["name"],
                    "phone": contact["phone"],
                    "error": result
                }
            )

    # =====================================================
    # UPDATE CAMPAIGN STATUS
    # =====================================================

    status = (
        f"Sent {sent}, Failed {failed}"
    )

    c.execute(
        """
        UPDATE campaigns
        SET status=?
        WHERE id=?
        """,
        (
            status,
            cid
        )
    )

    c.commit()
    c.close()

    # =====================================================
    # SHOW RESULT
    # =====================================================

    flash(
        f"Campaign finished: "
        f"{sent} sent, {failed} failed."
    )

    # Show maximum 10 errors
    for item in errors[:10]:

        flash(
            f"FAILED: "
            f"{item['name']} "
            f"({item['phone']}): "
            f"{item['error']}"
        )

    # If more errors
    if len(errors) > 10:

        flash(
            f"{len(errors) - 10} "
            f"additional failures not shown."
        )

    return redirect(
        url_for("campaigns")
    )


# =========================================================
# SETTINGS
# =========================================================

@app.route("/settings")
def settings():

    return render_template(
        "settings.html",
        config_ok=whatsapp_configured()
    )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route("/health")
def health():

    return {
        "status": "ok",
        "whatsapp_configured":
            whatsapp_configured()
    }


# =========================================================
# START APPLICATION
# =========================================================

if __name__ == "__main__":

    init_db()

    port = int(
        os.getenv(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
