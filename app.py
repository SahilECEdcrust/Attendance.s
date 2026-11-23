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
            "subjects": subjects
        })

    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"})

if __name__ == '__main__':
    app.run(debug=True)