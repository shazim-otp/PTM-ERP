import os
import json
import sqlite3
import requests
from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3")
DATABASE_PATH = os.getenv("DATABASE_PATH", "database.db")

def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def fetch_student_data(student_id):
    """
    Fetches all student data including grades, attendance, remarks, and basic info
    to compile a comprehensive context for the AI.
    """
    db = get_db()
    
    # Student Info
    student = db.execute("""
        SELECT s.*, b.batch_name 
        FROM students s 
        LEFT JOIN batches b ON s.batch_id = b.id 
        WHERE s.id=?
    """, (student_id,)).fetchone()
    if not student:
        db.close()
        return None
    student = dict(student)

    # Attendance Info
    attendance_records = db.execute("SELECT * FROM attendance WHERE student_id=?", (student_id,)).fetchall()
    total_days = len(attendance_records)
    present_days = sum(1 for r in attendance_records if r['status'] == 'Present')
    late_days = sum(1 for r in attendance_records if r['status'] == 'Late')
    leave_days = sum(1 for r in attendance_records if r['status'] == 'Leave')
    absent_days = sum(1 for r in attendance_records if r['status'] == 'Absent')
    
    # count late as present but note it, leave is excused
    effective_present = present_days + (late_days * 0.5)
    attendance_percentage = 100.0 if total_days == 0 else round(((effective_present + leave_days) / total_days) * 100, 1)
    
    # Marks Info
    marks_records = db.execute("""
        SELECT m.*, s.subject_name, s.subject_code, e.exam_name, e.exam_date 
        FROM marks m
        JOIN subjects s ON m.subject_id = s.id
        JOIN exams e ON m.exam_id = e.id
        WHERE m.student_id=?
    """, (student_id,)).fetchall()
    
    marks_list = [dict(m) for m in marks_records]
    
    # Leave Requests
    leaves = db.execute("SELECT * FROM leave_requests WHERE student_id=?", (student_id,)).fetchall()
    leave_count = len(leaves)
    approved_leaves = sum(1 for l in leaves if l['approved'] == 1)

    # Teacher Remarks
    remarks = db.execute("""
        SELECT r.*, t.name as teacher_name 
        FROM teacher_remarks r
        LEFT JOIN teachers t ON r.teacher_id = t.id
        WHERE r.student_id=?
        ORDER BY r.created_at DESC
    """, (student_id,)).fetchall()
    remarks_list = [dict(rem) for rem in remarks]
    
    db.close()

    return {
        "student": student,
        "attendance": {
            "percentage": attendance_percentage,
            "total": total_days,
            "present": present_days,
            "late": late_days,
            "leave": leave_days,
            "absent": absent_days
        },
        "marks": marks_list,
        "leaves": {
            "total": leave_count,
            "approved": approved_leaves
        },
        "remarks": remarks_list
    }

def call_ollama(prompt, system_prompt="You are an expert educational AI analyst."):
    """
    Sends a prompt to the local Ollama instance. Falls back to None if unavailable.
    """
    try:
        url = f"{OLLAMA_URL}/api/generate"
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": f"{system_prompt}\n\n{prompt}",
            "stream": False,
            "options": {
                "temperature": 0.3,
                "top_p": 0.9
            }
        }
        response = requests.post(url, json=payload, timeout=8)
        if response.status_code == 200:
            return response.json().get("response", "").strip()
    except Exception as e:
        print(f"Ollama API not available (using rule-based fallback). Error: {e}")
    return None

def calculate_grade(percentage):
    if percentage >= 95: return "A+"
    elif percentage >= 90: return "A"
    elif percentage >= 80: return "B+"
    elif percentage >= 70: return "B"
    elif percentage >= 60: return "C"
    elif percentage >= 50: return "D"
    else: return "F"

def analyze_student(student_id):
    """
    Main entry point for student analysis. Compiles AI context, predicts risk level,
    calculates metrics, and queries Ollama or falls back to rule-based analysis.
    """
    data = fetch_student_data(student_id)
    if not data:
        return None

    # Pre-calculate performance stats for rules/fallback
    att_pct = data["attendance"]["percentage"]
    marks = data["marks"]
    remarks = data["remarks"]
    
    # Calculate subject averages
    subject_marks = {}
    for m in marks:
        sub = m["subject_name"]
        if sub not in subject_marks:
            subject_marks[sub] = []
        subject_marks[sub].append((m["marks_obtained"] / m["max_marks"]) * 100)
    
    subject_averages = {sub: sum(pcts)/len(pcts) for sub, pcts in subject_marks.items()}
    
    # Determine strengths and weaknesses
    strengths = [sub for sub, avg in subject_averages.items() if avg >= 80]
    weak_subjects = [sub for sub, avg in subject_averages.items() if avg < 60]
    
    # Overall performance score
    overall_avg = sum(subject_averages.values()) / len(subject_averages) if subject_averages else 0.0
    performance_score = round(overall_avg, 1)
    
    # Calculate risk score
    # Lower attendance and lower grades increase risk
    attendance_risk = max(0, 100 - att_pct) * 0.4
    academic_risk = max(0, 100 - performance_score) * 0.5
    behavioral_risk = len(data["leaves"]["total"] if isinstance(data["leaves"]["total"], list) else []) * 2 # simple factor
    
    risk_score = round(min(100, attendance_risk + academic_risk), 1)
    
    # Risk Level Category
    if risk_score >= 70 or att_pct < 60 or (performance_score < 40 and performance_score > 0):
        risk_level = "Critical"
    elif risk_score >= 50 or att_pct < 75:
        risk_level = "High"
    elif risk_score >= 30 or att_pct < 85:
        risk_level = "Medium"
    else:
        risk_level = "Low"

    # Context for AI
    ai_context = f"""
    Student Name: {data['student']['name']}
    Batch: {data['student'].get('batch_name') or 'N/A'}
    Class: {data['student']['class']} | Stream: {data['student']['stream']}
    Attendance: {att_pct}% (Total: {data['attendance']['total']}, Present: {data['attendance']['present']}, Absent: {data['attendance']['absent']})
    Subject Performance:
    """
    for sub, avg in subject_averages.items():
        ai_context += f"  - {sub}: {round(avg, 1)}% (Grade: {calculate_grade(avg)})\n"
    
    ai_context += "Teacher Remarks:\n"
    for r in remarks:
        ai_context += f"  - [{r['created_at'][:10]}] {r['teacher_name'] or 'Teacher'}: {r['remark']}\n"
    
    prompt = f"""
    Perform a complete academic and behavioral risk analysis for the following student.
    
    {ai_context}
    
    Please provide your analysis in clean Markdown format with the following sections:
    1. **Performance Summary**: Briefly describe the student's current academic stand.
    2. **Strengths**: List subjects or areas where they excel.
    3. **Weaknesses**: List subjects or areas needing focus.
    4. **Recommendations**: Specific actionable recommendations for teachers and parents.
    5. **Risk Assessment**: Assess their risk of failure or drop-out based on attendance and marks.
    
    Keep the report professional, actionable, and concise. Do not add conversational fillers.
    """
    
    report_text = call_ollama(prompt, system_prompt="You are an advanced AI PTMHSS ERP Assistant analyzing student data.")
    
    # If Ollama is unavailable, generate highly specific rule-based fallback report
    if not report_text:
        report_text = generate_rule_based_report(data, att_pct, performance_score, strengths, weak_subjects, risk_level)

    # Save to database
    db = get_db()
    
    # First delete any existing AI report to avoid bloating
    db.execute("DELETE FROM ai_reports WHERE student_id=?", (student_id,))
    
    # Insert new report
    db.execute("""
        INSERT INTO ai_reports 
        (student_id, risk_score, attendance_score, performance_score, weak_subjects, recommendations, report_text)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        student_id,
        risk_score,
        att_pct,
        performance_score,
        ", ".join(weak_subjects) if weak_subjects else "None",
        "Weekly tutoring; parent check-in" if risk_level in ["High", "Critical"] else "Regular monitoring",
        report_text
    ))
    db.commit()
    db.close()
    
    return {
        "student_id": student_id,
        "risk_level": risk_level,
        "risk_score": risk_score,
        "attendance_score": att_pct,
        "performance_score": performance_score,
        "weak_subjects": weak_subjects,
        "strengths": strengths,
        "report_text": report_text
    }

def generate_rule_based_report(data, att_pct, performance_score, strengths, weak_subjects, risk_level):
    student_name = data['student']['name']
    batch_name = data['student'].get('batch_name') or 'N/A'
    
    # Construct Teacher Remarks text
    remarks_str = ""
    if data["remarks"]:
        for r in data["remarks"]:
            remarks_str += f"- *{r['remark']}* (by {r['teacher_name'] or 'Teacher'})\n"
    else:
        remarks_str = "- No remarks recorded yet.\n"

    # Construct strengths/weaknesses
    strengths_str = ", ".join(strengths) if strengths else "General academic subjects"
    weakness_str = ", ".join(weak_subjects) if weak_subjects else "No critical weaknesses identified"
    
    # Construct recommendations based on risk
    if risk_level == "Critical":
        recs = "- **Immediate Intervention**: Arrange a mandatory parent-teacher meeting.\n- **Academic Support**: Enroll in remedial classes for weak subjects.\n- **Attendance Watch**: Strict daily monitoring of presence with automatic SMS alerts."
    elif risk_level == "High":
        recs = "- **Tutoring Support**: Provide extra guidance in weak subjects.\n- **Attendance Alert**: Contact parents regarding low school attendance.\n- **Regular Reviews**: Class teacher should review progress bi-weekly."
    elif risk_level == "Medium":
        recs = "- **Classroom Monitoring**: Teachers should seat student closer to the front.\n- **Extra Practice**: Solve worksheets for moderate subjects.\n- **Encouragement**: Boost participation in school activities."
    else:
        recs = "- **Enrichment**: Encourage participation in Olympiads or advanced projects.\n- **Peer Tutoring**: Allow them to assist classmates to reinforce knowledge.\n- **Maintain Consistency**: Keep up the excellent work ethic."

    report_tpl = f"""### AI-Generated Student Performance Report
**Student Name:** {student_name}
**Batch:** {batch_name}
**Attendance Percentage:** {att_pct}%
**Overall Academic Score:** {performance_score}% (Average)
**Risk Level:** {risk_level}

---

#### 1. Performance Summary
{student_name} exhibits a {risk_level.lower()}-risk academic profile. Their overall average is {performance_score}% with an attendance rate of {att_pct}%. Academic trends show steady participation, but attention should be paid to any weak areas.

#### 2. Strengths
- **Academic Strengths:** {strengths_str}
- **Positive Aspects:** Demonstrates consistent understanding of core concepts in these topics.

#### 3. Weaknesses
- **Areas of Concern:** {weakness_str}
- **Attendance Concerns:** {"Attendance is below the required 75% threshold. Needs urgent review." if att_pct < 75 else "Attendance is healthy and meets school standards."}

#### 4. Teacher Remarks
{remarks_str}

#### 5. AI Recommendations
{recs}
"""
    return report_tpl

def generate_report(student_id):
    """Triggers analysis and returns the report_text from DB (or generates it if missing)."""
    db = get_db()
    row = db.execute("SELECT report_text FROM ai_reports WHERE student_id=?", (student_id,)).fetchone()
    db.close()
    if row:
        return row[0]
    res = analyze_student(student_id)
    return res["report_text"] if res else "No data available to generate report."

def predict_risk(student_id):
    """Predicts risk score and classification category."""
    res = analyze_student(student_id)
    if res:
        return {
            "risk_score": res["risk_score"],
            "risk_level": res["risk_level"]
        }
    return {"risk_score": 0, "risk_level": "Unknown"}

def suggest_improvements(student_id):
    """Finds recommendations for the student."""
    res = analyze_student(student_id)
    if res:
        # Extract suggestions from report text or return fallback
        return "Weekly review and focused tutoring in weak subjects."
    return "No suggestions available."

def analyze_attendance(student_id):
    """Specific attendance analysis and warnings."""
    data = fetch_student_data(student_id)
    if not data:
        return "No attendance data found."
    
    att_pct = data["attendance"]["percentage"]
    if att_pct < 60:
        return f"Critical attendance issue: {att_pct}% (Severe absences)."
    elif att_pct < 75:
        return f"Warning: Low attendance at {att_pct}%. Below the 75% minimum threshold."
    elif att_pct < 90:
        return f"Moderate: Attendance is {att_pct}%. Improvement recommended."
    else:
        return f"Excellent: Attendance is {att_pct}%."

def analyze_marks(student_id):
    """Specific performance analysis."""
    data = fetch_student_data(student_id)
    if not data or not data["marks"]:
        return "No academic marks found."
    
    marks = data["marks"]
    averages = {}
    for m in marks:
        sub = m["subject_name"]
        pct = (m["marks_obtained"] / m["max_marks"]) * 100
        if sub not in averages:
            averages[sub] = []
        averages[sub].append(pct)
        
    sub_avgs = {s: sum(p)/len(p) for s, p in averages.items()}
    failing = [s for s, a in sub_avgs.items() if a < 50]
    
    if failing:
        return f"Academic Alert: Failing or critical grades in: {', '.join(failing)}."
    return "Academic Status: Satisfactory progress in all registered exams."
