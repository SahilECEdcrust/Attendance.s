from flask import Flask, render_template, request, jsonify
import requests
from bs4 import BeautifulSoup
import base64
import uuid
import re

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
        sessions[session_id] = {
            "session": session,
            "csrf": csrf_token
        }

        return jsonify({
            "status": "success",
            "session_id": session_id,
            "captcha": captcha_base64
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"DCRUST सर्वर से संपर्क नहीं हो पा रहा: {str(e)}"})

@app.route('/get-attendance', methods=['POST'])
def get_attendance():
    data = request.json
    session_id = data.get('session_id')
    username = data.get('username')
    password = data.get('password')
    captcha = data.get('captcha', '')

    if session_id not in sessions:
        return jsonify({"status": "error", "message": "सेशन एक्सपायर हो गया, पेज रिफ्रेश करें।"})

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
            return jsonify({"status": "error", "message": "गलत Roll No, Password या CAPTCHA! कृपया दोबारा प्रयास करें।"})

        att_res = session.get(ATTENDANCE_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(att_res.text, 'html.parser')

        subjects = []
        table = soup.find('table')
        
        if table:
            rows = table.find_all('tr')[1:]
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 6:
                    class_type = cols[1].get_text(strip=True)
                    course_full = cols[2].get_text(strip=True)
                    
                    clean_name = course_full.split(":")[-1].strip().title() if ":" in course_full else course_full
                    tag = "Lab" if "Lab" in class_type else ("Tute" if "Tute" in class_type else "Theory")
                    display_name = f"{clean_name} ({tag})"

                    view_link = cols[-1].find('a')
                    if view_link and view_link.get('href'):
                        href = view_link['href']
                        detail_url = href if href.startswith("http") else BASE_URL + href
                        
                        detail_res = session.get(detail_url, headers=HEADERS, timeout=15)
                        detail_text = BeautifulSoup(detail_res.text, 'html.parser').get_text(" ", strip=True)

                        # ऊपर वाले Summary Box से सटीक नंबर निकालें
                        total_match = re.search(r'Total Classes Taken:\s*(\d+)', detail_text)
                        present_match = re.search(r'Present Count:\s*(\d+)', detail_text)
                        leave_match = re.search(r'Leave Count:\s*(\d+)', detail_text)
                        percent_match = re.search(r'Present Percentage:\s*([\d.]+)%', detail_text)

                        if total_match and present_match:
                            total_taken = int(total_match.group(1))
                            present_count = int(present_match.group(1))
                            leave_count = int(leave_match.group(1)) if leave_match else 0
                            
                            # Samarth नियम: Effective Total = Total Taken - Leave Count
                            effective_total = max(total_taken - leave_count, 1)
                            exact_percent = float(percent_match.group(1)) if percent_match else round((present_count / effective_total) * 100, 2)

                            subjects.append({
                                "name": display_name,
                                "attended": present_count,
                                "total": effective_total,
                                "leaves": leave_count,
                                "percentage": exact_percent
                            })

        if not subjects:
            return jsonify({"status": "error", "message": "अटेंडेंस डेटा नहीं मिल सका।"})

        return jsonify({
            "status": "success",
            "student_name": f"B.Tech CSE ({username})",
            "subjects": subjects
        })

    except Exception as e:
        return jsonify({"status": "error", "message": f"त्रुटि: {str(e)}"})

if __name__ == '__main__':
    app.run(debug=True)