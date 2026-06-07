from flask import Flask, render_template, request, redirect, session, jsonify, send_file
import sqlite3
import os
import json
import bcrypt
import base64
from datetime import datetime
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.utils import secure_filename

# Import custom services
import ai_service
import pdf_generator

# Load environment configurations
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "secret123_change_me")
csrf = CSRFProtect(app)

# Initialize scheduler safely (only runs in main child thread under Werkzeug reloader)
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    import backup_service
    backup_service.init_scheduler()

# ---------- SECURE SESSION & COOKIES ----------
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ---------- SECURE UPLOADS ----------
UPLOAD_FOLDER = "static/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ---------- DATABASE CONNECTION ----------
def get_db():
    conn = sqlite3.connect(os.getenv("DATABASE_PATH", "database.db"))
    conn.row_factory = sqlite3.Row
    return conn

# ---------- ROLE CHECKING HELPER ----------
def is_admin():
    return session.get("admin", False)

# ---------- LOGIN REQUIRED ROUTE GUARD ----------
@app.before_request
def require_login():
    # Endpoints that don't require credentials
    allowed = ["login", "static", "admin"]
    if request.endpoint not in allowed:
        if not session.get("user") and not session.get("admin"):
            return redirect("/login")

# ---------- CSRF ERROR HANDLER ----------
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return f"CSRF validation failed: {e.description}. Try reloading the page.", 400

# ---------- TEACHER/ADMIN LOGIN ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        db = get_db()
        teacher = db.execute("SELECT * FROM teachers WHERE username=?", (username,)).fetchone()
        db.close()

        if teacher and bcrypt.checkpw(password.encode('utf-8'), teacher['password_hash'].encode('utf-8')):
            session["user"] = username
            session["role"] = "teacher"
            session["teacher_id"] = teacher["id"]
            return redirect("/")

        return "Invalid username or password"

    return render_template("login.html")

# ---------- LOGOUT ----------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------- ADMIN PORTAL LOGIN ----------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST":
        admin_pass = os.getenv("ADMIN_PASSWORD", "Schoolisgood")
        if request.form["password"] == admin_pass:
            session["admin"] = True
            session["role"] = "admin"
            return redirect("/dashboard")
        return "Invalid admin password"
    return render_template("login.html", is_admin=True)

# ---------- HOME / ERP ANALYTICS DASHBOARD ----------
@app.route("/")
def home():
    db = get_db()

    active_batch = db.execute("SELECT * FROM batches WHERE is_active=1").fetchone()
    active_batch_id = active_batch['id'] if active_batch else None
    active_batch_name = active_batch['batch_name'] if active_batch else "All"

    if active_batch_id:
        total_students = db.execute("SELECT COUNT(*) FROM students WHERE batch_id=?", (active_batch_id,)).fetchone()[0]
    else:
        total_students = db.execute("SELECT COUNT(*) FROM students").fetchone()[0]

    total_teachers = db.execute("SELECT COUNT(*) FROM teachers").fetchone()[0]
    total_exams = db.execute("SELECT COUNT(*) FROM exams").fetchone()[0]
    total_subjects = db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]

    # Calculate overall attendance percentage (filtered by active batch)
    if active_batch_id:
        att_row = db.execute("""
            SELECT 
                SUM(case status when 'Present' then 1.0 when 'Late' then 0.5 when 'Leave' then 1.0 else 0.0 end),
                COUNT(*) 
            FROM attendance a
            JOIN students s ON a.student_id = s.id
            WHERE s.batch_id = ?
        """, (active_batch_id,)).fetchone()
    else:
        att_row = db.execute("""
            SELECT 
                SUM(case status when 'Present' then 1.0 when 'Late' then 0.5 when 'Leave' then 1.0 else 0.0 end),
                COUNT(*) 
            FROM attendance
        """).fetchone()

    overall_attendance_pct = 0.0
    if att_row and att_row[1] and att_row[1] > 0:
        overall_attendance_pct = (att_row[0] / att_row[1]) * 100.0

    # Risk levels from AI Reports (filtered by active batch)
    if active_batch_id:
        high_risk_count = db.execute("""
            SELECT COUNT(*) 
            FROM ai_reports a
            JOIN students s ON a.student_id = s.id
            WHERE a.risk_score >= 50 AND s.batch_id = ?
        """, (active_batch_id,)).fetchone()[0]
    else:
        high_risk_count = db.execute("SELECT COUNT(*) FROM ai_reports WHERE risk_score >= 50").fetchone()[0]

    # Count low attendance students (< 75%) (filtered by active batch)
    if active_batch_id:
        low_att_row = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT student_id, (SUM(case status when 'Present' then 1.0 when 'Late' then 0.5 when 'Leave' then 1.0 else 0.0 end) / COUNT(*)) * 100 as pct
                FROM attendance a
                JOIN students s ON a.student_id = s.id
                WHERE s.batch_id = ?
                GROUP BY student_id
                HAVING pct < 75
            )
        """, (active_batch_id,)).fetchone()
    else:
        low_att_row = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT student_id, (SUM(case status when 'Present' then 1.0 when 'Late' then 0.5 when 'Leave' then 1.0 else 0.0 end) / COUNT(*)) * 100 as pct
                FROM attendance
                GROUP BY student_id
                HAVING pct < 75
            )
        """).fetchone()
    low_attendance_count = low_att_row[0] if low_att_row else 0

    # Count top performers (> 85% average marks) (filtered by active batch)
    if active_batch_id:
        top_perf_row = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT student_id, AVG((marks_obtained / max_marks) * 100) as avg_marks
                FROM marks m
                JOIN students s ON m.student_id = s.id
                WHERE s.batch_id = ?
                GROUP BY student_id
                HAVING avg_marks > 85
            )
        """, (active_batch_id,)).fetchone()
    else:
        top_perf_row = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT student_id, AVG((marks_obtained / max_marks) * 100) as avg_marks
                FROM marks
                GROUP BY student_id
                HAVING avg_marks > 85
            )
        """).fetchone()
    top_performers_count = top_perf_row[0] if top_perf_row else 0

    # Risk Distribution mapping (filtered by active batch)
    if active_batch_id:
        risk_row = db.execute("""
            SELECT 
                SUM(case when risk_score < 30 then 1 else 0 end) as low,
                SUM(case when risk_score >= 30 and risk_score < 50 then 1 else 0 end) as medium,
                SUM(case when risk_score >= 50 and risk_score < 70 then 1 else 0 end) as high,
                SUM(case when risk_score >= 70 then 1 else 0 end) as critical
            FROM ai_reports a
            JOIN students s ON a.student_id = s.id
            WHERE s.batch_id = ?
        """, (active_batch_id,)).fetchone()
    else:
        risk_row = db.execute("""
            SELECT 
                SUM(case when risk_score < 30 then 1 else 0 end) as low,
                SUM(case when risk_score >= 30 and risk_score < 50 then 1 else 0 end) as medium,
                SUM(case when risk_score >= 50 and risk_score < 70 then 1 else 0 end) as high,
                SUM(case when risk_score >= 70 then 1 else 0 end) as critical
            FROM ai_reports
        """).fetchone()
    risk_distribution = {
        "low": risk_row[0] if risk_row and risk_row[0] else 0,
        "medium": risk_row[1] if risk_row and risk_row[1] else 0,
        "high": risk_row[2] if risk_row and risk_row[2] else 0,
        "critical": risk_row[3] if risk_row and risk_row[3] else 0,
    }

    # Leaderboard Table: Top 5 Student Rankings (filtered by active batch)
    if active_batch_id:
        rankings_rows = db.execute("""
            SELECT s.id, s.name, AVG((m.marks_obtained / m.max_marks) * 100) as avg_marks
            FROM marks m
            JOIN students s ON m.student_id = s.id
            WHERE s.batch_id = ?
            GROUP BY s.id
            ORDER BY avg_marks DESC
            LIMIT 5
        """, (active_batch_id,)).fetchall()
    else:
        rankings_rows = db.execute("""
            SELECT s.id, s.name, AVG((m.marks_obtained / m.max_marks) * 100) as avg_marks
            FROM marks m
            JOIN students s ON m.student_id = s.id
            GROUP BY s.id
            ORDER BY avg_marks DESC
            LIMIT 5
        """).fetchall()
    student_rankings = [{"id": r["id"], "name": r["name"], "avg_marks": r["avg_marks"]} for r in rankings_rows]

    # Original breakdowns for backward compatibility (filtered by active batch)
    if active_batch_id:
        science_cnt = db.execute("SELECT COUNT(*) FROM students WHERE stream='science' AND batch_id=?", (active_batch_id,)).fetchone()[0]
        commerce_cnt = db.execute("SELECT COUNT(*) FROM students WHERE stream='commerce' AND batch_id=?", (active_batch_id,)).fetchone()[0]
        cs_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='cs' AND batch_id=?", (active_batch_id,)).fetchone()[0]
        bio_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='bio' AND batch_id=?", (active_batch_id,)).fetchone()[0]
        maths_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='maths' AND batch_id=?", (active_batch_id,)).fetchone()[0]
        ca_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='ca' AND batch_id=?", (active_batch_id,)).fetchone()[0]
    else:
        science_cnt = db.execute("SELECT COUNT(*) FROM students WHERE stream='science'").fetchone()[0]
        commerce_cnt = db.execute("SELECT COUNT(*) FROM students WHERE stream='commerce'").fetchone()[0]
        cs_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='cs'").fetchone()[0]
        bio_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='bio'").fetchone()[0]
        maths_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='maths'").fetchone()[0]
        ca_cnt = db.execute("SELECT COUNT(*) FROM students WHERE category='ca'").fetchone()[0]

    db.close()

    # Try mapping back to standard index if requested or using our rich design
    return render_template(
        "index.html",
        total_students=total_students,
        total_teachers=total_teachers,
        total_exams=total_exams,
        total_subjects=total_subjects,
        overall_attendance_pct=overall_attendance_pct,
        high_risk_count=high_risk_count,
        low_attendance_count=low_attendance_count,
        top_performers_count=top_performers_count,
        risk_distribution=risk_distribution,
        student_rankings=student_rankings,
        science=science_cnt,
        commerce=commerce_cnt,
        cs=cs_cnt,
        bio=bio_cnt,
        maths=maths_cnt,
        ca=ca_cnt,
        active_batch_name=active_batch_name
    )

# ---------- DIRECTORY SEARCH (JSON API) ----------
@app.route("/search")
def search():
    q = request.args.get("q", "")
    type_ = request.args.get("type")
    stream = request.args.get("stream")
    category = request.args.get("category")
    batch_id = request.args.get("batch_id")

    db = get_db()
    results = []

    # Filter Students
    if type_ in (None, "", "student"):
        sql = """
            SELECT s.id, s.name, s.register_no, s.class, s.stream, b.batch_name 
            FROM students s 
            LEFT JOIN batches b ON s.batch_id = b.id 
            WHERE (s.name LIKE ? COLLATE NOCASE 
               OR s.register_no LIKE ? COLLATE NOCASE
               OR s.class LIKE ? COLLATE NOCASE
               OR s.stream LIKE ? COLLATE NOCASE
               OR b.batch_name LIKE ? COLLATE NOCASE)
        """
        like_q = '%' + q + '%'
        params = [like_q, like_q, like_q, like_q, like_q]

        if stream:
            sql += " AND s.stream=?"
            params.append(stream)

        if category:
            sql += " AND s.category=?"
            params.append(category)

        if batch_id:
            sql += " AND s.batch_id=?"
            params.append(int(batch_id))

        for s in db.execute(sql, params):
            results.append({
                "id": s["id"], 
                "name": f"{s['name']} ({s['class']} {s['stream'].capitalize()} | Batch: {s['batch_name']})", 
                "type": "student"
            })

    # Filter Teachers
    if type_ in (None, "", "teacher"):
        for t in db.execute("SELECT id,name FROM teachers WHERE name LIKE ? COLLATE NOCASE", ('%'+q+'%',)):
            results.append({"id": t["id"], "name": t["name"], "type": "teacher"})

    db.close()
    return jsonify(results)

# ---------- TEACHERS ENDPOINTS ----------
@app.route("/teachers")
def teachers():
    db = get_db()
    res = db.execute("SELECT * FROM teachers").fetchall()
    db.close()
    return render_template("teachers.html", teachers=res)

@app.route("/teacher/<int:id>")
def teacher_detail(id):
    db = get_db()
    res = db.execute("SELECT * FROM teachers WHERE id=?", (id,)).fetchone()
    db.close()
    return render_template("teacher_detail.html", teacher=res)

@app.route("/edit/teacher/<int:id>", methods=["GET", "POST"])
def edit_teacher(id):
    if not is_admin():
        return redirect("/")
    db = get_db()
    if request.method == "POST":
        db.execute(
            "UPDATE teachers SET name=?, subject=?, details=? WHERE id=?",
            (request.form["name"], request.form["subject"], request.form["details"], id)
        )
        db.commit()
        db.close()
        return redirect("/admin-panel")
    teacher = db.execute("SELECT * FROM teachers WHERE id=?", (id,)).fetchone()
    db.close()
    return render_template("edit_teacher.html", teacher=teacher)

@app.route("/delete/teacher/<int:id>")
def delete_teacher(id):
    if not is_admin():
        return redirect("/")
    db = get_db()
    db.execute("DELETE FROM teachers WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/admin-panel")

# ---------- STUDENTS DIRECTORIES ----------
@app.route("/students")
def students():
    db = get_db()
    batches = db.execute("SELECT * FROM batches ORDER BY batch_name DESC").fetchall()
    
    batch_id_param = request.args.get("batch_id")
    stream_param = request.args.get("stream")
    category_param = request.args.get("category")
    class_param = request.args.get("class")
    
    # Default to active batch if none selected
    if not batch_id_param:
        active = db.execute("SELECT id FROM batches WHERE is_active=1").fetchone()
        batch_id_param = str(active["id"]) if active else ""
        
    query = """
        SELECT s.*, b.batch_name 
        FROM students s
        LEFT JOIN batches b ON s.batch_id = b.id
        WHERE 1=1
    """
    params = []
    
    if batch_id_param and batch_id_param != "all":
        query += " AND s.batch_id = ?"
        params.append(int(batch_id_param))
        
    if stream_param and stream_param != "all":
        query += " AND s.stream = ?"
        params.append(stream_param)
        
    if category_param and category_param != "all":
        query += " AND s.category = ?"
        params.append(category_param)
        
    if class_param and class_param != "all":
        query += " AND s.class = ?"
        params.append(class_param)
        
    students_list = db.execute(query + " ORDER BY s.name ASC", params).fetchall()
    
    # Unique classes
    classes_rows = db.execute("SELECT DISTINCT class FROM students WHERE class IS NOT NULL AND class != '' ORDER BY class ASC").fetchall()
    classes = [c["class"] for c in classes_rows]
    
    db.close()
    return render_template(
        "students.html",
        students=students_list,
        batches=batches,
        classes=classes,
        selected_batch=batch_id_param,
        selected_stream=stream_param or "all",
        selected_category=category_param or "all",
        selected_class=class_param or "all"
    )

@app.route("/students/science")
def science():
    return render_template("science.html")

@app.route("/students/commerce")
def commerce():
    return render_template("commerce.html")

@app.route("/students/<stream>/<category>")
def student_list(stream, category):
    db = get_db()
    batch_id_param = request.args.get("batch_id")
    
    # Default to active batch if none selected
    if not batch_id_param:
        active = db.execute("SELECT id FROM batches WHERE is_active=1").fetchone()
        batch_id_param = str(active["id"]) if active else ""
        
    query = """
        SELECT s.*, b.batch_name 
        FROM students s
        LEFT JOIN batches b ON s.batch_id = b.id
        WHERE s.stream=? AND s.category=?
    """
    params = [stream, category]
    
    if batch_id_param and batch_id_param != "all":
        query += " AND s.batch_id = ?"
        params.append(int(batch_id_param))
        
    res = db.execute(query + " ORDER BY s.name ASC", params).fetchall()
    batches = db.execute("SELECT * FROM batches ORDER BY batch_name DESC").fetchall()
    db.close()
    return render_template(
        "student_list.html",
        students=res,
        batches=batches,
        selected_batch=batch_id_param,
        stream=stream,
        category=category
    )

@app.route("/student/<int:id>")
def student_detail(id):
    db = get_db()
    student = db.execute("SELECT * FROM students WHERE id=?", (id,)).fetchone()
    if not student:
        db.close()
        return "Student not found", 404
    
    # Active subjects & exams list
    subjects = db.execute("SELECT * FROM subjects").fetchall()
    exams = db.execute("SELECT * FROM exams").fetchall()

    # Attendance percentages
    attendance_records = db.execute("SELECT * FROM attendance WHERE student_id=?", (id,)).fetchall()
    total_days = len(attendance_records)
    present_days = sum(1 for r in attendance_records if r['status'] == 'Present')
    late_days = sum(1 for r in attendance_records if r['status'] == 'Late')
    leave_days = sum(1 for r in attendance_records if r['status'] == 'Leave')
    absent_days = sum(1 for r in attendance_records if r['status'] == 'Absent')
    
    effective_present = present_days + (late_days * 0.5)
    attendance_percentage = 100.0 if total_days == 0 else round(((effective_present + leave_days) / total_days) * 100, 1)

    # Marks History
    marks = db.execute("""
        SELECT m.*, s.subject_name, e.exam_name 
        FROM marks m
        JOIN subjects s ON m.subject_id = s.id
        JOIN exams e ON m.exam_id = e.id
        WHERE m.student_id=?
        ORDER BY e.exam_date DESC, s.subject_name ASC
    """, (id,)).fetchall()

    # AI Report diagnostics
    ai_report = db.execute("SELECT * FROM ai_reports WHERE student_id=?", (id,)).fetchone()
    if ai_report:
        risk_level = "Low"
        if ai_report["risk_score"] >= 70:
            risk_level = "Critical"
        elif ai_report["risk_score"] >= 50:
            risk_level = "High"
        elif ai_report["risk_score"] >= 30:
            risk_level = "Medium"
        ai_report = dict(ai_report)
        ai_report["risk_level"] = risk_level
       
    # Remarks log
    remarks = db.execute("""
        SELECT r.*, t.name as teacher_name
        FROM teacher_remarks r
        LEFT JOIN teachers t ON r.teacher_id = t.id
        WHERE r.student_id=?
        ORDER BY r.created_at DESC
    """, (id,)).fetchall()

    db.close()

    return render_template(
        "student_detail.html",
        student=student,
        attendance={
            "percentage": attendance_percentage,
            "total": total_days,
            "present": present_days,
            "late": late_days,
            "leave": leave_days,
            "absent": absent_days
        },
        marks=marks,
        remarks=remarks,
        ai_report=ai_report,
        subjects=subjects,
        exams=exams,
        today_date=datetime.now().strftime("%Y-%m-%d")
    )

@app.route("/edit/student/<int:id>", methods=["GET", "POST"])
def edit_student(id):
    if not is_admin():
        return redirect("/")
    db = get_db()
    if request.method == "POST":
        filename = None
        captured_data = request.form.get("captured_image")
        if captured_data and captured_data.startswith("data:image"):
            try:
                header, encoded = captured_data.split(",", 1)
                img_data = base64.b64decode(encoded)
                filename = f"{int(datetime.now().timestamp())}_live.png"
                with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as f:
                    f.write(img_data)
            except Exception as e:
                print(f"Error saving live capture: {e}")
        else:
            file = request.files.get("image")
            if file and file.filename:
                if allowed_file(file.filename):
                    safe_name = secure_filename(file.filename)
                    filename = f"{int(datetime.now().timestamp())}_{safe_name}"
                    file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

        db.execute("""
            UPDATE students SET 
                name=?, stream=?, category=?, details=?,
                father_name=?, mother_name=?,
                phone_father=?, phone_mother=?, phone_student=?,
                register_no=?, class=?,
                adhar_no=?, admission_no=?, bank_acc_no=?, ifsc_code=?,
                state_level_participation=?, admission_type=?, caste_category=?,
                co_curricular=?, nss_scouts_jrc_lk=?, batch_id=?
            WHERE id=?
        """, (
            request.form["name"],
            request.form["stream"],
            request.form["category"],
            request.form["details"],
            request.form.get("father_name", ""),
            request.form.get("mother_name", ""),
            request.form.get("phone_father", ""),
            request.form.get("phone_mother", ""),
            request.form.get("phone_student"),
            request.form.get("register_no", ""),
            request.form.get("class", ""),
            request.form.get("adhar_no", ""),
            request.form.get("admission_no", ""),
            request.form.get("bank_acc_no", ""),
            request.form.get("ifsc_code", ""),
            request.form.get("state_level_participation", ""),
            request.form.get("admission_type", ""),
            request.form.get("caste_category", ""),
            request.form.get("co_curricular", ""),
            request.form.get("nss_scouts_jrc_lk", ""),
            request.form.get("batch_id"),
            id
        ))
        
        if filename:
            db.execute("UPDATE students SET image=? WHERE id=?", (filename, id))
            
        db.commit()
        db.close()
        return redirect("/admin-panel")
    student = db.execute("SELECT * FROM students WHERE id=?", (id,)).fetchone()
    batches = db.execute("SELECT * FROM batches ORDER BY batch_name DESC").fetchall()
    db.close()
    return render_template("edit_student.html", student=student, batches=batches)

@app.route("/delete/student/<int:id>")
def delete_student(id):
    if not is_admin():
        return redirect("/")
    db = get_db()
    db.execute("DELETE FROM students WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/admin-panel")

@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    is_adm = session.get("admin", False)
    is_teach = session.get("user") is not None

    if not is_adm and not is_teach:
        return redirect("/login")

    db = get_db()

    if request.method == "POST":
        type_ = request.form["type"]

        if type_ == "teacher":
            if not is_adm:
                db.close()
                return "Only administrators are authorized to register teachers.", 403
                
            # Hash password for new teachers in DB
            plain_pass = request.form.get("password")
            if not plain_pass:
                plain_pass = "1234"
            pwd_hash = bcrypt.hashpw(plain_pass.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            username = request.form["name"].lower().replace(" ", "")

            db.execute("""
                INSERT INTO teachers (name, subject, details, username, password_hash)
                VALUES (?, ?, ?, ?, ?)
            """, (request.form["name"], request.form["subject"], request.form["details"], username, pwd_hash))

        else:
            filename = ""
            captured_data = request.form.get("captured_image")
            if captured_data and captured_data.startswith("data:image"):
                try:
                    header, encoded = captured_data.split(",", 1)
                    img_data = base64.b64decode(encoded)
                    filename = f"{int(datetime.now().timestamp())}_live.png"
                    with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as f:
                        f.write(img_data)
                except Exception as e:
                    print(f"Error saving live capture: {e}")
            else:
                file = request.files.get("image")
                if file and file.filename:
                    if allowed_file(file.filename):
                        safe_name = secure_filename(file.filename)
                        filename = f"{int(datetime.now().timestamp())}_{safe_name}"
                        file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
                    else:
                        db.close()
                        return "Invalid file type. Only image uploads are permitted.", 400

            batch_id = request.form.get("batch_id")
            if not batch_id:
                active = db.execute("SELECT id FROM batches WHERE is_active=1").fetchone()
                batch_id = active["id"] if active else None

            db.execute("""
            INSERT INTO students
            (name, stream, category, details,
             father_name, mother_name,
             phone_father, phone_mother, phone_student,
             register_no, class, image,
             adhar_no, admission_no, bank_acc_no, ifsc_code,
             state_level_participation, admission_type, caste_category,
             co_curricular, nss_scouts_jrc_lk, batch_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                request.form["name"],
                request.form["stream"],
                request.form["category"],
                request.form["details"],
                request.form["father_name"],
                request.form["mother_name"],
                request.form["phone_father"],
                request.form["phone_mother"],
                request.form.get("phone_student"),
                request.form["register_no"],
                request.form["class"],
                filename,
                request.form.get("adhar_no", ""),
                request.form.get("admission_no", ""),
                request.form.get("bank_acc_no", ""),
                request.form.get("ifsc_code", ""),
                request.form.get("state_level_participation", ""),
                request.form.get("admission_type", ""),
                request.form.get("caste_category", ""),
                request.form.get("co_curricular", ""),
                request.form.get("nss_scouts_jrc_lk", ""),
                batch_id
            ))

        db.commit()
        db.close()
        return redirect(f"/dashboard?success=1&added_type={type_}")

    batches = db.execute("SELECT * FROM batches ORDER BY batch_name DESC").fetchall()
    active_batch = db.execute("SELECT * FROM batches WHERE is_active=1").fetchone()
    db.close()
    return render_template("dashboard.html", batches=batches, active_batch=active_batch)

# ---------- ADMIN PANEL: MANAGE DATA ----------
@app.route("/admin-panel")
def admin_panel():
    if not is_admin():
        return redirect("/")

    db = get_db()
    teachers_list = db.execute("SELECT * FROM teachers").fetchall()
    students_list = db.execute("SELECT * FROM students").fetchall()
    db.close()
    return render_template("admin_panel.html", teachers=teachers_list, students=students_list)

# ---------- ERB CUSTOM MODULES INTEGRATIONS ----------

# 1. ADD REMARKS
@app.route("/student/<int:student_id>/remark", methods=["POST"])
def add_remark(student_id):
    remark_text = request.form["remark"]
    teacher_username = session.get("user")
    db = get_db()
    teacher = db.execute("SELECT id FROM teachers WHERE username=?", (teacher_username,)).fetchone()
    teacher_id = teacher["id"] if teacher else None

    db.execute("""
        INSERT INTO teacher_remarks (student_id, teacher_id, remark)
        VALUES (?, ?, ?)
    """, (student_id, teacher_id, remark_text))
    db.commit()
    db.close()
    return redirect(f"/student/{student_id}")

@app.route("/student/remark/delete/<int:remark_id>")
def delete_remark(remark_id):
    student_id = request.args.get("student_id")
    db = get_db()
    db.execute("DELETE FROM teacher_remarks WHERE id=?", (remark_id,))
    db.commit()
    db.close()
    return redirect(f"/student/{student_id}")

# 2. ATTENDANCE MANAGEMENT ROUTES
@app.route("/attendance", methods=["GET"])
def attendance_view():
    db = get_db()
    students_list = db.execute("SELECT * FROM students ORDER BY name ASC").fetchall()
    classes = sorted(list(set([s["class"] for s in students_list if s["class"]])))

    history = db.execute("""
        SELECT a.*, s.name as student_name 
        FROM attendance a
        JOIN students s ON a.student_id = s.id
        ORDER BY a.attendance_date DESC, s.name ASC
        LIMIT 100
    """).fetchall()

    # Warning calculations
    percentages = db.execute("""
        SELECT student_id,
               (SUM(case status when 'Present' then 1.0 when 'Late' then 0.5 when 'Leave' then 1.0 else 0.0 end) / COUNT(*)) * 100 as pct
        FROM attendance
        GROUP BY student_id
    """).fetchall()
    
    critical_count = sum(1 for p in percentages if p["pct"] < 60)
    danger_count = sum(1 for p in percentages if p["pct"] < 75)
    warning_count = sum(1 for p in percentages if p["pct"] < 80)
    borderline_count = sum(1 for p in percentages if p["pct"] < 90)

    db.close()
    return render_template(
        "attendance.html",
        students=students_list,
        classes=classes,
        history=history,
        critical_count=critical_count,
        danger_count=danger_count,
        warning_count=warning_count,
        borderline_count=borderline_count,
        today_date=datetime.now().strftime("%Y-%m-%d")
    )

@app.route("/attendance/mark", methods=["POST"])
def attendance_mark():
    date_str = request.form["attendance_date"]
    marked_by = session.get("user") or "Admin"
    
    db = get_db()
    students_list = db.execute("SELECT id, name, phone_father, phone_mother FROM students").fetchall()
    
    for s in students_list:
        status_key = f"status_{s['id']}"
        remarks_key = f"remarks_{s['id']}"
        
        if status_key in request.form:
            status = request.form[status_key]
            remarks = request.form.get(remarks_key, "")
            
            existing = db.execute("SELECT id FROM attendance WHERE student_id=? AND attendance_date=?", (s["id"], date_str)).fetchone()
            
            if existing:
                db.execute("UPDATE attendance SET status=?, remarks=?, marked_by=? WHERE id=?", (status, remarks, marked_by, existing["id"]))
            else:
                db.execute("INSERT INTO attendance (student_id, attendance_date, status, remarks, marked_by) VALUES (?, ?, ?, ?, ?)", (s["id"], date_str, status, remarks, marked_by))
            
            pass

    db.commit()
    db.close()
    return redirect("/attendance")

@app.route("/attendance/mark-single", methods=["POST"])
def attendance_mark_single():
    student_id = request.form["student_id"]
    date_str = request.form["attendance_date"]
    status = request.form["status"]
    remarks = request.form.get("remarks", "")
    marked_by = session.get("user") or "Admin"
    
    db = get_db()
    existing = db.execute("SELECT id FROM attendance WHERE student_id=? AND attendance_date=?", (student_id, date_str)).fetchone()
    if existing:
        db.execute("UPDATE attendance SET status=?, remarks=?, marked_by=? WHERE id=?", (status, remarks, marked_by, existing["id"]))
    else:
        db.execute("INSERT INTO attendance (student_id, attendance_date, status, remarks, marked_by) VALUES (?, ?, ?, ?, ?)", (student_id, date_str, status, remarks, marked_by))
    
    db.commit()
    db.close()
    return redirect(f"/student/{student_id}")

@app.route("/attendance/delete/<int:id>")
def attendance_delete(id):
    db = get_db()
    db.execute("DELETE FROM attendance WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/attendance")

# 3. LEAVE MANAGEMENT ROUTES
@app.route("/leaves", methods=["GET"])
def leaves_view():
    db = get_db()
    students_list = db.execute("SELECT id, name, register_no FROM students ORDER BY name ASC").fetchall()
    
    pending_requests = db.execute("""
        SELECT l.*, s.name as student_name 
        FROM leave_requests l
        JOIN students s ON l.student_id = s.id
        WHERE l.approved = 0
        ORDER BY l.leave_date ASC
    """).fetchall()

    history = db.execute("""
        SELECT l.*, s.name as student_name 
        FROM leave_requests l
        JOIN students s ON l.student_id = s.id
        ORDER BY l.created_at DESC
    """).fetchall()
    
    total_count = db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0]
    approved_count = db.execute("SELECT COUNT(*) FROM leave_requests WHERE approved = 1").fetchone()[0]
    pending_count = db.execute("SELECT COUNT(*) FROM leave_requests WHERE approved = 0").fetchone()[0]
    
    db.close()
    return render_template(
        "leaves.html",
        students=students_list,
        pending_requests=pending_requests,
        history=history,
        total_count=total_count,
        approved_count=approved_count,
        pending_count=pending_count
    )

@app.route("/leaves/request", methods=["POST"])
def leaves_request():
    student_id = request.form["student_id"]
    leave_date = request.form["leave_date"]
    reason = request.form["reason"]
    
    db = get_db()
    db.execute("INSERT INTO leave_requests (student_id, leave_date, reason, approved) VALUES (?, ?, ?, 0)", (student_id, leave_date, reason))
    db.commit()
    db.close()
    return redirect("/leaves")

@app.route("/leaves/approve/<int:id>")
def leaves_approve(id):
    db = get_db()
    leave = db.execute("SELECT * FROM leave_requests WHERE id=?", (id,)).fetchone()
    
    if leave:
        student_id = leave["student_id"]
        leave_date = leave["leave_date"]
        
        db.execute("UPDATE leave_requests SET approved=1 WHERE id=?", (id,))
        
        existing = db.execute("SELECT id FROM attendance WHERE student_id=? AND attendance_date=?", (student_id, leave_date)).fetchone()
        marked_by = session.get("user") or "Admin"
        if existing:
            db.execute("UPDATE attendance SET status='Leave', remarks='Leave request approved' WHERE id=?", (existing["id"],))
        else:
            db.execute("INSERT INTO attendance (student_id, attendance_date, status, remarks, marked_by) VALUES (?, ?, 'Leave', 'Leave request approved', ?)", (student_id, leave_date, marked_by))
            
        pass
                
    db.commit()
    db.close()
    return redirect("/leaves")

@app.route("/leaves/reject/<int:id>")
def leaves_reject(id):
    db = get_db()
    db.execute("UPDATE leave_requests SET approved=-1 WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/leaves")

@app.route("/leaves/delete/<int:id>")
def leaves_delete(id):
    db = get_db()
    db.execute("DELETE FROM leave_requests WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/leaves")

# 4. SUBJECT MANAGEMENT ROUTES
@app.route("/subjects", methods=["GET", "POST"])
def subjects_view():
    db = get_db()
    if request.method == "POST":
        name = request.form["subject_name"]
        code = request.form["subject_code"]
        try:
            db.execute("INSERT INTO subjects (subject_name, subject_code) VALUES (?, ?)", (name, code))
            db.commit()
        except sqlite3.IntegrityError:
            pass
        return redirect("/subjects")
    
    edit_id = request.args.get("edit_id")
    edit_subject = None
    if edit_id:
        edit_subject = db.execute("SELECT * FROM subjects WHERE id=?", (edit_id,)).fetchone()
        
    subjects_list = db.execute("SELECT * FROM subjects ORDER BY subject_name ASC").fetchall()
    db.close()
    return render_template("subjects.html", subjects=subjects_list, edit_subject=edit_subject)

@app.route("/subjects/edit/<int:id>", methods=["POST"])
def subjects_edit(id):
    name = request.form["subject_name"]
    code = request.form["subject_code"]
    db = get_db()
    db.execute("UPDATE subjects SET subject_name=?, subject_code=? WHERE id=?", (name, code, id))
    db.commit()
    db.close()
    return redirect("/subjects")

@app.route("/subjects/delete/<int:id>")
def subjects_delete(id):
    db = get_db()
    db.execute("DELETE FROM subjects WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/subjects")

# 5. EXAMINATION MANAGEMENT ROUTES
@app.route("/exams", methods=["GET", "POST"])
def exams_view():
    db = get_db()
    if request.method == "POST":
        name = request.form["exam_name"]
        total = request.form["total_marks"]
        date = request.form["exam_date"]
        db.execute("INSERT INTO exams (exam_name, total_marks, exam_date) VALUES (?, ?, ?)", (name, total, date))
        db.commit()
        return redirect("/exams")
        
    edit_id = request.args.get("edit_id")
    edit_exam = None
    if edit_id:
        edit_exam = db.execute("SELECT * FROM exams WHERE id=?", (edit_id,)).fetchone()
        
    exams_list = db.execute("SELECT * FROM exams ORDER BY exam_date DESC").fetchall()
    db.close()
    return render_template("exams.html", exams=exams_list, edit_exam=edit_exam)

@app.route("/exams/edit/<int:id>", methods=["POST"])
def exams_edit(id):
    name = request.form["exam_name"]
    total = request.form["total_marks"]
    date = request.form["exam_date"]
    db = get_db()
    db.execute("UPDATE exams SET exam_name=?, total_marks=?, exam_date=? WHERE id=?", (name, total, date, id))
    db.commit()
    db.close()
    return redirect("/exams")

@app.route("/exams/delete/<int:id>")
def exams_delete(id):
    db = get_db()
    db.execute("DELETE FROM exams WHERE id=?", (id,))
    db.commit()
    db.close()
    return redirect("/exams")

# 6. MARKS & GRADING MANAGEMENT ROUTES
@app.route("/marks", methods=["GET"])
def marks_view():
    db = get_db()
    students_list = db.execute("SELECT id, name, register_no, stream, class FROM students ORDER BY name ASC").fetchall()
    subjects_list = db.execute("SELECT * FROM subjects ORDER BY subject_name ASC").fetchall()
    exams_list = db.execute("SELECT * FROM exams ORDER BY exam_date DESC").fetchall()
    
    classes = sorted(list(set([s["class"] for s in students_list if s["class"]])))
    
    # Ranks leaderboard calculating average percentage and grade
    rankings_rows = db.execute("""
        SELECT m.student_id, s.name as student_name, s.class, s.stream, e.exam_name,
               SUM(m.marks_obtained) as total_obtained, SUM(m.max_marks) as total_max,
               (SUM(m.marks_obtained) / SUM(m.max_marks)) * 100 as avg_percentage
        FROM marks m
        JOIN students s ON m.student_id = s.id
        JOIN exams e ON m.exam_id = e.id
        GROUP BY m.student_id, m.exam_id
        ORDER BY avg_percentage DESC
    """).fetchall()

    history = db.execute("""
        SELECT m.*, s.name as student_name, sub.subject_name, e.exam_name 
        FROM marks m
        JOIN students s ON m.student_id = s.id
        JOIN subjects sub ON m.subject_id = sub.id
        JOIN exams e ON m.exam_id = e.id
        ORDER BY m.created_at DESC
        LIMIT 100
    """).fetchall()

    db.close()
    return render_template(
        "marks.html",
        students=students_list,
        subjects=subjects_list,
        exams=exams_list,
        classes=classes,
        rankings=rankings_rows,
        history=history
    )

@app.route("/marks/add", methods=["POST"])
def marks_add():
    student_id = request.form["student_id"]
    exam_id = request.form["exam_id"]
    subject_id = request.form["subject_id"]
    obtained = float(request.form["marks_obtained"])
    max_marks = float(request.form["max_marks"])
    entered_by = session.get("user") or "Admin"
    
    db = get_db()
    existing = db.execute("SELECT id FROM marks WHERE student_id=? AND subject_id=? AND exam_id=?", (student_id, subject_id, exam_id)).fetchone()
    if existing:
        db.execute("UPDATE marks SET marks_obtained=?, max_marks=?, entered_by=? WHERE id=?", (obtained, max_marks, entered_by, existing["id"]))
    else:
        db.execute("INSERT INTO marks (student_id, subject_id, exam_id, marks_obtained, max_marks, entered_by) VALUES (?, ?, ?, ?, ?, ?)", (student_id, subject_id, exam_id, obtained, max_marks, entered_by))
    
    pass
            
    db.commit()
    db.close()
    return redirect("/marks")

@app.route("/marks/add-single", methods=["POST"])
def marks_add_single():
    student_id = request.form["student_id"]
    exam_id = request.form["exam_id"]
    subject_id = request.form["subject_id"]
    obtained = float(request.form["marks_obtained"])
    max_marks = float(request.form["max_marks"])
    entered_by = session.get("user") or "Admin"
    
    db = get_db()
    existing = db.execute("SELECT id FROM marks WHERE student_id=? AND subject_id=? AND exam_id=?", (student_id, subject_id, exam_id)).fetchone()
    if existing:
        db.execute("UPDATE marks SET marks_obtained=?, max_marks=?, entered_by=? WHERE id=?", (obtained, max_marks, entered_by, existing["id"]))
    else:
        db.execute("INSERT INTO marks (student_id, subject_id, exam_id, marks_obtained, max_marks, entered_by) VALUES (?, ?, ?, ?, ?, ?)", (student_id, subject_id, exam_id, obtained, max_marks, entered_by))
        
    pass
            
    db.commit()
    db.close()
    return redirect(f"/student/{student_id}")

@app.route("/marks/delete/<int:id>")
def marks_delete(id):
    student_id = request.args.get("student_id")
    db = get_db()
    db.execute("DELETE FROM marks WHERE id=?", (id,))
    db.commit()
    db.close()
    if student_id:
        return redirect(f"/student/{student_id}")
    return redirect("/marks")

# 7. LOCAL OLLAMA AI MONITORING INTERFACE
@app.route("/ai/analyze/<int:student_id>")
def ai_analyze(student_id):
    ai_service.analyze_student(student_id)
    return redirect(f"/student/{student_id}")

# 9. REPORTLAB PDF DISPATCHER
@app.route("/reports/pdf/student/<int:student_id>")
def pdf_student(student_id):
    include_ai = request.args.get("include_ai", "1") == "1"
    include_attendance = request.args.get("include_attendance", "1") == "1"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"student_report_{student_id}.pdf")
    pdf_generator.generate_student_report_pdf(student_id, filepath, include_ai=include_ai, include_attendance=include_attendance)
    return send_file(filepath, as_attachment=True)

@app.route("/reports/pdf/attendance/<int:student_id>")
def pdf_attendance(student_id):
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"attendance_report_{student_id}.pdf")
    pdf_generator.generate_attendance_report_pdf(student_id, filepath)
    return send_file(filepath, as_attachment=True)

@app.route("/reports/pdf/exam/<int:exam_id>")
def pdf_exam(exam_id):
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"exam_report_{exam_id}.pdf")
    pdf_generator.generate_exam_report_pdf(exam_id, filepath)
    return send_file(filepath, as_attachment=True)

@app.route("/reports/pdf/performance/<int:student_id>")
def pdf_performance(student_id):
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"performance_report_{student_id}.pdf")
    pdf_generator.generate_performance_report_pdf(student_id, filepath)
    return send_file(filepath, as_attachment=True)

@app.route("/reports/pdf/ai/<int:student_id>")
def pdf_ai(student_id):
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], f"ai_report_{student_id}.pdf")
    success = pdf_generator.generate_ai_report_pdf(student_id, filepath)
    if not success:
        return "AI report details not found. Please trigger AI analysis first.", 400
    return send_file(filepath, as_attachment=True)

# ---------- ADMIN: SMART AUTO BACKUP DASHBOARD ----------
@app.route("/admin/backups", methods=["GET"])
def admin_backups():
    if not is_admin():
        return redirect("/")
        
    db = get_db()
    system_status = db.execute("SELECT * FROM system_status WHERE id=1").fetchone()
    db.close()
    
    # Read backups files
    backups_list = []
    backups_dir = "backups"
    if os.path.exists(backups_dir):
        for filename in os.listdir(backups_dir):
            if filename.startswith("backup_") and filename.endswith(".db"):
                filepath = os.path.join(backups_dir, filename)
                size_bytes = os.path.getsize(filepath)
                date_str = "Unknown"
                try:
                    parts = filename.split("_")
                    if len(parts) >= 3:
                        dt_part = parts[1] + " " + parts[2].replace(".db", "").replace("-", ":")
                        date_str = dt_part
                except:
                    pass
                backups_list.append({
                    "filename": filename,
                    "date": date_str,
                    "size": f"{size_bytes / 1024.0:.2f} KB" if size_bytes < 1024*1024 else f"{size_bytes / (1024.0*1024.0):.2f} MB"
                })
                
    # Sort backups by filename descending (newest first)
    backups_list.sort(key=lambda x: x["filename"], reverse=True)
    
    # Read logs from logs/backup.log
    backup_logs = []
    log_file = "logs/backup.log"
    if os.path.exists(log_file):
        try:
            with open(log_file, "r") as f:
                backup_logs = f.readlines()[-50:]
        except:
            pass
            
    return render_template(
        "backups.html", 
        backups=backups_list, 
        status=system_status,
        logs="".join(backup_logs)
    )

@app.route("/admin/backups/download/<filename>")
def download_backup(filename):
    if not is_admin():
        return redirect("/")
    
    filename = secure_filename(filename)
    filepath = os.path.join("backups", filename)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True)
    return "Backup file not found", 404

@app.route("/admin/backups/restore/<filename>", methods=["POST"])
def restore_backup_route(filename):
    if not is_admin():
        return redirect("/")
        
    filename = secure_filename(filename)
    import backup_service
    success, result = backup_service.restore_backup(filename)
    
    if success:
        return redirect(f"/admin/backups?success=1&safety={result}")
    else:
        return f"Restore failed: {result}", 500


# ---------- ADMIN: ACADEMIC BATCH MANAGEMENT ----------
@app.route("/admin/batches", methods=["GET", "POST"])
def admin_batches():
    if not is_admin():
        return redirect("/")
    
    db = get_db()
    error_msg = None
    success_msg = None

    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "create":
            name = request.form["batch_name"]
            start = int(request.form["start_year"])
            end = int(request.form["end_year"])
            
            existing = db.execute("SELECT id FROM batches WHERE batch_name=?", (name,)).fetchone()
            if existing:
                error_msg = f"A batch with name '{name}' already exists."
            else:
                db.execute("""
                    INSERT INTO batches (batch_name, start_year, end_year, is_active)
                    VALUES (?, ?, ?, 0)
                """, (name, start, end))
                db.commit()
                success_msg = f"Batch '{name}' created successfully."
                
        elif action == "edit":
            batch_id = int(request.form["batch_id"])
            name = request.form["batch_name"]
            start = int(request.form["start_year"])
            end = int(request.form["end_year"])
            
            existing = db.execute("SELECT id FROM batches WHERE batch_name=? AND id!=?", (name, batch_id)).fetchone()
            if existing:
                error_msg = f"Another batch with name '{name}' already exists."
            else:
                db.execute("""
                    UPDATE batches SET batch_name=?, start_year=?, end_year=? WHERE id=?
                """, (name, start, end, batch_id))
                db.commit()
                success_msg = f"Batch updated successfully."
                
        elif action == "delete":
            batch_id = int(request.form["batch_id"])
            count = db.execute("SELECT COUNT(*) FROM students WHERE batch_id=?", (batch_id,)).fetchone()[0]
            if count > 0:
                error_msg = "Cannot delete batch. There are students registered under this academic batch."
            else:
                db.execute("DELETE FROM batches WHERE id=?", (batch_id,))
                db.commit()
                success_msg = "Batch deleted successfully."
                
        elif action == "activate":
            batch_id = int(request.form["batch_id"])
            db.execute("UPDATE batches SET is_active=0")
            db.execute("UPDATE batches SET is_active=1 WHERE id=?", (batch_id,))
            db.commit()
            success_msg = "Selected batch activated successfully."
            
        elif action == "archive":
            batch_id = int(request.form["batch_id"])
            db.execute("UPDATE batches SET is_active=0 WHERE id=?", (batch_id,))
            db.commit()
            success_msg = "Selected batch archived/deactivated successfully."

    batches_list = db.execute("""
        SELECT b.*, COUNT(s.id) as student_count 
        FROM batches b
        LEFT JOIN students s ON b.id = s.batch_id
        GROUP BY b.id
        ORDER BY b.batch_name DESC
    """).fetchall()
    
    db.close()
    return render_template("batches.html", batches=batches_list, error_msg=error_msg, success_msg=success_msg)


# ---------- ADMIN: STUDENT PROMOTION SYSTEM ----------
@app.route("/admin/promotion", methods=["GET", "POST"])
def admin_promotion():
    if not is_admin():
        return redirect("/")
        
    db = get_db()
    batches = db.execute("SELECT * FROM batches ORDER BY batch_name DESC").fetchall()
    
    current_class = request.args.get("class", "")
    current_batch = request.args.get("batch_id", "")
    
    students_list = []
    if current_class and current_batch:
        students_list = db.execute(
            "SELECT * FROM students WHERE class=? AND batch_id=? ORDER BY name ASC",
            (current_class, int(current_batch))
        ).fetchall()
        
    classes_rows = db.execute("SELECT DISTINCT class FROM students WHERE class IS NOT NULL AND class != ''").fetchall()
    classes = [c["class"] for c in classes_rows]
    
    success_msg = None
    error_msg = None

    if request.method == "POST":
        student_ids = request.form.getlist("student_ids")
        target_class = request.form.get("target_class")
        target_batch_id = request.form.get("target_batch_id")
        
        if not student_ids:
            error_msg = "Please select at least one student to promote."
        elif not target_class:
            error_msg = "Please specify the target Class."
        elif not target_batch_id:
            error_msg = "Please select the target academic Batch."
        else:
            try:
                for s_id in student_ids:
                    db.execute(
                        "UPDATE students SET class=?, batch_id=? WHERE id=?",
                        (target_class, int(target_batch_id), int(s_id))
                    )
                db.commit()
                success_msg = f"Successfully promoted {len(student_ids)} students to {target_class}."
                students_list = []
                current_class = ""
                current_batch = ""
            except Exception as e:
                db.rollback()
                error_msg = f"An error occurred during promotion: {e}"

    db.close()
    return render_template(
        "promotion.html",
        batches=batches,
        classes=classes,
        students=students_list,
        selected_class=current_class,
        selected_batch=current_batch,
        success_msg=success_msg,
        error_msg=error_msg
    )


# ---------- LAUNCH SERVER ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)