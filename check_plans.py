#!/usr/bin/env python3
"""
מעקב תכניות תכנון — סקריפט בדיקה שבועית
בודק סטטוס תכניות מ-iplan.gov.il ושולח עדכון במייל
"""

import os
import json
import csv
import io
import time
import smtplib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─── הגדרות (מ-GitHub Secrets) ────────────────────────────────────────────────
SHEETS_URL        = os.environ["SHEETS_URL"]          # קישור Google Sheets
PLAN_COL          = int(os.environ.get("PLAN_COL", "2"))    # עמודת מספר תכנית (0-based)
NAME_COL          = int(os.environ.get("NAME_COL", "1"))    # עמודת שם תכנית (0-based), -1 אם אין
HEADER_ROWS       = int(os.environ.get("HEADER_ROWS", "1")) # שורות כותרת לדלג
NOTIFY_EMAIL      = os.environ["NOTIFY_EMAIL"]         # מייל יעד
GMAIL_USER        = os.environ["GMAIL_USER"]           # Gmail שולח
GMAIL_APP_PASS    = os.environ["GMAIL_APP_PASS"]       # App Password של Gmail
SEND_FULL_SUMMARY = os.environ.get("SEND_FULL_SUMMARY", "true").lower() == "true"
STATUSES_FILE     = "statuses.json"  # נשמר ב-GitHub Actions cache / artifact

IPLAN_API = "https://api.iplan.gov.il/open_iplan_api/v1/instructions"

# ─── שלב 1: טעינת מספרי תכניות מ-Google Sheets ───────────────────────────────

def get_sheet_csv_url(sheets_url: str) -> str:
    """הופך קישור רגיל לגיליון ל-URL של CSV"""
    import re
    match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheets_url)
    if not match:
        raise ValueError(f"לא ניתן לחלץ מזהה גיליון מ: {sheets_url}")
    sheet_id = match.group(1)
    # חיפוש gid (מספר גיליון ספציפי אם קיים)
    gid_match = re.search(r'gid=(\d+)', sheets_url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_plan_numbers(sheets_url: str) -> list[dict]:
    """מחזיר רשימת dict עם plan_number ו-name"""
    csv_url = get_sheet_csv_url(sheets_url)
    print(f"📥 טוען גיליון: {csv_url}")
    
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode("utf-8")
    
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)
    
    plans = []
    for i, row in enumerate(rows):
        if i < HEADER_ROWS:
            continue
        if len(row) <= PLAN_COL:
            continue
        plan_num = row[PLAN_COL].strip()
        if not plan_num or not any(c.isdigit() for c in plan_num):
            continue
        name = row[NAME_COL].strip() if NAME_COL >= 0 and len(row) > NAME_COL else ""
        plans.append({"plan_number": plan_num, "name": name})
    
    print(f"✅ נמצאו {len(plans)} תכניות")
    return plans

# ─── שלב 2: בדיקת סטטוס מ-iplan ─────────────────────────────────────────────

def fetch_plan_status(plan_number: str) -> dict:
    """שולף סטטוס תכנית מה-API של iplan"""
    params = urllib.parse.urlencode({"PlanNumber": plan_number})
    url = f"{IPLAN_API}?{params}"
    
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; PlanningTracker/1.0)",
        "Accept": "application/json"
    })
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        
        # מבנה תגובה אפשרי מה-API
        plan_data = None
        if isinstance(data, dict):
            plan_data = data.get("data") or data.get("plan") or data
        elif isinstance(data, list) and len(data) > 0:
            plan_data = data[0]
        
        if not plan_data:
            return {"plan_number": plan_number, "status": "לא נמצא", "name_api": "", "last_updated": "", "error": False}
        
        status = (
            plan_data.get("status_desc") or
            plan_data.get("pl_status_desc") or
            plan_data.get("STATUS_DESC") or
            "לא ידוע"
        )
        name_api = (
            plan_data.get("pl_name") or
            plan_data.get("PLAN_NAME") or
            plan_data.get("name") or ""
        )
        last_updated = (
            plan_data.get("pl_last_update_date_text") or
            plan_data.get("last_update") or ""
        )
        
        return {
            "plan_number": plan_number,
            "status": status,
            "name_api": name_api,
            "last_updated": last_updated,
            "error": False
        }
    
    except urllib.error.HTTPError as e:
        print(f"  ⚠️  HTTP {e.code} עבור {plan_number}")
        return {"plan_number": plan_number, "status": "שגיאת API", "name_api": "", "last_updated": "", "error": True, "error_msg": str(e)}
    except Exception as e:
        print(f"  ⚠️  שגיאה עבור {plan_number}: {e}")
        return {"plan_number": plan_number, "status": "שגיאה", "name_api": "", "last_updated": "", "error": True, "error_msg": str(e)}

# ─── שלב 3: השוואת סטטוסים ───────────────────────────────────────────────────

def load_previous_statuses() -> dict:
    if os.path.exists(STATUSES_FILE):
        with open(STATUSES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}  # הרצה ראשונה — אין השוואה

def save_statuses(statuses: dict):
    with open(STATUSES_FILE, "w", encoding="utf-8") as f:
        json.dump(statuses, f, ensure_ascii=False, indent=2)

CRITICAL_STATUSES = ["הפקדה", "מותנה להפקדה", "התנגדויות", "מופקדת", "להפקדה"]

def is_critical(status: str) -> bool:
    return any(s in status for s in CRITICAL_STATUSES)

# ─── שלב 4: בניית תוכן המייל ─────────────────────────────────────────────────

def build_email(results: list, changed: list, is_first_run: bool, check_date: datetime) -> tuple[str, str]:
    date_str = check_date.strftime("%d/%m/%Y %H:%M")
    
    # נושא
    if is_first_run:
        subject = f"📋 מעקב תכניות — הרצה ראשונה ({len(results)} תכניות) — {check_date.strftime('%d/%m/%Y')}"
    elif changed:
        critical = [p for p in changed if is_critical(p["status_new"])]
        if critical:
            subject = f"⚠️ {len(critical)} תכניות בשלב קריטי! + {len(changed)} שינויים — {check_date.strftime('%d/%m/%Y')}"
        else:
            subject = f"🔔 {len(changed)} שינויי סטטוס בתכניות — {check_date.strftime('%d/%m/%Y')}"
    else:
        subject = f"✅ אין שינויים בתכניות — {check_date.strftime('%d/%m/%Y')}"
    
    # גוף HTML
    html = f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #f5f7fa; color: #1a1f2e; margin: 0; padding: 20px; }}
  .container {{ max-width: 700px; margin: 0 auto; }}
  .header {{ background: #1a1f2e; color: white; padding: 20px 28px; border-radius: 10px 10px 0 0; }}
  .header h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .header p {{ font-size: 13px; opacity: 0.6; margin: 0; }}
  .body {{ background: white; padding: 24px 28px; border-radius: 0 0 10px 10px; border: 1px solid #e2e5ef; border-top: none; }}
  .alert {{ border-radius: 8px; padding: 14px 18px; margin-bottom: 20px; }}
  .alert-critical {{ background: #fef2f2; border: 1.5px solid #fca5a5; color: #dc2626; }}
  .alert-changes {{ background: #fffbeb; border: 1.5px solid #fcd34d; color: #d97706; }}
  .alert-ok {{ background: #ecfdf5; border: 1.5px solid #6ee7b7; color: #059669; }}
  .alert-first {{ background: #eff4ff; border: 1.5px solid #93c5fd; color: #2563eb; }}
  .section-title {{ font-size: 13px; font-weight: 700; color: #8b92a9; text-transform: uppercase; letter-spacing: 0.06em; margin: 20px 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f7f8fb; padding: 9px 12px; text-align: right; font-weight: 600; color: #4a5168; border-bottom: 2px solid #e2e5ef; }}
  td {{ padding: 11px 12px; border-bottom: 1px solid #f0f2f7; vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f9fafb; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }}
  .badge-deposit {{ background: #fffbeb; color: #d97706; }}
  .badge-approval {{ background: #ecfdf5; color: #059669; }}
  .badge-preparation {{ background: #f5f3ff; color: #7c3aed; }}
  .badge-opposition {{ background: #fef2f2; color: #dc2626; }}
  .badge-unknown {{ background: #f7f8fb; color: #8b92a9; }}
  .changed-badge {{ background: #fffbeb; color: #d97706; border: 1px solid #fcd34d; font-size: 10px; padding: 2px 7px; border-radius: 10px; margin-right: 5px; }}
  .critical-row td {{ background: #fef9f0; }}
  .plan-link {{ color: #2563eb; text-decoration: none; font-weight: 600; }}
  .plan-link:hover {{ text-decoration: underline; }}
  .footer {{ text-align: center; font-size: 12px; color: #8b92a9; margin-top: 20px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>מעקב תכניות תכנון — ירושלים</h1>
    <p>בדיקה בוצעה: {date_str} | {len(results)} תכניות נבדקו</p>
  </div>
  <div class="body">
"""
    
    # בנר מצב
    if is_first_run:
        html += f'<div class="alert alert-first">📋 <strong>הרצה ראשונה</strong> — נשמרו סטטוסים ראשוניים. מהשבוע הבא תתקבלנה התראות על שינויים.</div>'
    elif not changed:
        html += f'<div class="alert alert-ok">✅ <strong>אין שינויי סטטוס</strong> השבוע. כל התכניות במצבן הקודם.</div>'
    else:
        critical = [p for p in changed if is_critical(p["status_new"])]
        if critical:
            html += f'<div class="alert alert-critical">⚠️ <strong>{len(critical)} תכניות הגיעו לשלב קריטי</strong> (הפקדה/התנגדויות) — דרוש טיפול!</div>'
        html += f'<div class="alert alert-changes">🔔 <strong>{len(changed)} שינויי סטטוס</strong> זוהו מאז הבדיקה הקודמת.</div>'
    
    # טבלת שינויים (אם יש)
    if changed:
        html += '<div class="section-title">שינויי סטטוס</div>'
        html += '<table><tr><th>מספר תכנית</th><th>שם התכנית</th><th>סטטוס קודם</th><th>סטטוס חדש</th></tr>'
        for p in changed:
            crit_class = 'class="critical-row"' if is_critical(p["status_new"]) else ""
            badge_cls = get_badge_class(p["status_new"])
            crit_icon = "⚠️ " if is_critical(p["status_new"]) else ""
            iplan_url = f"https://www.iplan.gov.il/pages/plan.aspx?PlanNumber={urllib.parse.quote(p['plan_number'])}"
            html += f'''<tr {crit_class}>
              <td><a class="plan-link" href="{iplan_url}" target="_blank">{p["plan_number"]}</a></td>
              <td>{p["name"] or p.get("name_api") or "—"}</td>
              <td><span class="badge badge-unknown">{p["status_old"]}</span></td>
              <td>{crit_icon}<span class="badge {badge_cls}">{p["status_new"]}</span></td>
            </tr>'''
        html += '</table>'
    
    # טבלת כל התכניות
    if SEND_FULL_SUMMARY:
        html += '<div class="section-title">סיכום כל התכניות</div>'
        html += '<table><tr><th>מספר תכנית</th><th>שם התכנית</th><th>סטטוס</th><th>עדכון אחרון</th></tr>'
        for r in results:
            badge_cls = get_badge_class(r["status"])
            changed_badge = '<span class="changed-badge">↑ שינוי</span>' if r.get("changed") else ""
            iplan_url = f"https://www.iplan.gov.il/pages/plan.aspx?PlanNumber={urllib.parse.quote(r['plan_number'])}"
            display_name = r.get("name") or r.get("name_api") or "—"
            html += f'''<tr>
              <td><a class="plan-link" href="{iplan_url}" target="_blank">{r["plan_number"]}</a></td>
              <td>{display_name}</td>
              <td>{changed_badge}<span class="badge {badge_cls}">{r["status"]}</span></td>
              <td style="color:#8b92a9;font-size:12px">{r.get("last_updated") or "—"}</td>
            </tr>'''
        html += '</table>'
    
    html += f'''
  </div>
  <div class="footer">נשלח אוטומטית על ידי סוכן מעקב תכניות | <a href="https://www.iplan.gov.il">iplan.gov.il</a></div>
</div>
</body></html>'''
    
    return subject, html

def get_badge_class(status: str) -> str:
    if not status: return "badge-unknown"
    if any(s in status for s in ["הפקד", "מותנה"]): return "badge-deposit"
    if any(s in status for s in ["אישור", "מאושר"]): return "badge-approval"
    if any(s in status for s in ["התנגד"]): return "badge-opposition"
    if any(s in status for s in ["הכנה", "חדשה", "ביוזמה"]): return "badge-preparation"
    return "badge-unknown"

# ─── שלב 5: שליחת מייל ───────────────────────────────────────────────────────

def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASS)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
    print(f"✉️  מייל נשלח ל-{NOTIFY_EMAIL}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"🔍 מעקב תכניות תכנון — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*50}\n")
    
    # טעינת תכניות מהגיליון
    plans = fetch_plan_numbers(SHEETS_URL)
    if not plans:
        print("❌ לא נמצאו תכניות")
        return
    
    # טעינת סטטוסים קודמים
    prev = load_previous_statuses()
    is_first_run = len(prev) == 0
    if is_first_run:
        print("📌 הרצה ראשונה — לא תהיה השוואת סטטוסים")
    
    # בדיקת כל תכנית
    results = []
    current = {}
    changed = []
    
    for i, plan in enumerate(plans):
        pn = plan["plan_number"]
        print(f"  [{i+1}/{len(plans)}] {pn}", end=" ... ")
        info = fetch_plan_status(pn)
        info["name"] = plan["name"]  # שם מהגיליון
        
        current[pn] = info["status"]
        
        if not is_first_run:
            old_status = prev.get(pn)
            if old_status and old_status != info["status"] and not info["error"]:
                info["changed"] = True
                changed.append({
                    "plan_number": pn,
                    "name": plan["name"],
                    "name_api": info.get("name_api", ""),
                    "status_old": old_status,
                    "status_new": info["status"]
                })
                print(f"⬆️  {old_status} → {info['status']}")
            else:
                info["changed"] = False
                print(f"{info['status']}")
        else:
            info["changed"] = False
            print(f"{info['status']}")
        
        results.append(info)
        time.sleep(0.3)  # כבד ה-API
    
    # שמירת סטטוסים עדכניים
    save_statuses(current)
    print(f"\n💾 סטטוסים נשמרו ({len(current)} תכניות)")
    
    # סיכום
    print(f"\n📊 סיכום:")
    print(f"   תכניות נבדקו: {len(results)}")
    print(f"   שינויים: {len(changed)}")
    print(f"   שגיאות: {sum(1 for r in results if r.get('error'))}")
    
    # בניית ושליחת מייל
    subject, html = build_email(results, changed, is_first_run, datetime.now())
    send_email(subject, html)
    print(f"\n✅ הסתיים בהצלחה")

if __name__ == "__main__":
    main()
