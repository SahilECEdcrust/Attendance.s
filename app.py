from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import base64
import uuid
import re
import math

app = Flask(__name__)
sessions = {}

BASE_URL = "https://dcrustm.samarth.edu.in"
LOGIN_URL = f"{BASE_URL}/index.php/site/login"
ATTENDANCE_URL = f"{BASE_URL}/index.php/student-attendance/attendance/index"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

def fetch_timetable(session, dashboard_soup):
    """Auto-discovers and parses the e-Samarth weekly timetable."""
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    schedule = {d: [] for d in days_order}

    candidate_urls = []
    for a in dashboard_soup.find_all('a', href=True):
        href = a['href']
        text = a.get_text(" ", strip=True).lower()
        if any(k in href.lower() or k in text for k in ["timetable", "time table", "routine", "schedule"]):
            full_url = href if href.startswith("http") else BASE_URL + href
            if full_url not in candidate_urls:
                candidate_urls.append(full_url)

    fallback_paths = [
        "/index.php/student/timetable/index",
        "/index.php/student/timetable/view",
        "/index.php/student-timetable/timetable/index",
        "/index.php/timetable/student/index",
        "/index.php/academic/timetable/student",
        "/index.php/course-registration/student-timetable/index"
    ]
    for path in fallback_paths:
        u = BASE_URL + path
        if u not in candidate_urls:
            candidate_urls.append(u)

    for url in candidate_urls:
        try:
            res = session.get(url, headers=HEADERS, timeout=12)
            if res.status_code != 200 or "site/login" in res.url:
                continue

            soup = BeautifulSoup(res.text, 'html.parser')

            for table in soup.find_all('table'):
                rows = table.find_all('tr')
                if len(rows) < 2:
                    continue

                headers = [th.get_text(" ", strip=True) for th in rows[0].find_all(['th', 'td'])]
                found_any = False

                # Case A: Rows = Days, Columns = Time Slots
                for row in rows[1:]:
                    cols = [c.get_text(" | ", strip=True) for c in row.find_all(['td', 'th'])]
                    if not cols:
                        continue
                    first_cell = cols[0].strip().title()
                    matched_day = next((d for d in days_order if d.lower() in first_cell.lower()), None)
                    if matched_day:
                        for idx in range(1, len(cols)):
                            cell_text = cols[idx].strip()
                            if cell_text and cell_text not in ["-", "—", "Break", "Lunch", "N/A", "Free"]:
                                found_any = True
                                slot_time = headers[idx] if idx < len(headers) else f"Period {idx}"
                                schedule[matched_day].append({"time": slot_time, "raw": cell_text})

                # Case B: Columns = Days, Rows = Time Slots
                if not found_any:
                    day_col_map = {}
                    for idx, h in enumerate(headers):
                        matched_day = next((d for d in days_order if d.lower() in h.lower()), None)
                        if matched_day:
                            day_col_map[idx] = matched_day

                    if day_col_map:
                        for row in rows[1:]:
                            cols = [c.get_text(" | ", strip=True) for c in row.find_all(['td', 'th'])]
                            slot_time = cols[0] if cols else "Slot"
                            for col_idx, day_name in day_col_map.items():
                                if col_idx < len(cols):
                                    cell_text = cols[col_idx].strip()
                                    if cell_text and cell_text not in ["-", "—", "Break", "Lunch", "N/A", "Free"]:
                                        found_any = True
                                        schedule[day_name].append({"time": slot_time, "raw": cell_text})

                if found_any:
                    return schedule
        except Exception:
            continue

    return schedule

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

        dash_soup = BeautifulSoup(login_res.text, 'html.parser')

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

        timetable = fetch_timetable(session, dash_soup)

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
            "timetable": timetable
        })

    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"})

if __name__ == '__main__':
    app.run(debug=True)