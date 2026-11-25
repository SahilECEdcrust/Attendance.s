import base64
import os
import re
import time
import uuid
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

SAMARTH_BASE_URL = "https://dcrust.samarth.edu.in"
SAMARTH_LOGIN_URL = f"{SAMARTH_BASE_URL}/index.php/site/login"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# In-memory session store: { session_id: { "session": requests.Session(), "csrf_name": str, "csrf_token": str, "created_at": float } }
SESSIONS = {}
SESSION_TTL_SECONDS = 1800  # 30 minutes


def cleanup_expired_sessions():
    """Removes expired login sessions from memory."""
    now = time.time()
    expired_ids = [
        sid for sid, meta in SESSIONS.items()
        if now - meta.get("created_at", 0) > SESSION_TTL_SECONDS
    ]
    for sid in expired_ids:
        SESSIONS.pop(sid, None)


def extract_csrf_and_captcha(soup, sess):
    """Extracts hidden CSRF token and base64 CAPTCHA image from the Samarth login page."""
    csrf_name = "_csrf"
    csrf_token = ""

    csrf_input = soup.find("input", {"name": re.compile(r"csrf", re.I)})
    if csrf_input:
        csrf_name = csrf_input.get("name") or "_csrf"
        csrf_token = csrf_input.get("value") or ""

    captcha_b64 = None
    captcha_img = (
        soup.find("img", {"id": re.compile(r"captcha", re.I)})
        or soup.find("img", {"src": re.compile(r"captcha", re.I)})
    )
    if captcha_img and captcha_img.get("src"):
        captcha_url = urljoin(SAMARTH_LOGIN_URL, captcha_img["src"].strip())
        try:
            cap_res = sess.get(captcha_url, headers=DEFAULT_HEADERS, timeout=12)
            if cap_res.ok and cap_res.content:
                captcha_b64 = base64.b64encode(cap_res.content).decode("utf-8")
        except Exception:
            captcha_b64 = None

    return csrf_name, csrf_token, captcha_b64


def normalize_status(raw_status):
    """Maps raw attendance cell labels to PRESENT, ABSENT, or LEAVE."""
    val = (raw_status or "").strip().upper()
    if val in ("P", "PRESENT", "1", "Y", "YES", "A/P"):
        return "PRESENT"
    if val in ("A", "ABSENT", "0", "N", "NO"):
        return "ABSENT"
    if val in ("L", "LEAVE", "OD", "ON DUTY", "MEDICAL"):
        return "LEAVE"
    return "PRESENT" if "PRESENT" in val else ("ABSENT" if "ABSENT" in val else "LEAVE")


def classify_subject_tag(title, code=""):
    """Identifies whether a subject entry is Theory, Lab, or Tutorial."""
    combined = f"{title} {code}".lower()
    if any(k in combined for k in ("lab", "practical", "workshop")):
        return "Lab"
    if any(k in combined for k in ("tute", "tutorial")):
        return "Tutorial"
    return "Theory"


def extract_subject_code(text):
    """Extracts an alphanumeric course code such as BSMAT103D or ESCS101D."""
    match = re.search(r"\b([A-Z]{2,6}\d{2,4}[A-Z0-9]*)\b", (text or "").upper())
    return match.group(1) if match else ""


def parse_attendance_logs(soup):
    """Extracts date-wise attendance logs grouped by subject code/title if available."""
    logs_by_key = {}

    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        has_date_col = any("date" in h for h in headers)
        has_status_col = any(k in h for h in headers for k in ("status", "att", "present", "remark"))

        if has_date_col or has_status_col:
            for row in rows[1:]:
                cols = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cols) < 2:
                    continue

                date_val = ""
                time_val = "Scheduled Slot"
                status_val = ""
                sub_key = ""

                for cell in cols:
                    if not date_val and re.search(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b", cell):
                        date_val = cell
                    elif re.search(r"\b\d{1,2}:\d{2}\b", cell):
                        time_val = cell
                    elif cell.strip().upper() in ("P", "A", "L", "PRESENT", "ABSENT", "LEAVE", "OD", "ON DUTY"):
                        status_val = normalize_status(cell)
                    elif not sub_key and len(cell) > 3:
                        code = extract_subject_code(cell)
                        sub_key = code if code else cell.strip().lower()

                if date_val and status_val:
                    entry = {
                        "date": date_val,
                        "time": time_val,
                        "status": status_val,
                    }
                    target_key = sub_key or "__GENERAL__"
                    logs_by_key.setdefault(target_key, []).append(entry)

    return logs_by_key


def parse_timetable(soup):
    """Extracts weekly Monday–Saturday class schedule if present on the page."""
    timetable = {
        "Monday": [],
        "Tuesday": [],
        "Wednesday": [],
        "Thursday": [],
        "Friday": [],
        "Saturday": [],
    }
    days_re = re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\b", re.I)

    for table in soup.find_all("table"):
        table_text = table.get_text(" ", strip=True)
        if not days_re.search(table_text):
            continue

        header_cells = [c.get_text(" ", strip=True) for c in table.find_all("th")]
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 2:
                continue

            day_match = days_re.search(cells[0])
            if day_match:
                day_name = day_match.group(1).capitalize()
                for idx, slot_text in enumerate(cells[1:], start=1):
                    cleaned = " ".join(slot_text.split())
                    if cleaned and cleaned.lower() not in ("-", "--", "free", "break", "lunch", "off"):
                        slot_time = (
                            header_cells[idx]
                            if idx < len(header_cells) and header_cells[idx]
                            else f"Slot {idx}"
                        )
                        timetable[day_name].append({
                            "time": slot_time,
                            "raw": cleaned,
                        })

    return timetable


def parse_attendance_payload(html_text, roll_no):
    """Parses subjects, attendance counts, date logs, and timetable from Samarth HTML."""
    soup = BeautifulSoup(html_text, "html.parser")
    logs_by_key = parse_attendance_logs(soup)
    timetable = parse_timetable(soup)
    subjects = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows[1:]:
            cols = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cols) < 3:
                continue

            # Skip pure timetable rows
            if re.match(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)$", cols[0].strip(), re.I):
                continue

            subject_cell = cols[0]
            code_cell = cols[1] if len(cols) > 1 else ""
            code = extract_subject_code(f"{code_cell} {subject_cell}")

            name_parts = [subject_cell]
            if code and code not in subject_cell.upper():
                name_parts.append(f"({code})")
            full_name = " ".join(p for p in name_parts if p).strip()

            tag = classify_subject_tag(full_name, code)
            attended = 0
            total = 0
            leaves = 0

            for cell in cols[1:]:
                frac_match = re.search(r"(\d+)\s*/\s*(\d+)", cell)
                if frac_match:
                    attended = int(frac_match.group(1))
                    total = int(frac_match.group(2))
                    break

            if total == 0:
                nums = [int(n) for n in re.findall(r"\b\d+\b", " ".join(cols[1:]))]
                if len(nums) >= 2:
                    attended = nums[0]
                    total = max(nums[1], nums[0])

            for cell in cols[1:]:
                leave_match = re.search(r"leave[:\s-]*(\d+)", cell, re.I)
                if leave_match:
                    leaves = int(leave_match.group(1))
                    break

            if full_name and total > 0:
                matched_logs = (
                    logs_by_key.get(code)
                    or logs_by_key.get(full_name.lower())
                    or []
                )
                subjects.append({
                    "code": code,
                    "name": full_name,
                    "tag": tag,
                    "attended": attended,
                    "total": total,
                    "leaves": leaves,
                    "logs": matched_logs,
                })

    return {
        "status": "success",
        "roll_no": roll_no,
        "subjects": subjects,
        "timetable": timetable,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    cleanup_expired_sessions()
    return jsonify({
        "status": "ok",
        "active_sessions": len(SESSIONS),
    })


@app.route("/init-login", methods=["GET"])
def init_login():
    cleanup_expired_sessions()
    session_id = str(uuid.uuid4())
    sess = requests.Session()
    sess.headers.update(DEFAULT_HEADERS)

    try:
        res = sess.get(SAMARTH_LOGIN_URL, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        csrf_name, csrf_token, captcha_b64 = extract_csrf_and_captcha(soup, sess)

        SESSIONS[session_id] = {
            "session": sess,
            "csrf_name": csrf_name,
            "csrf_token": csrf_token,
            "created_at": time.time(),
        }
        return jsonify({
            "status": "success",
            "session_id": session_id,
            "captcha": captcha_b64,
        })
    except Exception as exc:
        SESSIONS[session_id] = {
            "session": sess,
            "csrf_name": "_csrf",
            "csrf_token": "",
            "created_at": time.time(),
        }
        return jsonify({
            "status": "success",
            "session_id": session_id,
            "captcha": None,
            "warning": f"Login session initialized with fallback: {exc}",
        })


@app.route("/get-attendance", methods=["POST"])
def get_attendance():
    cleanup_expired_sessions()
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get("session_id") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    captcha = (payload.get("captcha") or "").strip()

    if not username or not password:
        return jsonify({
            "status": "error",
            "message": "Roll number and password are required.",
        })

    session_entry = SESSIONS.get(session_id)
    if session_entry:
        sess = session_entry["session"]
    else:
        sess = requests.Session()
        sess.headers.update(DEFAULT_HEADERS)

    try:
        login_page_res = sess.get(SAMARTH_LOGIN_URL, timeout=15)
        login_page_res.raise_for_status()
        login_soup = BeautifulSoup(login_page_res.text, "html.parser")
        csrf_name, csrf_token, _ = extract_csrf_and_captcha(login_soup, sess)

        form_data = {
            "LoginForm[username]": username,
            "LoginForm[password]": password,
        }
        if csrf_token:
            form_data[csrf_name] = csrf_token
        if captcha:
            form_data["LoginForm[verifyCode]"] = captcha

        for hidden_inp in login_soup.find_all("input", {"type": "hidden"}):
            inp_name = hidden_inp.get("name")
            if inp_name and inp_name not in form_data:
                form_data[inp_name] = hidden_inp.get("value") or ""

        login_post_res = sess.post(
            SAMARTH_LOGIN_URL,
            data=form_data,
            timeout=20,
            allow_redirects=True,
        )
        login_post_res.raise_for_status()

        body_lower = login_post_res.text.lower()
        if "loginform[password]" in body_lower or "incorrect username or password" in body_lower:
            return jsonify({
                "status": "error",
                "message": "Invalid credentials or captcha. Please verify and try again.",
            })

        combined_html = login_post_res.text
        dashboard_soup = BeautifulSoup(login_post_res.text, "html.parser")

        for link in dashboard_soup.find_all("a", href=True):
            href_lower = link["href"].lower()
            if any(k in href_lower for k in ("attendance", "timetable", "routine", "schedule")):
                sub_url = urljoin(login_post_res.url, link["href"])
                try:
                    sub_res = sess.get(sub_url, timeout=15)
                    if sub_res.ok and sub_res.text:
                        combined_html += "\n" + sub_res.text
                except Exception:
                    continue

        result = parse_attendance_payload(combined_html, username)
        if not result["subjects"]:
            return jsonify({
                "status": "error",
                "message": "Logged in, but no attendance rows were found on the portal.",
            })

        return jsonify(result)

    except requests.Timeout:
        return jsonify({
            "status": "error",
            "message": "DCRUST portal timed out. Please try again.",
        })
    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": f"Failed to fetch attendance: {exc}",
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)