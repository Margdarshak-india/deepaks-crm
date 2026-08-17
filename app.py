from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3, csv, io, os, requests

app=Flask(__name__)
app.secret_key="change-this-secret-key"
DB="deepaks_crm.db"

def db():
    c=sqlite3.connect(DB)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    c=db()
    c.execute("""CREATE TABLE IF NOT EXISTS contacts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        group_name TEXT DEFAULT 'General')""")
    c.execute("""CREATE TABLE IF NOT EXISTS campaigns(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        message TEXT NOT NULL,
        group_name TEXT,
        status TEXT DEFAULT 'Draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.commit(); c.close()

def whatsapp_configured():
    return bool(os.getenv("WHATSAPP_ACCESS_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID"))

def send_whatsapp_text(phone, body):
    token=os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        return False, "API credentials missing"
    url=f"https://graph.facebook.com/v23.0/{phone_id}/messages"
    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}
    payload={"messaging_product":"whatsapp","to":phone,"type":"text",
             "text":{"preview_url":False,"body":body}}
    try:
        r=requests.post(url,headers=headers,json=payload,timeout=20)
        return (True,r.json()) if r.ok else (False,r.text)
    except Exception as e:
        return False,str(e)

@app.route("/")
def dashboard():
    c=db()
    contacts=c.execute("SELECT COUNT(*) n FROM contacts").fetchone()["n"]
    campaigns=c.execute("SELECT COUNT(*) n FROM campaigns").fetchone()["n"]
    recent=c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 10").fetchall()
    c.close()
    return render_template("dashboard.html",contacts=contacts,campaigns=campaigns,recent=recent)

@app.route("/contacts",methods=["GET","POST"])
def contacts():
    if request.method=="POST":
        f=request.files.get("file")
        if not f:
            flash("CSV file select करें.")
            return redirect(url_for("contacts"))
        text=f.read().decode("utf-8-sig",errors="ignore")
        reader=csv.DictReader(io.StringIO(text))
        c=db(); added=0
        for row in reader:
            name=(row.get("name") or row.get("Name") or "").strip() or "Customer"
            phone=(row.get("phone") or row.get("Phone") or row.get("mobile") or row.get("Mobile") or "").strip()
            group=(row.get("group") or row.get("Group") or "General").strip() or "General"
            if phone:
                try:
                    c.execute("INSERT INTO contacts(name,phone,group_name) VALUES(?,?,?)",(name,phone,group))
                    added+=1
                except sqlite3.IntegrityError: pass
        c.commit(); c.close()
        flash(f"{added} contacts imported.")
        return redirect(url_for("contacts"))
    c=db(); rows=c.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall(); c.close()
    return render_template("contacts.html",rows=rows)

@app.route("/campaigns",methods=["GET","POST"])
def campaigns():
    if request.method=="POST":
        c=db()
        c.execute("INSERT INTO campaigns(name,message,group_name) VALUES(?,?,?)",
                  (request.form["name"].strip(),request.form["message"].strip(),
                   request.form.get("group_name","").strip()))
        c.commit(); c.close()
        flash("Campaign saved as Draft.")
        return redirect(url_for("campaigns"))
    c=db()
    rows=c.execute("SELECT * FROM campaigns ORDER BY id DESC").fetchall()
    groups=[r["group_name"] for r in c.execute("SELECT DISTINCT group_name FROM contacts ORDER BY group_name").fetchall()]
    c.close()
    return render_template("campaigns.html",rows=rows,groups=groups)

@app.route("/campaign/<int:cid>/send",methods=["POST"])
def send_campaign(cid):
    c=db()
    campaign=c.execute("SELECT * FROM campaigns WHERE id=?",(cid,)).fetchone()
    if not campaign:
        c.close(); flash("Campaign not found."); return redirect(url_for("campaigns"))
    if not whatsapp_configured():
        c.execute("UPDATE campaigns SET status='API Not Configured' WHERE id=?",(cid,))
        c.commit(); c.close()
        flash("पहले WHATSAPP_ACCESS_TOKEN और WHATSAPP_PHONE_NUMBER_ID configure करें.")
        return redirect(url_for("campaigns"))
    q="SELECT * FROM contacts"; params=()
    if campaign["group_name"]:
        q+=" WHERE group_name=?"; params=(campaign["group_name"],)
    contacts=c.execute(q,params).fetchall()
    sent=failed=0
    for contact in contacts:
        body=campaign["message"].replace("{{name}}",contact["name"])
        ok,_=send_whatsapp_text(contact["phone"],body)
        sent+=int(ok); failed+=int(not ok)
    c.execute("UPDATE campaigns SET status=? WHERE id=?",(f"Sent {sent}, Failed {failed}",cid))
    c.commit(); c.close()
    flash(f"Campaign finished: {sent} sent, {failed} failed.")
    return redirect(url_for("campaigns"))

@app.route("/settings")
def settings():
    return render_template("settings.html",config_ok=whatsapp_configured())

if __name__=="__main__":
    init_db()
    app.run(debug=True)
