from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime, timezone, timedelta
JST = timezone(timedelta(hours=9))
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DB_NAME = 'database.db'

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                role TEXT DEFAULT 'employee'
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                work_date DATE NOT NULL,
                clock_in DATETIME,
                clock_out DATETIME,
                break_minutes INTEGER DEFAULT 60,
                tardy_minutes INTEGER DEFAULT 0,    -- 遅刻（分）
                early_leave_minutes INTEGER DEFAULT 0, -- 早退（分）
                actual_hours REAL DEFAULT 0,
                overtime_hours REAL DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS correction_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attendance_id INTEGER NOT NULL,
                requested_clock_in DATETIME,
                requested_clock_out DATETIME,
                reason TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        cursor.execute('SELECT COUNT(*) FROM users')
        if cursor.fetchone()[0] == 0:
            cursor.execute("INSERT INTO users (name, role) VALUES ('山田 太郎', 'employee')")
            cursor.execute("INSERT INTO users (name, role) VALUES ('佐藤 花子', 'employee')")
            cursor.execute("INSERT INTO users (name, role) VALUES ('管理者', 'admin')")
            conn.commit()

init_db()

def calculate_hours(clock_in_str, clock_out_str, break_minutes=60):
    if not clock_in_str or not clock_out_str:
        return 0, 0
    fmt = '%Y-%m-%d %H:%M:%S'
    cin = datetime.strptime(clock_in_str, fmt)
    cout = datetime.strptime(clock_out_str, fmt)
    
    total_minutes = (cout - cin).total_seconds() / 60 - break_minutes
    if total_minutes < 0:
        total_minutes = 0
        
    actual_hours = round(total_minutes / 60, 2)
    overtime_hours = round(max(0, actual_hours - 8.0), 2)
    return actual_hours, overtime_hours

# --- 打刻画面 ---
auth = HTTPBasicAuth()

# 管理者のパスワード設定（"your_password" の部分を設定したいパスワードに変更してください）
users = {
    "admin": generate_password_hash("kazuumi72381")
}

@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username

@app.route('/')
def index():
    user_id = request.args.get('user_id', 1)
    today = datetime.now(JST).strftime('%Y-%m-%d')
    
    with get_db() as conn:
        user = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        attendance = conn.execute(
            'SELECT * FROM attendance WHERE user_id = ? AND work_date = ?', 
            (user_id, today)
        ).fetchone()
        users = conn.execute('SELECT * FROM users').fetchall()
        
    return render_template('index.html', user=user, attendance=attendance, users=users, today=today)

# 打刻処理（出社・退社・遅刻・早退）
@app.route('/clock', methods=['POST'])
def clock():
    user_id = request.form['user_id']
    action = request.form['action']
   now_str = datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')
   today = datetime.now(JST).strftime('%Y-%m-%d')
    
    with get_db() as conn:
        cursor = conn.cursor()
        attendance = cursor.execute(
            'SELECT * FROM attendance WHERE user_id = ? AND work_date = ?', 
            (user_id, today)
        ).fetchone()
        
        if action == 'in':
            tardy = int(request.form.get('tardy_minutes', 0))
            if not attendance:
                cursor.execute(
                    'INSERT INTO attendance (user_id, work_date, clock_in, tardy_minutes) VALUES (?, ?, ?, ?)',
                    (user_id, today, now_str, tardy)
                )
        elif action == 'out':
            early = int(request.form.get('early_leave_minutes', 0))
            if attendance and attendance['clock_in'] and not attendance['clock_out']:
                actual_h, overtime_h = calculate_hours(attendance['clock_in'], now_str, attendance['break_minutes'])
                cursor.execute('''
                    UPDATE attendance 
                    SET clock_out = ?, actual_hours = ?, overtime_hours = ?, early_leave_minutes = ?
                    WHERE id = ?
                ''', (now_str, actual_h, overtime_h, early, attendance['id']))
        conn.commit()
        
    return redirect(url_for('index', user_id=user_id))

# 修正申請送信
@app.route('/request_correction', methods=['POST'])
def request_correction():
    attendance_id = request.form['attendance_id']
    user_id = request.form['user_id']
    req_in = request.form['requested_clock_in']
    req_out = request.form['requested_clock_out']
    reason = request.form['reason']
    
    with get_db() as conn:
        conn.execute('''
            INSERT INTO correction_requests 
            (attendance_id, requested_clock_in, requested_clock_out, reason) 
            VALUES (?, ?, ?, ?)
        ''', (attendance_id, req_in, req_out, reason))
        conn.commit()
        
    return redirect(url_for('index', user_id=user_id))

# --- 管理者画面（集計・スタッフ追加） ---
@app.route('/admin')
@auth.login_required
def admin():
    selected_month = request.args.get('month', datetime.now(JST).strftime('%Y-%m'))
    
    with get_db() as conn:
        users = conn.execute('SELECT * FROM users').fetchall()
        
        # 1日ベースの全勤怠履歴
        attendances = conn.execute('''
            SELECT a.*, u.name 
            FROM attendance a 
            JOIN users u ON a.user_id = u.id 
            ORDER BY a.work_date DESC, a.id DESC
        ''').fetchall()
        
        # 未承認申請
        requests = conn.execute('''
            SELECT r.*, u.name, a.work_date 
            FROM correction_requests r
            JOIN attendance a ON r.attendance_id = a.id
            JOIN users u ON a.user_id = u.id
            WHERE r.status = 'pending'
        ''').fetchall()
        
        # 月ベースのスタッフ別集計
        monthly_summary = conn.execute('''
            SELECT 
                u.name,
                COUNT(a.id) as work_days,
                SUM(a.actual_hours) as total_actual_hours,
                SUM(a.overtime_hours) as total_overtime_hours,
                SUM(CASE WHEN a.tardy_minutes > 0 THEN 1 ELSE 0 END) as tardy_count,
                SUM(CASE WHEN a.early_leave_minutes > 0 THEN 1 ELSE 0 END) as early_leave_count
            FROM users u
            LEFT JOIN attendance a ON u.id = a.user_id AND strftime('%Y-%m', a.work_date) = ?
            GROUP BY u.id
        ''', (selected_month,)).fetchall()
        
    return render_template('admin.html', attendances=attendances, requests=requests, users=users, monthly_summary=monthly_summary, selected_month=selected_month)

# スタッフ登録
@app.route('/add_user', methods=['POST'])
def add_user():
    name = request.form['name']
    role = request.form.get('role', 'employee')
    if name:
        with get_db() as conn:
            conn.execute('INSERT INTO users (name, role) VALUES (?, ?)', (name, role))
            conn.commit()
    return redirect(url_for('admin'))

# 承認処理
@app.route('/approve/<int:req_id>/<string:action>', methods=['POST'])
def approve(req_id, action):
    with get_db() as conn:
        cursor = conn.cursor()
        req_data = cursor.execute('SELECT * FROM correction_requests WHERE id = ?', (req_id,)).fetchone()
        
        if req_data and action in ['approve', 'reject']:
            status = 'approved' if action == 'approve' else 'rejected'
            cursor.execute('UPDATE correction_requests SET status = ? WHERE id = ?', (status, req_id))
            
            if action == 'approve':
                actual_h, overtime_h = calculate_hours(req_data['requested_clock_in'], req_data['requested_clock_out'])
                cursor.execute('''
                    UPDATE attendance 
                    SET clock_in = ?, clock_out = ?, actual_hours = ?, overtime_hours = ? 
                    WHERE id = ?
                ''', (req_data['requested_clock_in'], req_data['requested_clock_out'], actual_h, overtime_h, req_data['attendance_id']))
            conn.commit()
            
    return redirect(url_for('admin'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
