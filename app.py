import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import os
import re
import uuid

from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)
sessions = {}

BASE_URL = "https://dcrustm.samarth.edu.in"
LOGIN_URL = f"{BASE_URL}/index.php/site/login"
ATTENDANCE_URL = f"{BASE_URL}/index.php/student-attendance/attendance/index"
TIMETABLE_PAGE_URL = f"{BASE_URL}/index.php/student-attendance/attendance/view-time-table"
CLASSES_API_URL = f"{BASE_URL}/index.php/student-attendance/attendance-api/get-classes-of-student"

IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": TIMETABLE_PAGE_URL,
}


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "active_sessions": len(sessions)})


@app.route("/init-login", methods=["GET"])
def init_login():
    try:
        session = requests.Session()
        res = session.get(LOGIN_URL, headers=HEADERS, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")

        csrf_input = soup.find("input", {"name": "_csrf"})
        csrf_token = csrf_input["value"] if csrf_input and csrf_input.get("value") else ""

        captcha_img = soup.find("img", {"id": "loginform-verifycode-image"})
        captcha_base64 = None

        if captcha_img and captcha_img.get("src"):
            src = captcha_img["src"].strip()
            img_url = (
                src
                if src.startswith("http")
                else f"{BASE_URL}{src if src.startswith('/') else '/' + src}"
            )
            img_res = session.get(img_url, headers=HEADERS, timeout=15)
            if img_res.ok and img_res.content:
                captcha_base64 = base64.b64encode(img_res.content).decode("utf-8")

        session_id = str(uuid.uuid4())
        sessions[session_id] = {"session": session, "csrf": csrf_token}

        return jsonify({
            "status": "success",
            "session_id": session_id,
            "captcha": captcha_base64,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"DCRUST server unreachable: {str(e)}"})


def classify_class_tag(class_type, course_full):
    """Classifies row into Lab, Tutorial, or Theory."""
    text = f"{class_type} {course_full}".lower()
    if re.search(r"\b(lab|practical|workshop)\b", text):
        return "Lab"
    if re.search(r"\b(tute|tutorial)\b", text):
        return "Tutorial"
    return "Theory"


def parse_date_rows(soup, subject_name, tag):
    """Flexibly extracts [Date, Time, Status] rows regardless of column order or date format."""
    logs = []
    date_pattern = re.compile(
        r"(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})"
    )
    status_keywords = {"PRESENT", "ABSENT", "LEAVE", "LATE", "MEDICAL"}

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
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
                    "tag": tag,
                })
    return logs


def fetch_single_subject(session, row_data):
    """Fetches summary + all paginated date logs for one subject."""
    try:
        detail_res = session.get(row_data["url"], headers=HEADERS, timeout=15)
        detail_res.raise_for_status()
        soup = BeautifulSoup(detail_res.text, "html.parser")
        detail_text = soup.get_text(" ", strip=True)

        total_match = re.search(r"Total Classes Taken:\s*(\d+)", detail_text)
        present_match = re.search(r"Present Count:\s*(\d+)", detail_text)
        leave_match = re.search(r"Leave Count:\s*(\d+)", detail_text)
        percent_match = re.search(r"Present Percentage:\s*([\d.]+)%", detail_text)

        if total_match and present_match:
            total_taken = int(total_match.group(1))
            present_count = int(present_match.group(1))
            leave_count = int(leave_match.group(1)) if leave_match else 0
            effective_total = max(total_taken - leave_count, 1)
            exact_percent = (
                float(percent_match.group(1))
                if percent_match
                else round((present_count / effective_total) * 100, 2)
            )

            all_logs = parse_date_rows(soup, row_data["name"], row_data["tag"])

            total_pages = math.ceil(total_taken / 20)
            for page_num in range(2, total_pages + 1):
                sep = "&" if "?" in row_data["url"] else "?"
                page_url = f"{row_data['url']}{sep}page={page_num}"
                p_res = session.get(page_url, headers=HEADERS, timeout=15)
                if p_res.ok:
                    p_soup = BeautifulSoup(p_res.text, "html.parser")
                    all_logs.extend(parse_date_rows(p_soup, row_data["name"], row_data["tag"]))

            display_name = (
                f"{row_data['name']} ({row_data['class_name']})"
                if row_data.get("class_name")
                else row_data["name"]
            )

            return {
                "code": row_data["code"],
                "name": display_name,
                "tag": row_data["tag"],
                "attended": present_count,
                "total": effective_total,
                "leaves": leave_count,
                "percentage": exact_percent,
                "logs": all_logs,
            }
    except Exception:
        return None
    return None


def extract_student_token(html_text):
    """Extracts student_id token from HTML/JS."""
    if not html_text:
        return None
    patterns = [
        r"get-classes-of-student[^\"'\s<>]*student_id(?:=|%3D)([A-Za-z0-9_-]{12,})",
        r"student[_-]?id[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_-]{12,})[\"']",
        r"data-student-id=[\"']([A-Za-z0-9_-]{12,})[\"']",
        r"\b([a-f0-9]{32,80})\b",
    ]
    for pat in patterns:
        m = re.search(pat, html_text, re.I)
        if m:
            return m.group(1)
    return None


def parse_dt(val):
    """Parses Unix timestamps (int/float/numeric str) or ISO/date strings into IST datetime."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        ts = val / 1000.0 if val > 1e11 else float(val)
        return datetime.fromtimestamp(ts, tz=IST)
    if isinstance(val, str):
        s = val.strip()
        if re.fullmatch(r"\d{10,13}", s):
            ts = int(s) / 1000.0 if len(s) > 11 else float(s)
            return datetime.fromtimestamp(ts, tz=IST)
        cleaned = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
            return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
        except Exception:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y %H:%M",
                "%Y-%m-%d",
            ):
                try:
                    return datetime.strptime(cleaned[:19], fmt).replace(tzinfo=IST)
                except Exception:
                    continue
    return None


DAY_MAP = {
    "mon": "Monday",
    "monday": "Monday",
    "tue": "Tuesday",
    "tues": "Tuesday",
    "tuesday": "Tuesday",
    "wed": "Wednesday",
    "wednesday": "Wednesday",
    "thu": "Thursday",
    "thur": "Thursday",
    "thurs": "Thursday",
    "thursday": "Thursday",
    "fri": "Friday",
    "friday": "Friday",
    "sat": "Saturday",
    "saturday": "Saturday",
}


def canonical_day(text):
    cleaned = re.sub(r"[^a-z]", "", (text or "").lower())
    return DAY_MAP.get(cleaned)


def is_empty_slot(val):
    low = (val or "").strip().lower()
    return not low or low in ("-", "--", "---", "free", "break", "lunch", "na", "n/a", "nil", "off")


def parse_timetable_tables(html_text):
    """Fallback HTML table parser for timetable pages."""
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    schedule = {d: [] for d in days_order}
    if not html_text:
        return schedule

    soup = BeautifulSoup(html_text, "html.parser")
    for table in soup.find_all("table"):
        grid = [
            [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        grid = [r for r in grid if r]
        if len(grid) < 2:
            continue

        header_days = {
            idx: d for idx, cell in enumerate(grid[0]) if (d := canonical_day(cell))
        }
        if len(header_days) >= 3:
            for row in grid[1:]:
                slot_time = row[0] if row else "Scheduled Slot"
                for idx, day_name in header_days.items():
                    if idx < len(row) and not is_empty_slot(row[idx]):
                        schedule[day_name].append({"time": slot_time, "raw": row[idx]})
            if any(schedule.values()):
                return schedule

        col0_days = [canonical_day(r[0]) for r in grid[1:] if r]
        if sum(1 for d in col0_days if d) >= 2:
            headers = grid[0]
            for row in grid[1:]:
                day_name = canonical_day(row[0]) if row else None
                if not day_name:
                    continue
                for col_idx in range(1, len(row)):
                    if not is_empty_slot(row[col_idx]):
                        slot_time = (
                            headers[col_idx]
                            if col_idx < len(headers) and headers[col_idx]
                            else f"Slot {col_idx}"
                        )
                        schedule[day_name].append({"time": slot_time, "raw": row[col_idx]})
            if any(schedule.values()):
                return schedule

    return schedule


def fetch_timetable(session, att_html="", login_html=""):
    """Fetches weekly schedule (Mon-Sat) from get-classes-of-student API with HTML fallback."""
    days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    schedule = {d: [] for d in days_order}

    try:
        tt_res = session.get(TIMETABLE_PAGE_URL, headers=HEADERS, timeout=12)
        tt_html = tt_res.text or ""
        combined_html = tt_html + "\n" + (att_html or "") + "\n" + (login_html or "")
        student_token = extract_student_token(combined_html)

        if student_token:
            api_res = session.get(
                CLASSES_API_URL,
                params={"student_id": student_token},
                headers=API_HEADERS,
                timeout=15,
            )
            if api_res.status_code == 200:
                data = api_res.json()
                payload_items = []
                if isinstance(data, list):
                    payload_items = data
                elif isinstance(data, dict):
                    for key in ("data", "classes", "events", "timetable", "records", "result"):
                        if isinstance(data.get(key), list):
                            payload_items = data[key]
                            break

                seen = {d: set() for d in days_order}

                for item in payload_items:
                    if not isinstance(item, dict):
                        continue

                    start_dt = parse_dt(
                        item.get("start") or item.get("start_time") or item.get("date")
                    )
                    end_dt = parse_dt(item.get("end") or item.get("end_time"))

                    if not start_dt:
                        continue

                    day_name = start_dt.strftime("%A")
                    if day_name not in schedule:
                        continue

                    if end_dt:
                        time_label = f"{start_dt.strftime('%I:%M %p')} - {end_dt.strftime('%I:%M %p')}"
                    else:
                        time_label = start_dt.strftime("%I:%M %p")

                    slot_title = str(item.get("title") or "").strip()
                    desc = str(item.get("desc") or item.get("description") or "").strip()

                    if ":" in desc:
                        code_part, sub_part = [p.strip() for p in desc.split(":", 1)]
                        sub_title = f"{sub_part.title()} ({code_part})"
                    elif desc:
                        sub_title = desc.title()
                    else:
                        sub_title = ""

                    raw_label = (
                        f"{sub_title} • {slot_title}"
                        if (sub_title and slot_title)
                        else (sub_title or slot_title)
                    )
                    if not raw_label:
                        continue

                    dedup_key = f"{time_label}|{raw_label}"
                    if dedup_key not in seen[day_name]:
                        seen[day_name].add(dedup_key)
                        schedule[day_name].append({
                            "time": time_label,
                            "raw": raw_label,
                            "_sort": start_dt.strftime("%H:%M"),
                        })

                for d in days_order:
                    schedule[d].sort(key=lambda x: x.get("_sort", ""))
                    for entry in schedule[d]:
                        entry.pop("_sort", None)

        if not any(schedule.values()):
            schedule = parse_timetable_tables(tt_html)

    except Exception:
        pass

    return schedule


def extract_enrolled_tasks(soup):
    """Extracts all enrolled Lab, Tutorial, and Theory rows (including English)."""
    tasks = []
    seen_urls = set()

    for row in soup.find_all("tr"):
        view_link = row.find("a", href=lambda h: h and "view-attendance" in h)
        if not view_link:
            continue

        href = view_link["href"].strip()
        detail_url = (
            href
            if href.startswith("http")
            else f"{BASE_URL}{href if href.startswith('/') else '/' + href}"
        )
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        td_texts = [" ".join(td.get_text(" ", strip=True).split()) for td in row.find_all("td")]
        non_action_cells = [
            t for idx_c, t in enumerate(td_texts)
            if not (idx_c == 0 and re.fullmatch(r"\d+", t))
            and t.lower() not in ("view", "view attendance")
            and not re.fullmatch(r"20\d{2}\s*-\s*20\d{2}", t)
            and "semester" not in t.lower()
        ]

        class_name = non_action_cells[0] if len(non_action_cells) >= 1 else ""
        course_full = ""
        for cell_val in non_action_cells:
            if ":" in cell_val or re.search(r"\b[A-Z]{2,6}\d{2,4}[A-Z0-9]*\b", cell_val.upper()):
                course_full = cell_val
                break
        if not course_full and len(non_action_cells) >= 2:
            course_full = non_action_cells[1]

        if ":" in course_full:
            left, right = course_full.split(":", 1)
            code = re.sub(r"\s+", "", left.strip().upper())
            clean_name = right.strip().title()
        else:
            code_match = re.search(r"\b([A-Z]{2,6}\d{2,4}[A-Z0-9]*)\b", course_full.upper())
            code = code_match.group(1) if code_match else ""
            clean_name = course_full.strip().title() if course_full else class_name

        tag = classify_class_tag(class_name, course_full)

        tasks.append({
            "code": code,
            "name": clean_name,
            "class_name": class_name,
            "tag": tag,
            "url": detail_url,
        })

    return tasks


@app.route("/get-attendance", methods=["POST"])
def get_attendance():
    data = request.json or {}
    session_id = data.get("session_id")
    username = data.get("username")
    password = data.get("password")
    captcha = data.get("captcha", "")

    if session_id not in sessions:
        return jsonify({"status": "error", "message": "Session expired. Refreshing..."})

    user_data = sessions.pop(session_id)
    session = user_data["session"]
    csrf_token = user_data["csrf"]

    payload = {
        "_csrf": csrf_token,
        "LoginForm[username]": username,
        "LoginForm[password]": password,
        "LoginForm[verifyCode]": captcha,
    }

    try:
        login_res = session.post(LOGIN_URL, data=payload, headers=HEADERS, timeout=15)
        if "site/login" in login_res.url and "Logout" not in login_res.text:
            return jsonify({"status": "error", "message": "Invalid Roll No, Password, or CAPTCHA."})

        att_res = session.get(ATTENDANCE_URL, headers=HEADERS, timeout=15)
        att_res.raise_for_status()
        soup = BeautifulSoup(att_res.text, "html.parser")

        tasks = extract_enrolled_tasks(soup)

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda t: fetch_single_subject(session, t), tasks))

        subjects = [r for r in results if r is not None]
        if not subjects:
            return jsonify({"status": "error", "message": "Could not parse attendance records."})

        timetable = fetch_timetable(session, att_res.text, login_res.text)

        total_attended = sum(s["attended"] for s in subjects)
        total_classes = sum(s["total"] for s in subjects)
        overall_percent = round((total_attended / max(total_classes, 1)) * 100, 2)

        return jsonify({
            "status": "success",
            "roll_no": username,
            "overall": {
                "attended": total_attended,
                "total": total_classes,
                "percentage": overall_percent,
            },
            "subjects": subjects,
            "timetable": timetable,
        })

    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)