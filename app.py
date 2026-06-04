from flask import Flask, render_template, request, jsonify
import requests
import pyotp
import re
import random
import string
import sqlite3
import time
import json
import hashlib
import base64
import uuid
import os
from datetime import datetime

# Optional imports (comment if not available)
try:
    from googletrans import Translator
    translator = Translator()
    TRANSLATE_AVAILABLE = True
except:
    TRANSLATE_AVAILABLE = False
    print("⚠️ Translation feature disabled. Install: pip install googletrans==4.0.0-rc1")

try:
    import speech_recognition as sr
    from pydub import AudioSegment
    import tempfile
    VOICE_AVAILABLE = True
except:
    VOICE_AVAILABLE = False
    print("⚠️ Voice feature disabled. Install: pip install SpeechRecognition pydub")

app = Flask(__name__)

# =========================================================
# DATABASE SETUP
# =========================================================
conn = sqlite3.connect("database.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS temp_mails(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT,
    password TEXT,
    token TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bin_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bin_number TEXT,
    result TEXT,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS short_links(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_code TEXT UNIQUE,
    original_url TEXT,
    user_id TEXT,
    device_info TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    clicks INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users_sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT UNIQUE,
    device_fingerprint TEXT,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS secrets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    secret TEXT,
    name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()

# =========================================================
# DEVICE FINGERPRINT & USER SESSION
# =========================================================
def generate_device_fingerprint(request):
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')
    accept_lang = request.headers.get('Accept-Language', '')
    fp_string = f"{ip}_{user_agent}_{accept_lang}"
    return hashlib.md5(fp_string.encode()).hexdigest()[:16]

def get_or_create_user_id(request):
    fingerprint = generate_device_fingerprint(request)
    cursor.execute("SELECT user_id FROM users_sessions WHERE device_fingerprint=?", (fingerprint,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE users_sessions SET last_seen=CURRENT_TIMESTAMP WHERE device_fingerprint=?", (fingerprint,))
        conn.commit()
        return row[0]
    else:
        user_id = str(uuid.uuid4())[:8]
        cursor.execute("INSERT INTO users_sessions(user_id, device_fingerprint) VALUES(?,?)", 
                      (user_id, fingerprint))
        conn.commit()
        return user_id

# =========================================================
# TEMP MAIL FUNCTIONS
# =========================================================
def get_domain():
    try:
        r = requests.get("https://api.mail.tm/domains", timeout=10)
        data = r.json()
        return data["hydra:member"][0]["domain"]
    except:
        return "mail.tm"

def create_temp_mail():
    domain = get_domain()
    username = "Thispersonisbrand" + str(random.randint(100000000, 999999999))
    email = f"{username}@{domain}"
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    
    try:
        r = requests.post("https://api.mail.tm/accounts", 
                         json={"address": email, "password": password}, timeout=10)
        if r.status_code not in [200, 201]:
            return None, None, None
        
        token_req = requests.post("https://api.mail.tm/token",
                                 json={"address": email, "password": password}, timeout=10)
        token = token_req.json()["token"]
        
        cursor.execute("INSERT INTO temp_mails(email, password, token) VALUES(?,?,?)",
                      (email, password, token))
        conn.commit()
        return email, password, token
    except:
        return None, None, None

def login_existing_mail(email, password):
    try:
        payload = {"address": email, "password": password}
        r = requests.post("https://api.mail.tm/token", json=payload, timeout=10)
        if r.status_code == 200:
            token = r.json()["token"]
            # Save to database
            cursor.execute("INSERT INTO temp_mails(email, password, token) VALUES(?,?,?)",
                          (email, password, token))
            conn.commit()
            return token
    except:
        pass
    return None

def get_inbox(token):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get("https://api.mail.tm/messages", headers=headers, timeout=10)
        return r.json().get("hydra:member", [])
    except:
        return []

def get_message(token, msg_id):
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=10)
        return r.json()
    except:
        return None

def extract_otp(text):
    otp_codes = re.findall(r'\b\d{4,8}\b', text)
    return otp_codes[0] if otp_codes else "NOT FOUND"

# =========================================================
# BIN CHECK FUNCTIONS
# =========================================================
def check_bin(bin_num):
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin_num[:6]}", 
                        headers={"Accept-Version": "3"}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            result = {
                "scheme": data.get("scheme", "N/A"),
                "type": data.get("type", "N/A"),
                "brand": data.get("brand", "N/A"),
                "prepaid": data.get("prepaid", False),
                "bank": data.get("bank", {}).get("name", "N/A"),
                "bank_url": data.get("bank", {}).get("url", "N/A"),
                "bank_phone": data.get("bank", {}).get("phone", "N/A"),
                "country": data.get("country", {}).get("name", "N/A"),
                "country_code": data.get("country", {}).get("alpha2", "N/A"),
                "country_emoji": data.get("country", {}).get("emoji", ""),
                "currency": data.get("country", {}).get("currency", "N/A")
            }
            cursor.execute("INSERT INTO bin_history(bin_number, result) VALUES(?,?)",
                          (bin_num[:6], str(result)))
            conn.commit()
            return result
    except:
        pass
    return None

# =========================================================
# SHORTLINK MANAGEMENT
# =========================================================
def generate_short_code(url, user_id):
    hash_obj = hashlib.md5(f"{url}_{user_id}".encode())
    code = base64.b64encode(hash_obj.digest())[:6].decode().replace('/', '_').replace('+', '-')
    return code

def create_or_update_short_link(url, user_id):
    short_code = generate_short_code(url, user_id)
    try:
        cursor.execute("""
            INSERT INTO short_links(short_code, original_url, user_id) 
            VALUES(?,?,?)
        """, (short_code, url, user_id))
        conn.commit()
        return short_code, "created"
    except sqlite3.IntegrityError:
        cursor.execute("""
            UPDATE short_links 
            SET original_url=?, updated_at=CURRENT_TIMESTAMP 
            WHERE short_code=?
        """, (url, short_code))
        conn.commit()
        return short_code, "updated"

def get_user_shortlinks(user_id):
    cursor.execute("""
        SELECT short_code, original_url, clicks, created_at, updated_at 
        FROM short_links 
        WHERE user_id=? 
        ORDER BY created_at DESC
    """, (user_id,))
    rows = cursor.fetchall()
    return [{
        "short_code": r[0],
        "original_url": r[1],
        "clicks": r[2],
        "created_at": r[3],
        "updated_at": r[4]
    } for r in rows]

def update_short_link(short_code, new_url, user_id):
    cursor.execute("""
        UPDATE short_links 
        SET original_url=?, updated_at=CURRENT_TIMESTAMP 
        WHERE short_code=? AND user_id=?
    """, (new_url, short_code, user_id))
    conn.commit()
    return cursor.rowcount > 0

def delete_short_link(short_code, user_id):
    cursor.execute("DELETE FROM short_links WHERE short_code=? AND user_id=?", 
                  (short_code, user_id))
    conn.commit()
    return cursor.rowcount > 0

def get_original_url(short_code):
    cursor.execute("UPDATE short_links SET clicks = clicks + 1 WHERE short_code=?", (short_code,))
    conn.commit()
    cursor.execute("SELECT original_url FROM short_links WHERE short_code=?", (short_code,))
    row = cursor.fetchone()
    return row[0] if row else None

# =========================================================
# FACEBOOK UID EXTRACTOR
# =========================================================
def extract_fb_uid(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=15)
        html = r.text
        patterns = [
            r'"userID":"(\d+)"',
            r'"entity_id":"(\d+)"',
            r'"actorID":"(\d+)"',
            r'profile\.php\?id=(\d+)',
            r'facebook\.com\/(\d+)'
        ]
        for p in patterns:
            match = re.search(p, html)
            if match:
                title = re.search(r'<title>(.*?)</title>', html)
                name = title.group(1) if title else "Unknown"
                name = re.sub(r'\(.*?\)', '', name).strip()
                return match.group(1), name
        return None, None
    except:
        return None, None

# =========================================================
# QR CODE GENERATOR
# =========================================================
def generate_qr_code(data):
    try:
        url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={requests.utils.quote(data)}"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return base64.b64encode(response.content).decode()
    except:
        pass
    return None

# =========================================================
# PASSWORD GENERATOR
# =========================================================
def generate_password(length=12, use_upper=True, use_lower=True, use_digits=True, use_symbols=True):
    chars = ''
    if use_upper:
        chars += string.ascii_uppercase
    if use_lower:
        chars += string.ascii_lowercase
    if use_digits:
        chars += string.digits
    if use_symbols:
        chars += "!@#$%^&*()_+-=[]{}|;:,.<>?"
    if not chars:
        chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

# =========================================================
# TIMEZONE CONVERTER
# =========================================================
timezone_offsets = {
    "UTC": 0, "GMT": 0, "BST": 1, "CET": 1, "IST": 5.5, "PKT": 5,
    "BDT": 6, "BTT": 6, "MMT": 6.5, "ICT": 7, "CST": 8, "WST": 8,
    "JST": 9, "KST": 9, "AEST": 10, "NZST": 12, "EST": -5, 
    "CST_US": -6, "MST": -7, "PST": -8, "AKST": -9, "HST": -10
}

def convert_timezone(time_str, from_tz, to_tz):
    try:
        hours, minutes = map(int, time_str.split(':'))
        from_offset = timezone_offsets.get(from_tz.upper(), 0)
        to_offset = timezone_offsets.get(to_tz.upper(), 0)
        total_minutes = hours * 60 + minutes
        diff = to_offset - from_offset
        new_total = total_minutes + int(diff * 60)
        new_hours = (new_total // 60) % 24
        new_minutes = new_total % 60
        return f"{new_hours:02d}:{new_minutes:02d}"
    except:
        return None

# =========================================================
# DICTIONARY API
# =========================================================
def get_word_meaning(word):
    try:
        r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                meanings = []
                for meaning in data[0].get('meanings', [])[:3]:
                    meanings.append({
                        "part": meaning.get('partOfSpeech', 'unknown'),
                        "definition": meaning.get('definitions', [{}])[0].get('definition', 'N/A'),
                        "example": meaning.get('definitions', [{}])[0].get('example', '')
                    })
                return {
                    "word": data[0].get('word', word),
                    "phonetic": data[0].get('phonetic', ''),
                    "meanings": meanings
                }
    except:
        pass
    return None

# =========================================================
# TRANSLATION FUNCTIONS
# =========================================================
def translate_text(text, target_lang='bn'):
    if not TRANSLATE_AVAILABLE:
        return {"success": False, "error": "Translation not available"}
    try:
        translated = translator.translate(text, dest=target_lang)
        return {
            "success": True,
            "original": text,
            "translated": translated.text,
            "source_lang": translated.src,
            "target_lang": target_lang
        }
    except:
        return {"success": False, "error": "Translation failed"}

def detect_language(text):
    if not TRANSLATE_AVAILABLE:
        return 'en'
    try:
        detection = translator.detect(text)
        return detection.lang
    except:
        return 'en'

# =========================================================
# VOICE TO TEXT
# =========================================================
def voice_to_text(audio_data):
    if not VOICE_AVAILABLE:
        return None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name
        
        audio = AudioSegment.from_file(tmp_path, format="webm")
        wav_path = tmp_path + '.wav'
        audio.export(wav_path, format="wav")
        
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio, language="bn-IN")
            except:
                text = recognizer.recognize_google(audio, language="en-US")
        
        os.unlink(tmp_path)
        os.unlink(wav_path)
        return text
    except:
        return None

# =========================================================
# ROUTES
# =========================================================
@app.route('/')
def index():
    return render_template('index.html')

# ========== TEMP MAIL ROUTES ==========
@app.route('/api/create_mail', methods=['GET'])
def api_create_mail():
    email, password, token = create_temp_mail()
    if email:
        return jsonify({"success": True, "email": email, "password": password, "token": token})
    return jsonify({"success": False, "error": "Failed to create mail"})

@app.route('/api/login_mail', methods=['POST'])
def api_login_mail():
    data = request.json
    email = data.get('email', '')
    password = data.get('password', '')
    
    if not email or not password:
        return jsonify({"success": False, "error": "Email and password required"})
    
    token = login_existing_mail(email, password)
    if token:
        return jsonify({"success": True, "email": email, "password": password, "token": token})
    return jsonify({"success": False, "error": "Invalid credentials"})

@app.route('/api/check_inbox', methods=['POST'])
def api_check_inbox():
    data = request.json
    token = data.get('token')
    if not token:
        return jsonify({"success": False, "error": "No token provided"})
    
    inbox = get_inbox(token)
    if not inbox:
        return jsonify({"success": True, "messages": []})
    
    messages = []
    for msg in inbox[:10]:
        full = get_message(token, msg['id'])
        body = full.get('text', '') or str(full.get('html', '')) if full else ''
        otp = extract_otp(body)
        messages.append({
            "from": msg['from']['address'],
            "subject": msg.get('subject', 'No Subject'),
            "body": body[:500],
            "otp": otp,
            "date": msg.get('createdAt', 'Unknown')
        })
    return jsonify({"success": True, "messages": messages})

@app.route('/api/get_saved_mails', methods=['GET'])
def api_get_saved_mails():
    cursor.execute("SELECT email, password, created_at FROM temp_mails ORDER BY id DESC LIMIT 10")
    mails = cursor.fetchall()
    return jsonify({"success": True, "mails": [{"email": m[0], "password": m[1], "created": m[2]} for m in mails]})

# ========== 2FA ROUTES ==========
@app.route('/api/generate_2fa', methods=['POST'])
def api_generate_2fa():
    data = request.json
    secret = re.sub(r'\s+', '', data.get('secret', '')).upper()
    try:
        totp = pyotp.TOTP(secret)
        code = totp.now()
        remain = 30 - (int(time.time()) % 30)
        return jsonify({"success": True, "code": code, "expires": remain, "secret": secret})
    except:
        return jsonify({"success": False, "error": "Invalid secret key"})

@app.route('/api/save_2fa_secret', methods=['POST'])
def api_save_2fa_secret():
    data = request.json
    secret = data.get('secret', '').strip()
    name = data.get('name', 'Unnamed')
    user_id = get_or_create_user_id(request)
    
    # Remove spaces from secret
    secret = re.sub(r'\s+', '', secret).upper()
    
    cursor.execute("INSERT INTO secrets(user_id, secret, name) VALUES(?,?,?)", (user_id, secret, name))
    conn.commit()
    return jsonify({"success": True})

@app.route('/api/get_2fa_secrets', methods=['GET'])
def api_get_2fa_secrets():
    user_id = get_or_create_user_id(request)
    cursor.execute("SELECT secret, name FROM secrets WHERE user_id=? ORDER BY id DESC", (user_id,))
    secrets = cursor.fetchall()
    return jsonify({"success": True, "secrets": [{"secret": s[0], "name": s[1]} for s in secrets]})

@app.route('/api/delete_2fa_secret', methods=['POST'])
def api_delete_2fa_secret():
    data = request.json
    secret = data.get('secret', '')
    user_id = get_or_create_user_id(request)
    cursor.execute("DELETE FROM secrets WHERE user_id=? AND secret=?", (user_id, secret))
    conn.commit()
    return jsonify({"success": True})

# ========== BIN CHECK ROUTES ==========
@app.route('/api/check_bin', methods=['POST'])
def api_check_bin():
    data = request.json
    bin_num = data.get('bin', '')[:6]
    if len(bin_num) < 6:
        return jsonify({"success": False, "error": "Enter at least 6 digits"})
    result = check_bin(bin_num)
    if result:
        return jsonify({"success": True, **result, "bin": bin_num})
    return jsonify({"success": False, "error": "Invalid BIN"})

@app.route('/api/bin_history', methods=['GET'])
def api_bin_history():
    cursor.execute("SELECT bin_number, checked_at FROM bin_history ORDER BY id DESC LIMIT 10")
    history = cursor.fetchall()
    return jsonify({"success": True, "history": [{"bin": h[0], "date": h[1]} for h in history]})

# ========== UID EXTRACTOR ROUTES ==========
@app.route('/api/extract_uid', methods=['POST'])
def api_extract_uid():
    data = request.json
    url = data.get('url', '')
    if not url:
        return jsonify({"success": False, "error": "No URL provided"})
    
    uid, name = extract_fb_uid(url)
    if uid:
        return jsonify({"success": True, "uid": uid, "name": name})
    return jsonify({"success": False, "error": "UID not found"})

# ========== SHORTLINK ROUTES ==========
@app.route('/api/shorten_url', methods=['POST'])
def api_shorten_url():
    data = request.json
    url = data.get('url', '')
    if not url:
        return jsonify({"success": False, "error": "No URL provided"})
    
    user_id = get_or_create_user_id(request)
    short_code, action = create_or_update_short_link(url, user_id)
    short_url = f"https://{request.host}/s/{short_code}"
    
    return jsonify({"success": True, "short_url": short_url, "code": short_code, "action": action})

@app.route('/api/get_my_links', methods=['GET'])
def api_get_my_links():
    user_id = get_or_create_user_id(request)
    links = get_user_shortlinks(user_id)
    return jsonify({"success": True, "links": links})

@app.route('/api/update_link', methods=['POST'])
def api_update_link():
    data = request.json
    short_code = data.get('short_code', '')
    new_url = data.get('new_url', '')
    
    if not short_code or not new_url:
        return jsonify({"success": False, "error": "Missing data"})
    
    user_id = get_or_create_user_id(request)
    if update_short_link(short_code, new_url, user_id):
        return jsonify({"success": True, "message": "Link updated"})
    return jsonify({"success": False, "error": "Failed to update"})

@app.route('/api/delete_link', methods=['POST'])
def api_delete_link():
    data = request.json
    short_code = data.get('short_code', '')
    
    if not short_code:
        return jsonify({"success": False, "error": "No short code provided"})
    
    user_id = get_or_create_user_id(request)
    if delete_short_link(short_code, user_id):
        return jsonify({"success": True, "message": "Link deleted"})
    return jsonify({"success": False, "error": "Failed to delete"})

@app.route('/s/<short_code>')
def redirect_short_link(short_code):
    original_url = get_original_url(short_code)
    if original_url:
        return f'<html><head><meta http-equiv="refresh" content="0;url={original_url}"></head><body>Redirecting...</body></html>'
    return "Link not found", 404

# ========== QR CODE ROUTES ==========
@app.route('/api/generate_qr', methods=['POST'])
def api_generate_qr():
    data = request.json
    text = data.get('text', '')
    if not text:
        return jsonify({"success": False, "error": "No text provided"})
    
    qr_image = generate_qr_code(text)
    if qr_image:
        return jsonify({"success": True, "qr_image": qr_image})
    return jsonify({"success": False, "error": "Failed to generate QR"})

# ========== PASSWORD GENERATOR ROUTES ==========
@app.route('/api/generate_password', methods=['POST'])
def api_generate_password():
    data = request.json
    length = int(data.get('length', 12))
    use_upper = data.get('use_upper', True)
    use_lower = data.get('use_lower', True)
    use_digits = data.get('use_digits', True)
    use_symbols = data.get('use_symbols', True)
    
    password = generate_password(length, use_upper, use_lower, use_digits, use_symbols)
    return jsonify({"success": True, "password": password, "length": length})

# ========== TIMEZONE CONVERTER ROUTES ==========
@app.route('/api/convert_timezone', methods=['POST'])
def api_convert_timezone():
    data = request.json
    time_str = data.get('time', '')
    from_tz = data.get('from_tz', 'UTC')
    to_tz = data.get('to_tz', 'BDT')
    
    result = convert_timezone(time_str, from_tz, to_tz)
    if result:
        return jsonify({"success": True, "converted_time": result})
    return jsonify({"success": False, "error": "Invalid time format. Use HH:MM"})

# ========== DICTIONARY ROUTES ==========
@app.route('/api/dictionary', methods=['POST'])
def api_dictionary():
    data = request.json
    word = data.get('word', '').strip()
    if not word:
        return jsonify({"success": False, "error": "No word provided"})
    
    result = get_word_meaning(word)
    if result:
        return jsonify({"success": True, **result})
    return jsonify({"success": False, "error": "Word not found"})

# ========== TRANSLATION ROUTES ==========
@app.route('/api/translate', methods=['POST'])
def api_translate():
    data = request.json
    text = data.get('text', '')
    target = data.get('target', 'bn')
    
    if not text:
        return jsonify({"success": False, "error": "No text provided"})
    
    result = translate_text(text, target)
    return jsonify(result)

@app.route('/api/detect_language', methods=['POST'])
def api_detect_language():
    data = request.json
    text = data.get('text', '')
    if not text:
        return jsonify({"success": False, "error": "No text provided"})
    
    lang = detect_language(text)
    return jsonify({"success": True, "language": lang})

@app.route('/api/languages', methods=['GET'])
def api_languages():
    languages = {
        "en": "English", "bn": "Bengali", "hi": "Hindi", "es": "Spanish",
        "fr": "French", "de": "German", "zh-cn": "Chinese", "ja": "Japanese",
        "ko": "Korean", "ru": "Russian", "ar": "Arabic"
    }
    return jsonify({"success": True, "languages": languages})

# ========== VOICE TO TEXT ROUTE ==========
@app.route('/api/voice_to_text', methods=['POST'])
def api_voice_to_text():
    if not VOICE_AVAILABLE:
        return jsonify({"success": False, "error": "Voice feature not available"})
    
    if 'audio' not in request.files:
        return jsonify({"success": False, "error": "No audio file provided"})
    
    audio_file = request.files['audio']
    audio_data = audio_file.read()
    
    text = voice_to_text(audio_data)
    if text:
        return jsonify({"success": True, "text": text})
    return jsonify({"success": False, "error": "Could not recognize speech"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
