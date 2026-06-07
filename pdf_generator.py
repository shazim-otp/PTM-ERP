import os
import re
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "database.db")

def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Helper to fetch student full data
def get_student_details(student_id):
    db = get_db()
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
    
    # Get attendance count
    attendance_records = db.execute("SELECT * FROM attendance WHERE student_id=?", (student_id,)).fetchall()
    total_days = len(attendance_records)
    present_days = sum(1 for r in attendance_records if r['status'] == 'Present')
    late_days = sum(1 for r in attendance_records if r['status'] == 'Late')
    leave_days = sum(1 for r in attendance_records if r['status'] == 'Leave')
    absent_days = sum(1 for r in attendance_records if r['status'] == 'Absent')
    
    effective_present = present_days + (late_days * 0.5)
    attendance_percentage = 100.0 if total_days == 0 else round(((effective_present + leave_days) / total_days) * 100, 1)
    
    # Get marks
    marks = db.execute("""
        SELECT m.*, s.subject_name, s.subject_code, e.exam_name, e.exam_date 
        FROM marks m
        JOIN subjects s ON m.subject_id = s.id
        JOIN exams e ON m.exam_id = e.id
        WHERE m.student_id=?
    """, (student_id,)).fetchall()
    
    # Get AI report
    ai_report = db.execute("SELECT * FROM ai_reports WHERE student_id=?", (student_id,)).fetchone()
    
    # Get Remarks
    remarks = db.execute("""
        SELECT r.*, t.name as teacher_name 
        FROM teacher_remarks r
        LEFT JOIN teachers t ON r.teacher_id = t.id
        WHERE r.student_id=?
        ORDER BY r.created_at DESC
    """, (student_id,)).fetchall()
    
    db.close()
    
    return {
        "student": student,
        "attendance": {
            "percentage": attendance_percentage,
            "total": total_days,
            "present": present_days,
            "late": late_days,
            "leave": leave_days,
            "absent": absent_days,
            "records": [dict(r) for r in attendance_records]
        },
        "marks": [dict(m) for m in marks],
        "ai_report": dict(ai_report) if ai_report else None,
        "remarks": [dict(rem) for rem in remarks]
    }

def get_pdf_styles():
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#1e293b'),
        spaceAfter=15
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#64748b'),
        spaceAfter=25
    )
    
    h2_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor('#0f172a'),
        spaceBefore=15,
        spaceAfter=10
    )
    
    body_style = ParagraphStyle(
        'BodyTextCustom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#334155'),
        spaceAfter=8
    )

    header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=12,
        textColor=colors.white
    )
    
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=11,
        textColor=colors.HexColor('#334155')
    )

    alert_style = ParagraphStyle(
        'AlertText',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=12,
        textColor=colors.HexColor('#b91c1c')
    )

    return {
        "title": title_style,
        "subtitle": subtitle_style,
        "h2": h2_style,
        "body": body_style,
        "header": header_style,
        "cell": cell_style,
        "alert": alert_style
    }

def calculate_grade(percentage):
    if percentage >= 95: return "A+"
    elif percentage >= 90: return "A"
    elif percentage >= 80: return "B+"
    elif percentage >= 70: return "B"
    elif percentage >= 60: return "C"
    elif percentage >= 50: return "D"
    else: return "F"

def generate_student_report_pdf(student_id, filepath, include_ai=True, include_attendance=True):
    """
    Generates a full academic transcript and student overview.
    """
    data = get_student_details(student_id)
    if not data:
        return False
        
    doc = SimpleDocTemplate(filepath, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    sty = get_pdf_styles()
    
    # Title & Metadata
    story.append(Paragraph(f"Academic Report Card: {data['student']['name']}", sty["title"]))
    story.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | PTMHSS ERP", sty["subtitle"]))
    
    # Load student image if it exists
    student_img = None
    image_filename = data['student'].get('image')
    if image_filename:
        image_path = os.path.join("static/uploads", image_filename)
        if os.path.exists(image_path):
            try:
                # Scale image to 1.1 x 1.1 inches (or similar)
                student_img = Image(image_path, width=1.1*inch, height=1.1*inch)
                student_img.hAlign = 'CENTER'
            except Exception as e:
                print(f"Error loading image in PDF: {e}")
                
    # Section: General Info
    story.append(Paragraph("Student Profile Details", sty["h2"]))
    
    # Map all new fields and handle None / default values
    reg_no = data['student'].get('register_no') or 'N/A'
    batch_name = data['student'].get('batch_name') or 'N/A'
    adm_no = data['student'].get('admission_no') or 'N/A'
    adm_type = (data['student'].get('admission_type') or 'N/A').capitalize()
    caste_cat = (data['student'].get('caste_category') or 'N/A').upper()
    adhar = data['student'].get('adhar_no') or 'N/A'
    
    bank_acc = data['student'].get('bank_acc_no') or 'N/A'
    ifsc = data['student'].get('ifsc_code') or 'N/A'
    bank_details = f"{bank_acc} (IFSC: {ifsc})" if bank_acc != 'N/A' else 'N/A'
    
    co_curr = data['student'].get('co_curricular') or 'N/A'
    nss_etc = data['student'].get('nss_scouts_jrc_lk') or 'N/A'
    state_part = data['student'].get('state_level_participation') or 'N/A'
    
    cls_val = data['student'].get('class') or 'N/A'
    stream_val = (data['student'].get('stream') or 'N/A').capitalize()
    cat_val = (data['student'].get('category') or 'N/A').upper()
    
    father = data['student'].get('father_name') or 'N/A'
    mother = data['student'].get('mother_name') or 'N/A'
    f_phone = data['student'].get('phone_father') or 'N/A'
    s_phone = data['student'].get('phone_student') or 'N/A'
    
    info_data = [
        [Paragraph("<b>Register No / Batch:</b>", sty["body"]), Paragraph(f"{reg_no} / {batch_name}", sty["cell"]),
         Paragraph("<b>Class / Stream:</b>", sty["body"]), Paragraph(f"{cls_val} / {stream_val} ({cat_val})", sty["cell"])],
         
        [Paragraph("<b>Admission No:</b>", sty["body"]), Paragraph(str(adm_no), sty["cell"]),
         Paragraph("<b>Admission Type:</b>", sty["body"]), Paragraph(adm_type, sty["cell"])],
         
        [Paragraph("<b>Aadhaar Number:</b>", sty["body"]), Paragraph(str(adhar), sty["cell"]),
         Paragraph("<b>Caste Category:</b>", sty["body"]), Paragraph(caste_cat, sty["cell"])],
         
        [Paragraph("<b>Father's Name:</b>", sty["body"]), Paragraph(father, sty["cell"]),
         Paragraph("<b>Mother's Name:</b>", sty["body"]), Paragraph(mother, sty["cell"])],
         
        [Paragraph("<b>Father's Phone:</b>", sty["body"]), Paragraph(f_phone, sty["cell"]),
         Paragraph("<b>Student Phone:</b>", sty["body"]), Paragraph(s_phone, sty["cell"])],
         
        [Paragraph("<b>Bank A/C Details:</b>", sty["body"]), Paragraph(bank_details, sty["cell"]),
         Paragraph("<b>Co-Curricular:</b>", sty["body"]), Paragraph(co_curr, sty["cell"])],
         
        [Paragraph("<b>NSS/Scouts/JRC/LK:</b>", sty["body"]), Paragraph(nss_etc, sty["cell"]),
         Paragraph("<b>State Level Part.:</b>", sty["body"]), Paragraph(state_part, sty["cell"])]
    ]
    
    # If the student has extra details, we add it as well
    extra_details = data['student'].get('details')
    if extra_details:
        info_data.append([
            Paragraph("<b>Extra Details:</b>", sty["body"]), Paragraph(extra_details, sty["cell"]),
            Paragraph("", sty["body"]), Paragraph("", sty["cell"])
        ])
        
    info_table = Table(info_data, colWidths=[1.3*inch, 1.6*inch, 1.3*inch, 1.6*inch])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
    ]))
    
    # Side-by-side Layout with student image
    if student_img:
        # Details take 5.8 inches, image takes 1.7 inches (with padding)
        layout_table = Table([[info_table, student_img]], colWidths=[5.8*inch, 1.7*inch])
    else:
        # If no image, render details full-width
        layout_table = Table([[info_table, '']], colWidths=[5.8*inch, 1.7*inch])
        
    layout_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (1,0), (1,0), 'CENTER'),
        ('LEFTPADDING', (1,0), (1,0), 10),
        ('RIGHTPADDING', (1,0), (1,0), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
    ]))
    
    story.append(layout_table)
    story.append(Spacer(1, 15))
    
    # Section: Attendance Summary (Always shown briefly)
    story.append(Paragraph("Attendance Summary", sty["h2"]))
    att = data["attendance"]
    att_text = f"The student has attended <b>{att['present']}</b> days present and <b>{att['absent']}</b> days absent out of <b>{att['total']}</b> school days. Current attendance percentage is <b>{att['percentage']}%</b>."
    story.append(Paragraph(att_text, sty["body"]))
    
    if att['percentage'] < 75:
        story.append(Paragraph("⚠️ WARNING: Attendance is below the minimum required threshold of 75%.", sty["alert"]))
        
    story.append(Spacer(1, 15))
    
    # Section: Academic Marks
    story.append(Paragraph("Examination Marks & Grades", sty["h2"]))
    if not data["marks"]:
        story.append(Paragraph("No exam marks have been entered for this student yet.", sty["body"]))
    else:
        marks_headers = [
            Paragraph("Exam", sty["header"]),
            Paragraph("Subject", sty["header"]),
            Paragraph("Marks Obtained", sty["header"]),
            Paragraph("Max Marks", sty["header"]),
            Paragraph("Percentage", sty["header"]),
            Paragraph("Grade", sty["header"])
        ]
        
        marks_rows = [marks_headers]
        for m in data["marks"]:
            pct = round((m["marks_obtained"] / m["max_marks"]) * 100, 1) if m["max_marks"] > 0 else 0
            marks_rows.append([
                Paragraph(m["exam_name"], sty["cell"]),
                Paragraph(m["subject_name"], sty["cell"]),
                Paragraph(str(m["marks_obtained"]), sty["cell"]),
                Paragraph(str(m["max_marks"]), sty["cell"]),
                Paragraph(f"{pct}%", sty["cell"]),
                Paragraph(calculate_grade(pct), sty["cell"])
            ])
            
        marks_table = Table(marks_rows, colWidths=[1.5*inch, 1.5*inch, 1.2*inch, 1.0*inch, 1.0*inch, 0.8*inch])
        marks_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('TOPPADDING', (0,0), (-1,-1), 6),
        ]))
        story.append(marks_table)

    story.append(Spacer(1, 15))
    
    # Section: Detailed Attendance Log Table (Conditional)
    if include_attendance:
        story.append(Paragraph("Detailed Attendance Log History", sty["h2"]))
        records = data["attendance"]["records"]
        if not records:
            story.append(Paragraph("No detailed attendance records found for this student.", sty["body"]))
        else:
            table_rows = [[
                Paragraph("Date", sty["header"]),
                Paragraph("Status", sty["header"]),
                Paragraph("Remarks", sty["header"]),
                Paragraph("Marked By", sty["header"])
            ]]
            # Sort by date descending
            for r in sorted(records, key=lambda x: x["attendance_date"], reverse=True):
                status_style = sty["cell"]
                if r["status"] == "Absent":
                    status_style = ParagraphStyle('AbsentSty', parent=sty["cell"], textColor=colors.HexColor('#dc2626'), fontName='Helvetica-Bold')
                elif r["status"] == "Late":
                    status_style = ParagraphStyle('LateSty', parent=sty["cell"], textColor=colors.HexColor('#d97706'), fontName='Helvetica-Bold')
                elif r["status"] == "Leave":
                    status_style = ParagraphStyle('LeaveSty', parent=sty["cell"], textColor=colors.HexColor('#2563eb'), fontName='Helvetica-Bold')
                    
                table_rows.append([
                    Paragraph(r["attendance_date"], sty["cell"]),
                    Paragraph(r["status"], status_style),
                    Paragraph(r["remarks"] or "None", sty["cell"]),
                    Paragraph(str(r["marked_by"] or "N/A"), sty["cell"])
                ])
                
            t = Table(table_rows, colWidths=[1.8*inch, 1.2*inch, 3.0*inch, 1.5*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
                ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 4),
            ]))
            story.append(t)
        story.append(Spacer(1, 15))

    # Section: AI Analysis & Risk Score (Conditional)
    if include_ai and data["ai_report"]:
        story.append(Paragraph("AI-Powered Performance Insights", sty["h2"]))
        ai = data["ai_report"]
        story.append(Paragraph(f"<b>AI Academic Risk Score:</b> {ai['risk_score']}/100", sty["body"]))
        story.append(Paragraph(f"<b>Core Recommendations:</b> {ai['recommendations']}", sty["body"]))
        story.append(Spacer(1, 5))
        story.append(Paragraph("<b>Detailed AI Report:</b>", sty["body"]))
        
        # Simple rendering of markdown report text by paragraphs
        lines = ai["report_text"].split('\n')
        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("###") or line_str.startswith("####"):
                header_text = line_str.replace("####", "").replace("###", "").strip()
                story.append(Paragraph(f"<b>{header_text}</b>", sty["body"]))
            else:
                line_str = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line_str)
                story.append(Paragraph(line_str, sty["cell"]))
    
    doc.build(story)
    return True

def generate_attendance_report_pdf(student_id, filepath):
    """
    Generates a student attendance log sheet.
    """
    data = get_student_details(student_id)
    if not data:
        return False
        
    doc = SimpleDocTemplate(filepath, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    sty = get_pdf_styles()
    
    story.append(Paragraph(f"Attendance Log: {data['student']['name']}", sty["title"]))
    story.append(Paragraph(f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Attendance Percentage: {data['attendance']['percentage']}%", sty["subtitle"]))
    
    story.append(Paragraph("Attendance Record Breakdown", sty["h2"]))
    
    records = data["attendance"]["records"]
    if not records:
        story.append(Paragraph("No attendance records found for this student.", sty["body"]))
    else:
        table_rows = [[
            Paragraph("Date", sty["header"]),
            Paragraph("Status", sty["header"]),
            Paragraph("Remarks", sty["header"]),
            Paragraph("Marked By", sty["header"])
        ]]
        
        for r in sorted(records, key=lambda x: x["attendance_date"], reverse=True):
            status_style = sty["cell"]
            if r["status"] == "Absent":
                status_style = ParagraphStyle('AbsentSty', parent=sty["cell"], textColor=colors.HexColor('#dc2626'), fontName='Helvetica-Bold')
            elif r["status"] == "Late":
                status_style = ParagraphStyle('LateSty', parent=sty["cell"], textColor=colors.HexColor('#d97706'), fontName='Helvetica-Bold')
            elif r["status"] == "Leave":
                status_style = ParagraphStyle('LeaveSty', parent=sty["cell"], textColor=colors.HexColor('#2563eb'), fontName='Helvetica-Bold')
                
            table_rows.append([
                Paragraph(r["attendance_date"], sty["cell"]),
                Paragraph(r["status"], status_style),
                Paragraph(r["remarks"] or "None", sty["cell"]),
                Paragraph(str(r["marked_by"] or "N/A"), sty["cell"])
            ])
            
        t = Table(table_rows, colWidths=[2.0*inch, 1.5*inch, 2.5*inch, 1.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('TOPPADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        
    doc.build(story)
    return True

def generate_exam_report_pdf(exam_id, filepath):
    """
    Generates a summary of an exam, including class ranks and scores.
    """
    db = get_db()
    exam = db.execute("SELECT * FROM exams WHERE id=?", (exam_id,)).fetchone()
    if not exam:
        db.close()
        return False
    exam = dict(exam)
    
    # Fetch all marks for this exam
    records = db.execute("""
        SELECT m.*, s.name as student_name, s.register_no, sub.subject_name
        FROM marks m
        JOIN students s ON m.student_id = s.id
        JOIN subjects sub ON m.subject_id = sub.id
        WHERE m.exam_id=?
        ORDER BY s.name ASC
    """, (exam_id,)).fetchall()
    
    db.close()
    
    doc = SimpleDocTemplate(filepath, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    sty = get_pdf_styles()
    
    story.append(Paragraph(f"Exam Report: {exam['exam_name']}", sty["title"]))
    story.append(Paragraph(f"Exam Date: {exam['exam_date']} | Out of: {exam['total_marks']} Marks", sty["subtitle"]))
    
    story.append(Paragraph("Student Performance Log", sty["h2"]))
    
    if not records:
        story.append(Paragraph("No marks have been recorded for this exam yet.", sty["body"]))
    else:
        table_rows = [[
            Paragraph("Reg No", sty["header"]),
            Paragraph("Student Name", sty["header"]),
            Paragraph("Subject", sty["header"]),
            Paragraph("Marks Obtained", sty["header"]),
            Paragraph("Percentage", sty["header"]),
            Paragraph("Grade", sty["header"])
        ]]
        
        for r in records:
            pct = round((r["marks_obtained"] / r["max_marks"]) * 100, 1) if r["max_marks"] > 0 else 0
            table_rows.append([
                Paragraph(str(r["register_no"] or 'N/A'), sty["cell"]),
                Paragraph(r["student_name"], sty["cell"]),
                Paragraph(r["subject_name"], sty["cell"]),
                Paragraph(f"{r['marks_obtained']} / {r['max_marks']}", sty["cell"]),
                Paragraph(f"{pct}%", sty["cell"]),
                Paragraph(calculate_grade(pct), sty["cell"])
            ])
            
        t = Table(table_rows, colWidths=[1.0*inch, 2.0*inch, 1.5*inch, 1.5*inch, 1.0*inch, 0.5*inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1e293b')),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('TOPPADDING', (0,0), (-1,-1), 5),
        ]))
        story.append(t)
        
    doc.build(story)
    return True

def generate_performance_report_pdf(student_id, filepath, include_ai=True, include_attendance=True):
    """
    Generates a student-wise detailed performance breakdown.
    """
    return generate_student_report_pdf(student_id, filepath, include_ai=include_ai, include_attendance=include_attendance)

def generate_ai_report_pdf(student_id, filepath):
    """
    Generates a PDF focusing strictly on the AI report content.
    """
    data = get_student_details(student_id)
    if not data or not data["ai_report"]:
        return False
        
    doc = SimpleDocTemplate(filepath, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    sty = get_pdf_styles()
    
    story.append(Paragraph(f"AI Performance Analytics: {data['student']['name']}", sty["title"]))
    story.append(Paragraph(f"Generated: {data['ai_report']['generated_at']} | Model: LOCAL AI", sty["subtitle"]))
    
    story.append(Paragraph("AI Student Monitoring Overview", sty["h2"]))
    
    metrics = [
        [Paragraph("<b>Risk Level:</b>", sty["body"]), Paragraph(str(data['ai_report']['risk_score']) + "% Risk Score", sty["alert"] if data['ai_report']['risk_score'] >= 50 else sty["cell"])],
        [Paragraph("<b>Attendance Score:</b>", sty["body"]), Paragraph(f"{data['ai_report']['attendance_score']}%", sty["cell"])],
        [Paragraph("<b>Academic Score:</b>", sty["body"]), Paragraph(f"{data['ai_report']['performance_score']}%", sty["cell"])],
        [Paragraph("<b>Weak Subjects:</b>", sty["body"]), Paragraph(data['ai_report']['weak_subjects'] or 'None', sty["cell"])],
        [Paragraph("<b>Primary Recommendation:</b>", sty["body"]), Paragraph(data['ai_report']['recommendations'], sty["cell"])],
    ]
    t = Table(metrics, colWidths=[2.0*inch, 5.0*inch])
    t.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#f1f5f9')),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 15))
    
    story.append(Paragraph("Full AI Report Analysis Text", sty["h2"]))
    
    # Parse Markdown blocks
    lines = data['ai_report']['report_text'].split('\n')
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        if line_str.startswith("###") or line_str.startswith("####"):
            header_text = line_str.replace("####", "").replace("###", "").strip()
            story.append(Paragraph(f"<b>{header_text}</b>", sty["body"]))
        else:
            line_str = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', line_str)
            story.append(Paragraph(line_str, sty["cell"]))
            
    doc.build(story)
    return True
