from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import base64
import uuid
import re
import math

app = Flask(__name__)
sessions = {}

BASE_URL = "https://dcrustm.samarth.edu.in"
LOGIN_URL = f"{BASE_URL}/index.php/site/login"
ATTENDANCE_URL = f"{BASE_URL}/index.php/student-attendance/attendance/index"
TIMETABLE_PAGE_URL = f"{BASE_URL}/index.php/student-attendance/attendance/view-time-table"
CLASSES_API_URL = f"{BASE_URL}/index.php/student-attendance/attendance-api/get-classes-of-student"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": TIMETABLE_PAGE_URL
}

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/init-login', methods=['GET'])
def init_login():
    try:
        session = requests.Session()
        res = session.get(LOGIN_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')

        csrf_input = soup.find('input', {'name': '_csrf'})
        csrf_token = csrf_input['value'] if csrf_input else ""

        captcha_img = soup.find('img', {'id': 'loginform-verifycode-image'})
        captcha_base64 = None

        if captcha_img and captcha_img.get('src'):
            img_url = BASE_URL + captcha_img['src']
            img_res = session.get(img_url, headers=HEADERS, timeout=15)
            captcha_base64 = base64.b64encode(img_res.content).decode('utf-8')

        session_id = str(uuid.uuid4())
        sessions[session_id] = {"session": session, "csrf": csrf_token}

        return jsonify({"status": "success", "session_id": session_id, "captcha": captcha_base64})
    except Exception as e:
        return jsonify({"status": "error", "message": f"DCRUST server unreachable: {str(e)}"})

def parse_date_rows(soup, subject_name, tag):
    """Flexibly extracts [Date, Time, Status] rows regardless of column order or date format."""
    logs = []
    date_pattern = re.compile(r'(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})')
    status_keywords = {"PRESENT", "ABSENT", "LEAVE", "LATE", "MEDICAL"}

    for table in soup.find_all('table'):
        for row in table.find_all('tr'):
            cols = [c.get_text(" ", strip=True) for c in row.find_all('td')]
            if len(cols) < 3:
                continue

            found_date = None
            found_status = None
            found_time = ""

            for col in cols:
                upper_col = col.upper().strip()
                if upper_col in status_keywords:
                    found_status = upper_col
                elif date_pattern.search(col) and not found_date:
                    found_date = date_pattern.search(col).group(1)
                elif (":" in col or "AM" in upper_col or "PM" in upper_col) and not found_time:
                    found_time = col

            if found_date and found_status:
                logs.append({
                    "date": found_date,
                    "time": found_time or "Scheduled Slot",
                    "status": found_status,
                    "subject": subject_name,
                    "tag": tag
                })
    return logs

def fetch_single_subject(session, row_data):
    """Fetches summary + all paginated date logs for one subject."""
    try:
        detail_res = session.get(row_data["url"], headers=HEADERS, timeout=15)
        soup = BeautifulSoup(detail_res.text, 'html.parser')
        detail_text = soup.get_text(" ", strip=True)

        total_match = re.search(r'Total Classes Taken:\s*(\d+)', detail_text)
        present_match = re.search(r'Present Count:\s*(\d+)', detail_text)
        leave_match = re.search(r'Leave Count:\s*(\d+)', detail_text)
        percent_match = re.search(r'Present Percentage:\s*([\d.]+)%', detail_text)

        if total_match and present_match:
            total_taken = int(total_match.group(1))
            present_count = int(present_match.group(1))
            leave_count = int(leave_match.group(1)) if leave_match else 0
            effective_total = max(total_taken - leave_count, 1)
            exact_percent = float(percent_match.group(1)) if percent_match else round((present_count / effective_total) * 100, 2)

            all_logs = parse_date_rows(soup, row_data["name"], row_data["tag"])

            total_pages = math.ceil(total_taken / 20)
            for page_num in range(2, total_pages + 1):
                sep = "&" if "?" in row_data["url"] else "?"
                page_url = f"{row_data['url']}{sep}page={page_num}"
                p_res = session.get(page_url, headers=HEADERS, timeout=15)
                p_soup = BeautifulSoup(p_res.text, 'html.parser')
                all_logs.extend(parse_date_rows(p_soup, row_data["name"], row_data["tag"]))

            return {
                "code": row_data["code"],
                "name": row_data["name"],
                "tag": row_data["tag"],
                "attended": present_count,
                "total": effective_total,
                "leaves": leave_count,
                "percentage": exact_percent,
                "logs": all_logs
            }
    except Exception:
        return None
    return None

def extract_student_token(html_text):
    """Extracts student_id token from HTML/JS."""
    if not html_text:
        return None
    patterns = [
        r'get-classes-of-student[^"\'\s<>]*student_id(?:=|%3D)([A-Za-z0-9_-]{16,})',
        r'student[_-]?id["\']?\s*[:=]\s*["\']([A-Za-z0-9_-]{16,})["\']',
        r'data-student-id=["\']([A-Za-z0-9_-]{16,})["\']',
        r'\b([a-f0-9]{40,80})\b',
    ]
    for pat in patterns:
        m = re.search(pat, html_text, re.I)
        if m:
            return m.group(1)
    return None

def parse_dt(val):
    """Parses ISO or standard datetime strings into datetime object."""
    if not val or not isinstance(val, str):
        return None
    cleaned = val.replace("Z", "+00:00").strip()
    try:
        return datetime.fromisoformat(cleaned)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(cleaned[:19], fmt)
            except Exception:
                continue
    return None

def fetch_timetable(session, att_html="", login_html=""):
    """Fetches student classes from e-Samarth attendance-api/get-classes-of-student."""
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    schedule = {d: [] for d in days_order}
    debug = {"token_found": False, "api_status": None, "count": 0, "sample_keys": []}

    try:
        tt_res = session.get(TIMETABLE_PAGE_URL, headers=HEADERS, timeout=12)
        combined_html = (tt_res.text or "") + "\n" + (att_html or "") + "\n" + (login_html or "")
        student_token = extract_student_token(combined_html)

        if not student_token:
            debug["note"] = "student_id not found in HTML"
            print("TT_DEBUG:", debug, flush=True)
            return schedule, debug

        debug["token_found"] = True

        api_res = session.get(
            CLASSES_API_URL,
            params={"student_id": student_token},
            headers=API_HEADERS,
            timeout=15
        )
        debug["api_status"] = api_res.status_code

        if api_res.status_code != 200:
            debug["note"] = api_res.text[:150]
            print("TT_DEBUG:", debug, flush=True)
            return schedule, debug

        data = api_res.json()
        payload_items = []
        if isinstance(data, list):
            payload_items = data
        elif isinstance(data, dict):
            for key in ("data", "classes", "events", "timetable", "records", "result"):
                if isinstance(data.get(key), list):
                    payload_items = data[key]
                    break

        debug["count"] = len(payload_items)
        if payload_items and isinstance(payload_items[0], dict):
            debug["sample_keys"] = list(payload_items[0].keys())

        seen = {d: set() for d in days_order}

        for item in payload_items:
            if not isinstance(item, dict):
                continue

            ext = item.get("extendedProps") if isinstance(item.get("extendedProps"), dict) else {}

            start_dt = parse_dt(
                item.get("start") or item.get("start_time") or item.get("startTime") or item.get("from_time") or item.get("date") or ext.get("start")
            )
            end_dt = parse_dt(
                item.get("end") or item.get("end_time") or item.get("endTime") or item.get("to_time") or ext.get("end")
            )

            day_name = None
            raw_day = str(item.get("day") or item.get("day_name") or item.get("weekday") or ext.get("day") or "").strip().title()
            if raw_day in schedule:
                day_name = raw_day
            elif start_dt:
                wk_name = start_dt.strftime("%A")
                if wk_name in schedule:
                    day_name = wk_name

            if not day_name:
                continue

            if start_dt and end_dt and (start_dt.hour or start_dt.minute or end_dt.hour or end_dt.minute):
                time_label = f"{start_dt.strftime('%I:%M %p')} - {end_dt.strftime('%I:%M %p')}"
            elif start_dt and (start_dt.hour or start_dt.minute):
                time_label = start_dt.strftime("%I:%M %p")
            else:
                time_label = str(item.get("time") or item.get("slot") or item.get("timing") or ext.get("time") or "Scheduled Slot")

            title = (
                item.get("title")
                or item.get("course_name")
                or item.get("subject_name")
                or item.get("course")
                or item.get("name")
                or item.get("label")
                or ext.get("title")
                or ext.get("course_name")
                or ext.get("subject")
                or ""
            )
            room = (
                item.get("room")
                or item.get("room_no")
                or item.get("venue")
                or item.get("location")
                or ext.get("room")
                or ext.get("venue")
                or ""
            )

            title_clean = BeautifulSoup(str(title), "html.parser").get_text(" - ", strip=True)
            if room and str(room).strip() and str(room).strip().lower() not in title_clean.lower():
                raw_label = f"{title_clean} ({str(room).strip()})" if title_clean else str(room).strip()
            else:
                raw_label = title_clean

            if not raw_label:
                continue

            dedup_key = f"{time_label}|{raw_label}"
            if dedup_key not in seen[day_name]:
                seen[day_name].add(dedup_key)
                schedule[day_name].append({
                    "time": time_label,
                    "raw": raw_label,
                    "_sort": start_dt.strftime("%H:%M") if start_dt else time_label
                })

        for d in days_order:
            schedule[d].sort(key=lambda x: x.get("_sort", ""))
            for entry in schedule[d]:
                entry.pop("_sort", None)

        print("TT_DEBUG:", debug, flush=True)
    except Exception as e:
        debug["error"] = str(e)
        print("TT_DEBUG:", debug, flush=True)

    return schedule, debug

@app.route('/get-attendance', methods=['POST'])
def get_attendance():
    data = request.json
    session_id = data.get('session_id')
    username = data.get('username')
    password = data.get('password')
    captcha = data.get('captcha', '')

    if session_id not in sessions:
        return jsonify({"status": "error", "message": "Session expired. Refreshing..."})

    user_data = sessions.pop(session_id)
    session = user_data["session"]
    csrf_token = user_data["csrf"]

    payload = {
        "_csrf": csrf_token,
        "LoginForm[username]": username,
        "LoginForm[password]": password,
        "LoginForm[verifyCode]": captcha
    }

    try:
        login_res = session.post(LOGIN_URL, data=payload, headers=HEADERS, timeout=15)
        if "site/login" in login_res.url and "Logout" not in login_res.text:
            return jsonify({"status": "error", "message": "Invalid Roll No, Password, or CAPTCHA."})

        att_res = session.get(ATTENDANCE_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(att_res.text, 'html.parser')

        tasks = []
        table = soup.find('table')
        if table:
            for row in table.find_all('tr')[1:]:
                cols = row.find_all('td')
                if len(cols) >= 6:
                    class_type = cols[1].get_text(strip=True)
                    course_full = cols[2].get_text(strip=True)

                    code = course_full.split(":")[0].strip() if ":" in course_full else ""
                    clean_name = course_full.split(":")[-1].strip().title() if ":" in course_full else course_full
                    tag = "Lab" if "Lab" in class_type else ("Tute" if "Tute" in class_type else "Theory")

                    view_link = cols[-1].find('a')
                    if view_link and view_link.get('href'):
                        href = view_link['href']
                        detail_url = href if href.startswith("http") else BASE_URL + href
                        tasks.append({"code": code, "name": clean_name, "tag": tag, "url": detail_url})

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda t: fetch_single_subject(session, t), tasks))

        subjects = [r for r in results if r is not None]
        if not subjects:
            return jsonify({"status": "error", "message": "Could not parse attendance records."})

        timetable, tt_debug = fetch_timetable(session, att_res.text, login_res.text)

        total_attended = sum(s["attended"] for s in subjects)
        total_classes = sum(s["total"] for s in subjects)
        overall_percent = round((total_attended / max(total_classes, 1)) * 100, 2)

        return jsonify({
            "status": "success",
            "roll_no": username,
            "overall": {
                "attended": total_attended,
                "total": total_classes,
                "percentage": overall_percent
            },
            "subjects": subjects,
            "timetable": timetable,
            "tt_debug": tt_debug
        })

    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"})

if __name__ == '__main__':
    app.run(debug=True)