from flask import Flask, render_template, jsonify, request, Response
import requests, time, hmac, hashlib, json, sqlite3, io, csv, os, threading
from datetime import datetime, timedelta

app = Flask(__name__)
start_time = time.time()

# ------------- Tuya config -------------
ACCESS_ID     = 'x5mtcdxhy9rkuh4rr9cg'
ACCESS_SECRET = 'd2d72c19d69d44af903384bc80bae60a'
DEVICE_ID     = 'd79340df0ae8be883d4o6o'
BASE_URL      = 'https://openapi.tuyain.com'

# ------------- Dynamic DB Path for Deployment -------------
# Check if running on Render and set DB path accordingly
is_on_render = 'RENDER' in os.environ
# The mount path for the persistent disk on Render is /var/data
DB_PATH = '/var/data/power.db' if is_on_render else 'power.db'

# ------------- DB init -------------
def init_db():
    print(f"Initializing database at: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('''CREATE TABLE IF NOT EXISTS readings
                    (ts INTEGER PRIMARY KEY, voltage REAL, current REAL, power REAL)''')
    conn.commit()
    conn.close()
init_db()

# ------------- Tuya Helpers (unchanged) -------------
def sign(method, path, body='', token=''):
    t = str(int(time.time() * 1000))
    msg = ACCESS_ID + (token or '') + t + f"{method}\n{hashlib.sha256(body.encode()).hexdigest()}\n\n{path}"
    return t, hmac.new(ACCESS_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest().upper()

def get_token():
    t, sig = sign('GET', '/v1.0/token?grant_type=1')
    r = requests.get(BASE_URL + '/v1.0/token?grant_type=1',
                     headers={'client_id': ACCESS_ID, 'sign': sig, 't': t, 'sign_method': 'HMAC-SHA256'})
    r.raise_for_status()
    return r.json()['result']['access_token']

def get_device_data():
    token = get_token()
    t, sig = sign('GET', f'/v1.0/devices/{DEVICE_ID}/status', '', token)
    headers = {'client_id': ACCESS_ID, 'access_token': token, 'sign': sig, 't': t, 'sign_method': 'HMAC-SHA256'}
    r = requests.get(BASE_URL + f'/v1.0/devices/{DEVICE_ID}/status', headers=headers)
    r.raise_for_status()
    data = r.json()['result']
    switch  = next((d['value'] for d in data if d['code'] == 'switch_1'), False)
    voltage = next((d['value'] / 10   for d in data if d['code'] == 'cur_voltage'), 0)
    current = next((d['value'] / 1000 for d in data if d['code'] == 'cur_current'), 0)
    power   = next((d['value'] / 10   for d in data if d['code'] == 'cur_power'),   0)
    return voltage, current, power, switch

# ------------- Background Data Collector (NEW) -------------
def collect_data_periodically():
    """
    This function runs in a background thread.
    It collects data from the Tuya device every 15 seconds and saves it to the database
    if the device switch is on. This ensures data is collected even if no one is
    viewing the website.
    """
    print("Starting background data collection thread...")
    while True:
        try:
            voltage, current, power, switch = get_device_data()
            
            if switch:
                ts = int(time.time())
                conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                conn.execute('INSERT OR IGNORE INTO readings VALUES (?,?,?,?)',
                             (ts, voltage, current, power))
                conn.commit()
                conn.close()
                print(f"Background task: Data saved (Power: {power}W)")
            else:
                print("Background task: Device is off, not saving data.")

        except Exception as e:
            print(f"Error in background data collection: {e}")
        
        # Wait for 15 seconds before the next collection
        time.sleep(15)

# ------------- API: Live Data (Simplified) -------------
# This endpoint no longer saves data. It just provides the current status for the dashboard.
@app.route('/api/live')
def api_live():
    try:
        voltage, current, power, switch = get_device_data()
        return jsonify({
            'switch': switch,
            'power': power,
            'voltage': voltage,
            'current': current,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ------------- API: Daily Summary (unchanged) -------------
@app.route('/api/summary')
def api_summary():
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(today_start.timestamp())
    
    conn = sqlite3.connect(DB_PATH)
    energy_ws = conn.execute(
        "SELECT SUM(power) * 15 FROM readings WHERE ts >= ?", (start_ts,)
    ).fetchone()[0] or 0
    energy_kwh = energy_ws / 3600 / 1000

    runtime_seconds = conn.execute(
        "SELECT COUNT(*) * 15 FROM readings WHERE ts >= ? AND power > 10", (start_ts,)
    ).fetchone()[0] or 0
    
    conn.close()
    
    return jsonify({
        'today_energy_kwh': round(energy_kwh, 2),
        'daily_runtime_seconds': runtime_seconds
    })

# ------------- API: Historical Data (unchanged) -------------
@app.route('/api/history')
def api_history():
    date_str = request.args.get('date')
    try:
        query_date = datetime.strptime(date_str, '%Y-%m-%d')
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    start_ts = int(query_date.timestamp())
    end_ts = int((query_date + timedelta(days=1)).timestamp())

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute('''
        SELECT 
            (ts / 600) * 600 as interval_ts,
            AVG(power) as avg_power
        FROM readings
        WHERE ts >= ? AND ts < ?
        GROUP BY interval_ts
        ORDER BY interval_ts
    ''', (start_ts, end_ts)).fetchall()
    conn.close()
    
    return jsonify([{'x': r[0] * 1000, 'y': r[1]} for r in rows])

# ------------- API: System Status (unchanged) -------------
@app.route('/api/system')
def api_system():
    uptime_seconds = time.time() - start_time
    return jsonify({'uptime_seconds': uptime_seconds})

# ------------- API: Switch Control (unchanged) -------------
@app.route('/switch', methods=['POST'])
def switch_power():
    on = request.json.get('on', False)
    try:
        token = get_token()
        body = json.dumps({"commands": [{"code": "switch_1", "value": on}]})
        t, sig = sign('POST', f'/v1.0/devices/{DEVICE_ID}/commands', body, token)
        headers = {'client_id':ACCESS_ID,'access_token':token,'sign':sig,'t':t,'sign_method':'HMAC-SHA256','Content-Type':'application/json'}
        r = requests.post(BASE_URL + f'/v1.0/devices/{DEVICE_ID}/commands', headers=headers, data=body)
        r.raise_for_status()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ------------- Frontend Entry Point (unchanged) -------------
@app.route('/')
def index():
    return render_template('dashboard.html', device_id=DEVICE_ID) 

# ------------- Run App with Background Thread -------------
if __name__ == '__main__':
    # Start the background thread for data collection
    # The 'daemon=True' part ensures the thread will stop when the main app stops
    data_collector_thread = threading.Thread(target=collect_data_periodically, daemon=True)
    data_collector_thread.start()
    
    # Run the Flask app for local testing
    # On Render, the waitress-serve command will be used directly to run this file.
    app.run(debug=False, host='0.0.0.0', port=5000)