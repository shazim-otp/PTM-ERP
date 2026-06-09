import sqlite3
import os
import json
import bcrypt

def init_database():
    db_path = "database.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Ensure students and teachers tables exist
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        stream TEXT,
        category TEXT,
        details TEXT,
        father_name TEXT,
        mother_name TEXT,
        phone_father TEXT,
        phone_mother TEXT,
        phone_student TEXT,
        register_no TEXT,
        class TEXT,
        image TEXT,
        adhar_no TEXT,
        admission_no TEXT,
        bank_acc_no TEXT,
        ifsc_code TEXT,
        state_level_participation TEXT,
        admission_type TEXT,
        caste_category TEXT,
        co_curricular TEXT,
        nss_scouts_jrc_lk TEXT,
        dob TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        subject TEXT,
        details TEXT,
        username TEXT UNIQUE,
        password_hash TEXT
    )
    """)

    # 2. Check and add columns to teachers table if upgrading
    cursor.execute("PRAGMA table_info(teachers)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if "username" not in columns:
        cursor.execute("ALTER TABLE teachers ADD COLUMN username TEXT")
        print("Added column 'username' to 'teachers' table.")
    if "password_hash" not in columns:
        cursor.execute("ALTER TABLE teachers ADD COLUMN password_hash TEXT")
        print("Added column 'password_hash' to 'teachers' table.")

    # 3. Check and add columns to students table if upgrading
    cursor.execute("PRAGMA table_info(students)")
    student_columns = [col[1] for col in cursor.fetchall()]
    new_student_cols = {
        "phone_student": "TEXT",
        "adhar_no": "TEXT",
        "admission_no": "TEXT",
        "bank_acc_no": "TEXT",
        "ifsc_code": "TEXT",
        "state_level_participation": "TEXT",
        "admission_type": "TEXT",
        "caste_category": "TEXT",
        "co_curricular": "TEXT",
        "nss_scouts_jrc_lk": "TEXT",
        "dob": "TEXT"
    }
    for col_name, col_type in new_student_cols.items():
        if col_name not in student_columns:
            cursor.execute(f"ALTER TABLE students ADD COLUMN {col_name} {col_type}")
            print(f"Added column '{col_name}' to 'students' table.")

    # 3.5 Create Batches and System Status tables
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_name TEXT UNIQUE,
        start_year INTEGER,
        end_year INTEGER,
        is_active INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_status (
        id INTEGER PRIMARY KEY,
        last_database_change TEXT,
        last_backup_time TEXT,
        backup_mode TEXT
    )
    """)

    # Seed system_status with single default row
    cursor.execute("""
    INSERT OR IGNORE INTO system_status (id, last_database_change, last_backup_time, backup_mode)
    VALUES (1, datetime('now', 'localtime'), datetime('now', 'localtime'), 'auto')
    """)

    # Check and add batch_id to students table
    cursor.execute("PRAGMA table_info(students)")
    student_columns = [col[1] for col in cursor.fetchall()]
    if "batch_id" not in student_columns:
        cursor.execute("ALTER TABLE students ADD COLUMN batch_id INTEGER REFERENCES batches(id)")
        print("Added column 'batch_id' to 'students' table.")

    # Seed default active batch 2025-26
    cursor.execute("SELECT id FROM batches WHERE batch_name='2025-26'")
    batch_row = cursor.fetchone()
    if not batch_row:
        cursor.execute("""
        INSERT INTO batches (batch_name, start_year, end_year, is_active)
        VALUES ('2025-26', 2025, 2026, 1)
        """)
        cursor.execute("SELECT id FROM batches WHERE batch_name='2025-26'")
        default_batch_id = cursor.fetchone()[0]
    else:
        default_batch_id = batch_row[0]

    # Assign existing students to the default batch 2025-26
    cursor.execute("UPDATE students SET batch_id=? WHERE batch_id IS NULL", (default_batch_id,))

    # Create change tracking triggers on database tables
    monitored_tables = ['students', 'teachers', 'attendance', 'marks', 'subjects', 'exams']
    for table in monitored_tables:
        for op in ['insert', 'update', 'delete']:
            trigger_name = f"track_{table}_{op}"
            cursor.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            cursor.execute(f"""
            CREATE TRIGGER {trigger_name} AFTER {op.upper()} ON {table}
            BEGIN
                UPDATE system_status SET last_database_change = datetime('now', 'localtime') WHERE id = 1;
            END;
            """)

    # 4. Create new tables

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        attendance_date TEXT,
        status TEXT,
        remarks TEXT,
        marked_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS leave_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        leave_date TEXT,
        reason TEXT,
        approved INTEGER DEFAULT 0, -- 0 = Pending, 1 = Approved, -1 = Rejected
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_name TEXT UNIQUE,
        subject_code TEXT UNIQUE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_name TEXT,
        total_marks INTEGER,
        exam_date TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        subject_id INTEGER,
        exam_id INTEGER,
        marks_obtained REAL,
        max_marks REAL,
        entered_by TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id),
        FOREIGN KEY (subject_id) REFERENCES subjects(id),
        FOREIGN KEY (exam_id) REFERENCES exams(id),
        UNIQUE(student_id, subject_id, exam_id)
    )
    """)

    pass

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ai_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        risk_score REAL,
        attendance_score REAL,
        performance_score REAL,
        weak_subjects TEXT,
        recommendations TEXT,
        report_text TEXT,
        generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS teacher_remarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        teacher_id INTEGER,
        remark TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (student_id) REFERENCES students(id)
    )
    """)

    # 5. Populate default subjects
    default_subjects = [
        ("Mathematics", "MATH101"),
        ("Physics", "PHYS101"),
        ("Chemistry", "CHEM101"),
        ("Biology", "BIOL101"),
        ("Computer Science", "CS101"),
        ("English", "ENGL101"),
        ("Commerce", "COMM101")
    ]
    for sub_name, sub_code in default_subjects:
        try:
            cursor.execute("INSERT INTO subjects (subject_name, subject_code) VALUES (?, ?)", (sub_name, sub_code))
        except sqlite3.IntegrityError:
            pass # Subject already exists

    # 6. Import teachers from teachers.json and seed database
    teachers_file = "teachers.json"
    if os.path.exists(teachers_file):
        try:
            with open(teachers_file) as f:
                data = json.load(f)
                for t in data.get("teachers", []):
                    username = t["username"]
                    plain_password = t["password"]
                    
                    # Check if teacher already exists in table
                    cursor.execute("SELECT id FROM teachers WHERE username=?", (username,))
                    teacher_row = cursor.fetchone()
                    
                    # Generate hash
                    pwd_hash = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                    
                    if not teacher_row:
                        # Insert teacher into DB
                        name = username.capitalize()
                        cursor.execute("""
                        INSERT INTO teachers (name, subject, details, username, password_hash)
                        VALUES (?, ?, ?, ?, ?)
                        """, (name, "General", "Imported from teachers.json", username, pwd_hash))
                        print(f"Imported and hashed password for teacher: {username}")
                    else:
                        # Check if password_hash is empty or update it anyway to ensure it matches
                        cursor.execute("UPDATE teachers SET password_hash=? WHERE username=?", (pwd_hash, username))
                        print(f"Updated password hash for teacher: {username}")
        except Exception as e:
            print(f"Error seeding teachers: {e}")

    conn.commit()
    conn.close()
    print("Database initialization and schema upgrade complete.")

if __name__ == "__main__":
    init_database()