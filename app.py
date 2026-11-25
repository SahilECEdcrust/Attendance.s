import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time
import uuid
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request
import requests

app = Flask(__name__)

SAMARTH_BASE_URL = "https://dcrustm.samarth.edu.in"
SAMARTH_LOGIN_URL = f"{SAMARTH_BASE_URL}/"
ATTENDANCE_INDEX_URL = f"{SAMARTH_BASE_URL}/index.php/student-attendance/attendance/index"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

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


def extract_csrf_and_captcha(soup, sess, base_url):
    """Extracts hidden CSRF token and base64 CAPTCHA image from the login page."""
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
        captcha_url = urljoin(base_url, captcha_img["src"].strip())
        try:
            cap_res = sess.get(captcha_url, headers=DEFAULT_HEADERS, timeout=12)
            if cap_res.ok and cap_res.content:
                captcha_b64 = base64.b64encode(cap_res.content).decode("utf-8")
        except Exception:
            captcha_b64 = None

    return csrf_name, csrf_token, captcha_b64


def normalize_status(raw_status):
    """Maps raw attendance status text to PRESENT, ABSENT, or LEAVE."""
    val = (raw_status or "").strip().upper()
    if "LEAVE" in val or val in ("L", "OD", "ON DUTY", "MEDICAL"):
        return "LEAVE"
    if "ABSENT" in val or val in ("A", "0", "N", "NO"):
        return "ABSENT"
    if "PRESENT" in val or val in ("P", "1", "Y", "YES", "A/P"):
        return "PRESENT"
    return "PRESENT"


def classify_subject_tag(class_name, course_name):
    """Determines whether an enrolled class is Lab, Tutorial, or Theory."""
    combined = f"{class_name} {course_name}".lower()
    if re.search(r"\b(lab|practical|workshop)\b", combined):
        return "Lab"
    if re.search(r"\b(tute|tutorial)\b", combined):
        return "Tutorial"
    return "Theory"


def extract_code_and_title(course_name):
    """Splits Samarth course string like 'BSPHY105D : QUANTUM MECHANICS FOR ENGINEERS'."""
    text = " ".join((course_name or "").split())
    if " : " in text:
        left, right = text.split(" : ", 1)
        code = re.sub(r"\s+", "", left.strip().upper())
        title = right.strip().title()
        return code, title

    code_match = re.search(r"\b([A-Z]{2,6}\d{2,4}[A-Z0-9]*)\b", text.upper())
    code = code_match.group(1) if code_match else ""
    title = text.strip().title() if text else "Subject"
    return code, title


def extract_log_rows_from_soup(soup):
    """Extracts date-wise attendance log rows from a view-attendance page soup."""
    page_logs = []
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
        if "attendance date" in headers or ("date" in " ".join(headers) and "status" in " ".join(headers)):
            for row in table.find_all("tr")[1:]:
                cols = [td.get_text(" ", strip=True) for td in row.find_all("td")]
                if len(cols) >= 4:
                    date_str = cols[1].strip()
                    time_str = cols[2].strip() or "Scheduled Slot"
                    status_str = normalize_status(cols[3])
                    if date_str:
                        page_logs.append({
                            "date": date_str,
                            "time": time_str,
                            "status": status_str,
                        })
    return page_logs


def collect_pagination_urls(soup, page_url):
    """Finds pagination page URLs (e.g., ?class_id=...&page=2) on a detail page."""
    paginated_urls = []
    seen = {page_url}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full_url = urljoin(page_url, href)
        parsed = urlparse(full_url)
        qs = parse_qs(parsed.query)
        link_text = a.get_text(" ", strip=True).lower()

        is_page_link = (
            "page" in qs
            or "per-page" in qs
            or bool(re.fullmatch(r"\d+", link_text))
            or link_text in ("next", "»", ">", "last")
        )
        if is_page_link and "view-attendance" in parsed.path.lower() and full_url not in seen:
            seen.add(full_url)
            paginated_urls.append(full_url)

    return paginated_urls


def parse_view_attendance_page(sess, initial_html, page_url, fallback_class, fallback_course):
    """Parses summary counts and all paginated date-wise attendance logs."""
    soup = BeautifulSoup(initial_html, "html.parser")
    full_text = " ".join(soup.get_text(" ", strip=True).split())

    class_name = fallback_class
    course_name = fallback_course

    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 2:
            label = cells[0].lower()
            val = cells[1].strip()
            if "class name" in label and val:
                class_name = val
            elif "course name" in label and val:
                course_name = val

    present_match = re.search(r"Present\s*Count\s*[:\-]?\s*(\d+)", full_text, re.I)
    absent_match = re.search(r"Absent\s*Count\s*[:\-]?\s*(\d+)", full_text, re.I)
    leave_match = re.search(r"Leave\s*Count\s*[:\-]?\s*(\d+)", full_text, re.I)
    taken_match = re.search(r"Total\s*Classes\s*Taken\s*[:\-]?\s*(\d+)", full_text, re.I)

    present_cnt = int(present_match.group(1)) if present_match else 0
    absent_cnt = int(absent_match.group(1)) if absent_match else 0
    leave_cnt = int(leave_match.group(1)) if leave_match else 0
    total_taken = int(taken_match.group(1)) if taken_match else 0

    logs = extract_log_rows_from_soup(soup)
    seen_log_keys = {(l["date"], l["time"], l["status"]) for l in logs}

    queue = collect_pagination_urls(soup, page_url)
    visited = {page_url}

    while queue:
        next_url = queue.pop(0)
        if next_url in visited:
            continue
        visited.add(next_url)
        try:
            p_res = sess.get(next_url, timeout=15, allow_redirects=True)
            if p_res.ok and p_res.text:
                p_soup = BeautifulSoup(p_res.text, "html.parser")
                for entry in extract_log_rows_from_soup(p_soup):
                    key = (entry["date"], entry["time"], entry["status"])
                    if key not in seen_log_keys:
                        seen_log_keys.add(key)
                        logs.append(entry)
                for discovered in collect_pagination_urls(p_soup, p_res.url):
                    if discovered not in visited and discovered not in queue:
                        queue.append(discovered)
        except Exception:
            continue

    if logs and not (present_match or absent_match or leave_match):
        present_cnt = sum(1 for item in logs if item["status"] == "PRESENT")
        absent_cnt = sum(1 for item in logs if item["status"] == "ABSENT")
        leave_cnt = sum(1 for item in logs if item["status"] == "LEAVE")

    base_total = total_taken if total_taken > 0 else (present_cnt + absent_cnt + leave_cnt)
    # Formula: Present / (Total Classes - Leave)
    attended = present_cnt
    effective_total = base_total - leave_cnt
    total = effective_total if effective_total > 0 else base_total

    code, title = extract_code_and_title(course_name)
    tag = classify_subject_tag(class_name, course_name)

    return {
        "code": code,
        "name": f"{title} ({class_name})" if class_name else title,
        "tag": tag,
        "attended": attended,
        "total": total,
        "leaves": leave_cnt,
        "logs": logs,
    }


def parse_timetable_from_soup(soup):
    """Extracts weekly Monday–Saturday schedule if a timetable table is present."""
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
        if not days_re.search(table.get_text(" ", strip=True)):
            continue
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 2:
                continue
            day_match = days_re.search(cells[0])
            if day_match:
                day_name = day_match.group(1).capitalize()
                for idx, slot_val in enumerate(cells[1:], start=1):
                    cleaned = " ".join(slot_val.split())
                    if cleaned and cleaned.lower() not in ("-", "--", "free", "break", "lunch", "na", "n/a"):
                        slot_time = headers[idx] if idx < len(headers) and headers[idx] else f"Slot {idx}"
                        timetable[day_name].append({
                            "time": slot_time,
                            "raw": cleaned,
                        })
    return timetable


def fetch_samarth_data(sess, roll_no):
    """Fetches enrolled classes, each class's paginated attendance details, and timetable."""
    idx_res = sess.get(ATTENDANCE_INDEX_URL, timeout=15, allow_redirects=True)
    idx_res.raise_for_status()

    if "loginform[password]" in idx_res.text.lower():
        raise RuntimeError("Samarth session was not authenticated.")

    soup = BeautifulSoup(idx_res.text, "html.parser")
    timetable = parse_timetable_from_soup(soup)

    for a in soup.find_all("a", href=True):
        label_l = a.get_text(" ", strip=True).lower()
        if "time" in label_l and "table" in label_l:
            tt_url = urljoin(idx_res.url, a["href"])
            try:
                tt_res = sess.get(tt_url, timeout=12)
                if tt_res.ok:
                    parsed_tt = parse_timetable_from_soup(BeautifulSoup(tt_res.text, "html.parser"))
                    for day, slots in parsed_tt.items():
                        if slots and not timetable[day]:
                            timetable[day] = slots
            except Exception:
                pass
            break

    enrolled_items = []
    seen_urls = set()

    for row in soup.find_all("tr"):
        view_link = row.find("a", href=lambda h: h and "view-attendance" in h)
        if not view_link:
            continue

        detail_url = urljoin(idx_res.url, view_link["href"])
        if detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        td_texts = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        content_cells = [
            t for idx_c, t in enumerate(td_texts)
            if not (idx_c == 0 and re.fullmatch(r"\d+", t))
            and "view" not in t.lower()
            and not re.fullmatch(r"20\d{2}\s*-\s*20\d{2}", t)
            and "semester" not in t.lower()
        ]

        class_name = content_cells[0] if len(content_cells) >= 1 else ""
        course_name = content_cells[1] if len(content_cells) >= 2 else ""

        enrolled_items.append({
            "class_name": class_name,
            "course_name": course_name,
            "url": detail_url,
        })

    subjects = []
    if enrolled_items:
        def _load_detail(item):
            resp = sess.get(item["url"], timeout=15, allow_redirects=True)
            resp.raise_for_status()
            return parse_view_attendance_page(
                sess, resp.text, resp.url, item["class_name"], item["course_name"]
            )

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_load_detail, item): idx for idx, item in enumerate(enrolled_items)}
            ordered = [None] * len(enrolled_items)
            for fut in as_completed(futures):
                pos = futures[fut]
                try:
                    ordered[pos] = fut.result()
                except Exception:
                    code, title = extract_code_and_title(enrolled_items[pos]["course_name"])
                    ordered[pos] = {
                        "code": code,
                        "name": f"{title} ({enrolled_items[pos]['class_name']})",
                        "tag": classify_subject_tag(enrolled_items[pos]["class_name"], enrolled_items[pos]["course_name"]),
                        "attended": 0,
                        "total": 0,
                        "leaves": 0,
                        "logs": [],
                    }
            subjects = [s for s in ordered if s and s["total"] > 0]

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
        res = sess.get(SAMARTH_LOGIN_URL, timeout=15, allow_redirects=True)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        csrf_name, csrf_token, captcha_b64 = extract_csrf_and_captcha(soup, sess, res.url)

        SESSIONS[session_id] = {
            "session": sess,
            "login_url": res.url,
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
            "login_url": SAMARTH_LOGIN_URL,
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
        target_login_url = session_entry.get("login_url") or SAMARTH_LOGIN_URL
    else:
        sess = requests.Session()
        sess.headers.update(DEFAULT_HEADERS)
        target_login_url = SAMARTH_LOGIN_URL

    try:
        login_page_res = sess.get(target_login_url, timeout=15, allow_redirects=True)
        login_page_res.raise_for_status()
        actual_login_url = login_page_res.url
        login_soup = BeautifulSoup(login_page_res.text, "html.parser")
        csrf_name, csrf_token, _ = extract_csrf_and_captcha(login_soup, sess, actual_login_url)

        form_action = actual_login_url
        login_form = login_soup.find("form")
        if login_form and login_form.get("action"):
            form_action = urljoin(actual_login_url, login_form["action"].strip())

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
            form_action,
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

        result = fetch_samarth_data(sess, username)
        if not result["subjects"]:
            return jsonify({
                "status": "error",
                "message": "Logged in, but no enrolled attendance rows were found on Samarth.",
            })

        return jsonify(result)

    except requests.Timeout:
        return jsonify({
            "status": "error",
            "message": "DCRUST Samarth portal timed out. Please try again.",
        })
    except Exception as exc:
        return jsonify({
            "status": "error",
            "message": f"Failed to fetch attendance: {exc}",
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)