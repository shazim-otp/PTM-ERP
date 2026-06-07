import os
import shutil
import sqlite3
from datetime import datetime
import logging
from apscheduler.schedulers.background import BackgroundScheduler

DATABASE_PATH = os.getenv("DATABASE_PATH", "database.db")
BACKUPS_DIR = "backups"
LOGS_DIR = "logs"
LOG_FILE = os.path.join(LOGS_DIR, "backup.log")

# Create directories
os.makedirs(BACKUPS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Configure Logging
logger = logging.getLogger("backup_service")
logger.setLevel(logging.INFO)
# Clear old handlers if any to avoid duplicate logs in Flask reload
if logger.hasHandlers():
    logger.handlers.clear()
file_handler = logging.FileHandler(LOG_FILE)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def backup_db_api(source_path, target_path):
    """
    Safely clones SQLite database using the native SQLite backup API
    to avoid file locks or corruption.
    """
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(target_path)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

def create_backup(is_maintenance=False):
    """
    Creates a new backup inside backups/ directory and handles retention.
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"backup_{timestamp}.db"
        target_path = os.path.join(BACKUPS_DIR, filename)

        backup_db_api(DATABASE_PATH, target_path)

        # Update last_backup_time in system_status
        db = get_db()
        db.execute("""
            UPDATE system_status 
            SET last_backup_time = datetime('now', 'localtime'),
                backup_mode = ?
            WHERE id = 1
        """, ("maintenance" if is_maintenance else "auto",))
        db.commit()
        db.close()

        # Log action
        size_bytes = os.path.getsize(target_path)
        size_kb = size_bytes / 1024.0
        mode_str = "Maintenance Backup" if is_maintenance else "Automated Backup"
        logger.info(f"Backup Created: {filename} ({size_kb:.2f} KB) - Mode: {mode_str}")

        # Clean old backups (keep last 100)
        clean_old_backups()
        return filename

    except Exception as e:
        logger.error(f"Error creating backup: {e}")
        return None

def clean_old_backups():
    """
    Retains only the 100 most recent backups, deleting older ones.
    """
    try:
        files = [f for f in os.listdir(BACKUPS_DIR) if f.startswith("backup_") and f.endswith(".db")]
        # Sorting alphabetically is identical to sorting by datetime string format YYYY-MM-DD_HH-MM-SS
        files.sort()

        if len(files) > 100:
            to_delete = files[:-100]
            for filename in to_delete:
                file_path = os.path.join(BACKUPS_DIR, filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Backup Deleted: {filename} (Retention policy limit exceeded)")
    except Exception as e:
        logger.error(f"Error during backup retention cleanup: {e}")

def restore_backup(backup_filename):
    """
    Restores the database from a backup file after performing a safety backup.
    """
    try:
        source_path = os.path.join(BACKUPS_DIR, backup_filename)
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Selected backup file not found: {backup_filename}")

        # 1. Create safety backup first
        safety_filename = create_backup(is_maintenance=False)
        if not safety_filename:
            raise RuntimeError("Failed to create safety backup prior to restore operation.")

        # 2. Restore selected backup into active database
        backup_db_api(source_path, DATABASE_PATH)

        # 3. Update timestamps in restored database to show restore completed
        db = get_db()
        db.execute("""
            UPDATE system_status 
            SET last_database_change = datetime('now', 'localtime'),
                last_backup_time = datetime('now', 'localtime'),
                backup_mode = 'restore'
            WHERE id = 1
        """,)
        db.commit()
        db.close()

        logger.info(f"Restore Performed: Restored database using backup '{backup_filename}'. Safety backup saved as '{safety_filename}'.")
        return True, safety_filename
    except Exception as e:
        err_msg = f"Error during restore operation: {e}"
        logger.error(err_msg)
        return False, err_msg

# Hourly job checker
def check_hourly_backup():
    try:
        db = get_db()
        status = db.execute("SELECT last_database_change, last_backup_time FROM system_status WHERE id=1").fetchone()
        db.close()

        if status:
            last_change = status['last_database_change']
            last_backup = status['last_backup_time']

            # If changed since last backup, trigger one
            if not last_backup or not last_change or last_change > last_backup:
                logger.info("Database changes detected since last backup. Initiating backup...")
                create_backup(is_maintenance=False)
            else:
                # Log that check occurred but no changes
                pass
    except Exception as e:
        logger.error(f"Error checking hourly backup: {e}")

# 48 hours job checker
def create_maintenance_backup():
    logger.info("Executing scheduled 48-hour maintenance backup...")
    create_backup(is_maintenance=True)

# Scheduler Initialization
scheduler = None

def init_scheduler():
    global scheduler
    if scheduler is not None:
        return scheduler

    scheduler = BackgroundScheduler()
    # Every hour: check if database changed since last backup. If changed: create backup.
    scheduler.add_job(check_hourly_backup, 'interval', hours=1, id='hourly_backup_check')
    
    # Every 48 hours: create backup regardless of changes.
    scheduler.add_job(create_maintenance_backup, 'interval', hours=48, id='maintenance_backup')

    scheduler.start()
    logger.info("Backup Service Scheduler successfully initialized and started.")
    return scheduler
