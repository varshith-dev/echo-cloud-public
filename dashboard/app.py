import os
import json
from flask import Flask, jsonify, request, session, redirect, url_for, send_from_directory, make_response
from flask_cors import CORS
import time
import subprocess
import pg_wrapper as sqlite3
import string
import datetime
import random
import secrets
import hashlib
import boto3
import uuid
import mimetypes
from functools import wraps

app = Flask(__name__, static_folder='static')
from flask_cors import CORS
CORS(app)
app.secret_key = 'oqens_production_secret_key_2026_!@#' # Fixed key for multi-worker support
app.permanent_session_lifetime = datetime.timedelta(hours=48)
app.config['SESSION_COOKIE_NAME'] = 'oqens_session'
app.config['SESSION_COOKIE_DOMAIN'] = '.oqens.me'

DB_FILE = os.environ.get('DATABASE_URL', 'postgresql://oqens_user:oqens_pass@localhost/oqens')

@app.teardown_request
def _return_db_connections(exc):
    """
    Automatically return any open DB connections to the pool at the end
    of every request — even if an exception was raised. This is the
    primary defence against pool exhaustion from leaked connections.
    """
    try:
        import pg_wrapper
        pg_wrapper.close_thread_connections()
    except Exception:
        pass
ADMIN_SECRET = '6069'

pay_transient_cache = {}

def obfuscate_token(token_hex):
    if not token_hex or len(token_hex) != 24:
        return token_hex
    try:
        val = int(token_hex, 16)
        # XOR with a secret constant
        secret_xor = 0x5a3f9e2d7c1b8a9f0e2d3c4b
        obf_val = val ^ secret_xor
        
        # Base36 encode
        chars = []
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        temp = obf_val
        while temp > 0:
            temp, r = divmod(temp, len(alphabet))
            chars.append(alphabet[r])
        return "".join(reversed(chars))
    except Exception:
        return token_hex

def deobfuscate_token(obf_str):
    if not obf_str:
        return None
    if len(obf_str) == 24:
        try:
            int(obf_str, 16)
            return obf_str
        except ValueError:
            pass
    try:
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        val = 0
        for char in obf_str:
            val = val * len(alphabet) + alphabet.index(char)
        secret_xor = 0x5a3f9e2d7c1b8a9f0e2d3c4b
        token_val = val ^ secret_xor
        token_hex = f"{token_val:024x}"
        return token_hex
    except Exception:
        return None

# Define dynamic storage directory (OCI VM /opt/dashboard/storage, fallback to local directory)
STORAGE_BASE_DIR = '/opt/dashboard/storage'
if not os.path.exists(STORAGE_BASE_DIR):
    try:
        os.makedirs(STORAGE_BASE_DIR, exist_ok=True)
    except Exception:
        STORAGE_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'storage')
        os.makedirs(STORAGE_BASE_DIR, exist_ok=True)

def generate_cloud_id():
    letter1 = random.choice(string.ascii_uppercase)
    digits1 = "".join(random.choices(string.digits, k=3))
    letters2 = "".join(random.choices(string.ascii_uppercase, k=4))
    digits2 = "".join(random.choices(string.digits, k=3))
    return f"oq-{letter1}{digits1}-{letters2}-{digits2}"

def init_db():
    return # Postgres tables were pre-migrated via pgloader

def run_migrations():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS checkout_token_expires_at TEXT")
        conn.commit()
    except Exception as e:
        print("Migration warning (checkout_token_expires_at):", e)
        conn.rollback()

    try:
        c.execute("ALTER TABLE shared_albums ADD COLUMN IF NOT EXISTS sharing_type TEXT DEFAULT 'public'")
        conn.commit()
    except Exception as e:
        print("Migration warning (sharing_type):", e)
        conn.rollback()
        
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS album_access_requests (
            id SERIAL PRIMARY KEY,
            share_token TEXT,
            requester_username TEXT,
            requester_email TEXT,
            details TEXT,
            status TEXT DEFAULT 'pending',
            approval_token TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    except Exception as e:
        print("Migration error (album_access_requests):", e)
        conn.rollback()
        
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS shared_album_access (
            id SERIAL PRIMARY KEY,
            share_token TEXT,
            email TEXT,
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    except Exception as e:
        print("Migration error (shared_album_access):", e)
        conn.rollback()

    try:
        c.execute('''CREATE TABLE IF NOT EXISTS analytics_datasets (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE,
            target_type TEXT,
            target_value TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    except Exception as e:
        print("Migration error (analytics_datasets):", e)
        conn.rollback()

    try:
        c.execute('''CREATE TABLE IF NOT EXISTS analytics_logs (
            id SERIAL PRIMARY KEY,
            dataset_id INTEGER REFERENCES analytics_datasets(id) ON DELETE CASCADE,
            ip_address TEXT,
            country TEXT,
            city TEXT,
            isp TEXT,
            device_type TEXT,
            device_name TEXT,
            os_name TEXT,
            browser_name TEXT,
            battery_level REAL,
            connection_type TEXT,
            source_url TEXT,
            referrer_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    except Exception as e:
        print("Migration error (analytics_logs):", e)
        conn.rollback()
        
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS subscription_plans (
            id SERIAL PRIMARY KEY,
            plan_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            monthly_rate REAL NOT NULL,
            storage_limit_gb REAL NOT NULL,
            bandwidth_limit_gb REAL NOT NULL,
            features TEXT,
            status TEXT DEFAULT 'Active'
        )''')
        c.execute("INSERT INTO subscription_plans (plan_id, name, monthly_rate, storage_limit_gb, bandwidth_limit_gb, features) VALUES ('free', 'Free Plan', 0.0, 0.1, 0.5, '[\"100 MB Storage\", \"500 MB Bandwidth\"]') ON CONFLICT (plan_id) DO NOTHING")
        c.execute("INSERT INTO subscription_plans (plan_id, name, monthly_rate, storage_limit_gb, bandwidth_limit_gb, features) VALUES ('starter', 'Starter Plan', 57.0, 5.0, 10.0, '[\"5 GB Storage\", \"10 GB Bandwidth\", \"Custom Domain Mapping\"]') ON CONFLICT (plan_id) DO NOTHING")
        c.execute("INSERT INTO subscription_plans (plan_id, name, monthly_rate, storage_limit_gb, bandwidth_limit_gb, features) VALUES ('pro', 'Pro Plan', 137.0, 10.0, 25.0, '[\"10 GB Storage\", \"25 GB Bandwidth\", \"Multiple Custom Domains\"]') ON CONFLICT (plan_id) DO NOTHING")
        conn.commit()
    except Exception as e:
        print("Migration error (subscription_plans):", e)
        conn.rollback()

    try:
        c.execute('''CREATE TABLE IF NOT EXISTS plan_enquiries (
            id SERIAL PRIMARY KEY,
            email TEXT,
            mobile TEXT,
            storage_req TEXT,
            bandwidth_req TEXT,
            message TEXT,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    except Exception as e:
        print("Migration error (plan_enquiries):", e)
        conn.rollback()
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_tenants_checkout_token ON tenants(checkout_token)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tenants_cloud_id ON tenants(cloud_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tenants_custom_domain ON tenants(custom_domain)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tenants_secret_code ON tenants(secret_code)")
        conn.commit()
    except Exception as e:
        print("Migration error (indexes):", e)
        conn.rollback()
        
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id SERIAL PRIMARY KEY,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            execute_after TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    except Exception as e:
        print("Migration error (scheduled_tasks table):", e)
        conn.rollback()
        
    try:
        c.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS papers_enabled BOOLEAN DEFAULT FALSE")
        c.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS photos_enabled BOOLEAN DEFAULT FALSE")
        conn.commit()
    except Exception as e:
        print("Migration warning (app_access_columns):", e)
        conn.rollback()

    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status ON scheduled_tasks(status)")
        conn.commit()
    except Exception as e:
        print("Migration error (scheduled_tasks):", e)
        conn.rollback()
        
    conn.close()

@app.errorhandler(404)
def not_found_error(error):
    return app.send_static_file('error_404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return app.send_static_file('error_500.html'), 500

@app.errorhandler(403)
def forbidden_error(error):
    return app.send_static_file('error_403.html'), 403

@app.errorhandler(429)
def ratelimit_error(error):
    return app.send_static_file('error_429.html'), 429

run_migrations()

def init_db():
    return # Postgres tables were pre-migrated via pgloader
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        secret_code TEXT UNIQUE,
        storage_limit_bytes INTEGER,
        storage_used_bytes INTEGER DEFAULT 0
    )''')
    
    # Dynamically add tracking columns for metrics and API credentials if they do not exist
    columns_to_add = [
        ('downloads_count', 'INTEGER DEFAULT 0'),
        ('unique_views_count', 'INTEGER DEFAULT 0'),
        ('bandwidth_bytes', 'INTEGER DEFAULT 0'),
        ('bandwidth_limit_bytes', 'INTEGER DEFAULT 0'),
        ('cloud_id', 'TEXT'),
        ('api_key', 'TEXT'),
        ('custom_domain', 'TEXT'),
        ('custom_domain_email', 'TEXT'),
        ('custom_domain_verified', 'INTEGER DEFAULT 0'),
        ('password', 'TEXT'),
        ('first_login_time', 'TEXT'),
        ('password_setup_token', 'TEXT'),
        ('block_downloads', 'INTEGER DEFAULT 0'),
        ('block_uploads', 'INTEGER DEFAULT 0'),
        ('monthly_rate', 'REAL DEFAULT 0.0'),
        ('auto_renew', 'INTEGER DEFAULT 1'),
        ('next_billing_date', 'TEXT'),
        ('billing_status', 'TEXT DEFAULT "Active"'),
        ('email_verified', 'INTEGER DEFAULT 0'),
        ('email_verification_token', 'TEXT'),
        ('checkout_token', 'TEXT'),
        ('is_deleted', 'INTEGER DEFAULT 0'),
        ('tier', 'TEXT DEFAULT "free"'),
        ('allot_collections', 'INTEGER DEFAULT 0'),
        ('allot_backgrounds', 'INTEGER DEFAULT 0'),
        ('custom_message', 'TEXT'),
        ('custom_message_theme', 'TEXT DEFAULT "danger"'),
        ('custom_message_icon', 'TEXT DEFAULT "ph-warning-circle"'),
        ('custom_message_active', 'INTEGER DEFAULT 0')
    ]
    for col_name, col_type in columns_to_add:
        try:
            c.execute(f'ALTER TABLE tenants ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass # Already exists
    
    try:
        c.execute('ALTER TABLE tenants ADD COLUMN granted_pages_limit INTEGER')
    except sqlite3.OperationalError:
        pass
            
    c.execute('''CREATE TABLE IF NOT EXISTS device_views (
        tenant_username TEXT,
        device_id TEXT,
        PRIMARY KEY (tenant_username, device_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER,
        title TEXT,
        category TEXT,
        status TEXT DEFAULT 'Open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER,
        sender TEXT,
        message TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS support_team (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        secret_code TEXT UNIQUE,
        email TEXT UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    support_cols = [
        ('email', 'TEXT'),
        ('password', 'TEXT'),
        ('first_login_time', 'TEXT'),
        ('password_setup_token', 'TEXT')
    ]
    for col_name, col_type in support_cols:
        try:
            c.execute(f'ALTER TABLE support_team ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass
    
    c.execute('''CREATE TABLE IF NOT EXISTS file_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_username TEXT,
        filename TEXT,
        tag TEXT,
        UNIQUE(tenant_username, filename, tag)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    # Seed default plan pricing configuration
    default_plan_configs = [
        ('plan_starter_rate', '57.0'),
        ('plan_starter_storage_gb', '5'),
        ('plan_starter_bandwidth_gb', '10'),
        ('plan_pro_rate', '137.0'),
        ('plan_pro_storage_gb', '10'),
        ('plan_pro_bandwidth_gb', '25'),
        ('plan_writer_rate', '19.0'),
        ('plan_writer_storage_gb', '1'),
        ('plan_writer_bandwidth_gb', '5'),
        ('plan_writer_pages', '10')
    ]
    for key, val in default_plan_configs:
        c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)', (key, val))
    
    # Create support polls tables
    c.execute('''CREATE TABLE IF NOT EXISTS support_polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT,
        options TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS support_poll_votes (
        poll_id INTEGER,
        voter_id TEXT,
        selected_option TEXT,
        PRIMARY KEY (poll_id, voter_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS billing_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER,
        amount REAL,
        date TEXT,
        status TEXT,
        description TEXT,
        FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )''')
    
    # Dynamically add search / payment details tracking columns to billing_transactions if they do not exist
    tx_columns_to_add = [
        ('payment_id', 'TEXT'),
        ('utr', 'TEXT'),
        ('email', 'TEXT')
    ]
    for col_name, col_type in tx_columns_to_add:
        try:
            c.execute(f'ALTER TABLE billing_transactions ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass # Already exists
    
    c.execute('''CREATE TABLE IF NOT EXISTS system_domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT UNIQUE NOT NULL,
        email TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'Active'
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS coupons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        discount_type TEXT NOT NULL,
        discount_value REAL NOT NULL,
        expiry_date TEXT,
        max_uses INTEGER DEFAULT -1,
        used_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'Active'
    )''')
    
    # Dynamically add targeting columns to coupons if they do not exist
    coupon_columns_to_add = [
        ('target_type', 'TEXT DEFAULT "global"'),
        ('target_tenants', 'TEXT'),
        ('target_plan', 'TEXT DEFAULT "all"')
    ]
    for col_name, col_type in coupon_columns_to_add:
        try:
            c.execute(f'ALTER TABLE coupons ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass # Already exists

    # Referral columns on tenants
    referral_tenant_cols = [
        ('referral_code', 'TEXT'),
        ('referral_benefit_expires', 'TEXT'),
        ('display_tier', 'TEXT')
    ]
    for col_name, col_type in referral_tenant_cols:
        try:
            c.execute(f'ALTER TABLE tenants ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass

    # Referral links table
    c.execute('''CREATE TABLE IF NOT EXISTS referral_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        label TEXT NOT NULL,
        granted_tier TEXT DEFAULT 'starter',
        display_tier TEXT,
        granted_storage_gb REAL,
        granted_bandwidth_gb REAL,
        granted_duration_days INTEGER DEFAULT -1,
        max_uses INTEGER DEFAULT -1,
        used_count INTEGER DEFAULT 0,
        link_expires_at TEXT,
        status TEXT DEFAULT 'Active',
        created_at TEXT
    )''')

    # Referral uses table
    c.execute('''CREATE TABLE IF NOT EXISTS referral_uses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referral_code TEXT NOT NULL,
        tenant_id INTEGER NOT NULL,
        tenant_username TEXT,
        device_fingerprint TEXT,
        ip_address TEXT,
        activated_at TEXT,
        benefit_expires_at TEXT
    )''')
            
    c.execute('''CREATE TABLE IF NOT EXISTS mail_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        subject TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    # Seed default templates
    default_templates = [
        ("Welcome Onboarding", "Welcome to OQENS, {{name}}!", """<div style="font-family: 'Inter', sans-serif; padding: 20px; max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
    <h2 style="color: #1a202c; border-bottom: 1px solid #e2e8f0; padding-bottom: 10px;">Welcome to OQENS!</h2>
    <p>Hello <strong>{{name}}</strong>,</p>
    <p>Thank you for choosing OQENS Cloud. Your media node is active and ready for use.</p>
    <div style="background-color: #f7fafc; border-left: 4px solid #3182ce; padding: 15px; margin: 20px 0; border-radius: 4px;">
        <p style="margin: 0 0 8px 0;"><strong>Your Account Details:</strong></p>
        <p style="margin: 0 0 4px 0;">Cloud ID: <code>{{cloud_id}}</code></p>
        <p style="margin: 0 0 4px 0;">Storage Plan: {{plan}}</p>
        <p style="margin: 0;">Registered Email: {{email}}</p>
    </div>
    <p>To access your dashboard, navigate to: <a href="https://dash.echo.oqens.me" style="color: #3182ce; text-decoration: none; font-weight: 500;">dash.echo.oqens.me</a></p>
    <p style="color: #718096; font-size: 0.85em; margin-top: 30px; border-top: 1px solid #e2e8f0; padding-top: 15px;">If you have any questions, please reach out via support.oqens.me</p>
</div>"""),
        ("Subscription Invoice", "OQENS Subscription Payment Success", """<div style="font-family: 'Inter', sans-serif; padding: 20px; max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
    <h2 style="color: #48bb78; border-bottom: 1px solid #e2e8f0; padding-bottom: 10px;">Payment Confirmation</h2>
    <p>Hello <strong>{{name}}</strong>,</p>
    <p>We've successfully processed your recurring subscription renewal payment.</p>
    <div style="background-color: #f7fafc; border-left: 4px solid #48bb78; padding: 15px; margin: 20px 0; border-radius: 4px;">
        <p style="margin: 0 0 8px 0;"><strong>Transaction Summary:</strong></p>
        <p style="margin: 0 0 4px 0;">Account Node: <code>{{cloud_id}}</code></p>
        <p style="margin: 0 0 4px 0;">Active Plan: {{plan}}</p>
        <p style="margin: 0 0 4px 0;">Billing Date: {{dateofexpiry}}</p>
        <p style="margin: 0;">Payment Status: <strong style="color: #48bb78;">{{paymentstatus}}</strong></p>
    </div>
    <p>Your subscription is active and will auto-renew on <strong>{{dateofexpiry}}</strong>.</p>
    <p style="color: #718096; font-size: 0.85em; margin-top: 30px; border-top: 1px solid #e2e8f0; padding-top: 15px;">Thank you for using OQENS!</p>
</div>"""),
        ("Suspension Notice", "ACTION REQUIRED: OQENS Cloud Subscription Suspended", """<div style="font-family: 'Inter', sans-serif; padding: 20px; max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
    <h2 style="color: #e53e3e; border-bottom: 1px solid #e2e8f0; padding-bottom: 10px;">Subscription Suspended</h2>
    <p>Hello <strong>{{name}}</strong>,</p>
    <p style="color: #e53e3e; font-weight: 500;">Your subscription is currently suspended due to a missed renewal payment.</p>
    <div style="background-color: #fffaf0; border-left: 4px solid #dd6b20; padding: 15px; margin: 20px 0; border-radius: 4px;">
        <p style="margin: 0 0 8px 0;"><strong>Details:</strong></p>
        <p style="margin: 0 0 4px 0;">Account: <code>{{cloud_id}}</code></p>
        <p style="margin: 0 0 4px 0;">Current Status: <strong style="color: #e53e3e;">{{paymentstatus}}</strong></p>
        <p style="margin: 0;">Expiry Date: {{dateofexpiry}}</p>
    </div>
    <p>Please resolve this immediately to restore storage access and avoid deletion of files. Access your portal to make a payment.</p>
    <p style="color: #718096; font-size: 0.85em; margin-top: 30px; border-top: 1px solid #e2e8f0; padding-top: 15px;">If you have any questions, submit a support ticket via support.oqens.me</p>
</div>""")
    ]
    for name, subject, body in default_templates:
        try:
            c.execute('INSERT OR IGNORE INTO mail_templates (name, subject, body) VALUES (?, ?, ?)', (name, subject, body))
        except Exception:
            pass
        
    c.execute('''CREATE TABLE IF NOT EXISTS mail_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient TEXT NOT NULL,
        subject TEXT NOT NULL,
        status TEXT NOT NULL,
        error_message TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    
    c.execute('''CREATE TABLE IF NOT EXISTS markdown_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_username TEXT NOT NULL,
        title TEXT NOT NULL,
        slug TEXT NOT NULL,
        content TEXT DEFAULT "",
        is_published INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(tenant_username, slug)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS markdown_collections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_username TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT "",
        slug TEXT NOT NULL,
        created_at TEXT,
        UNIQUE(tenant_username, slug)
    )''')

    page_cols_to_add = [
        ('collection_id', 'INTEGER DEFAULT NULL'),
        ('custom_background', 'TEXT DEFAULT ""')
    ]
    for col_name, col_type in page_cols_to_add:
        try:
            c.execute(f'ALTER TABLE markdown_pages ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass

    # Dynamic Feature Flags & Collection Access tables
    c.execute('''CREATE TABLE IF NOT EXISTS feature_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        flag_key TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT "",
        created_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS tenant_features (
        tenant_id INTEGER NOT NULL,
        feature_id INTEGER NOT NULL,
        PRIMARY KEY(tenant_id, feature_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS collection_access (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collection_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'view',
        notified INTEGER DEFAULT 0,
        UNIQUE(collection_id, email)
    )''')

    # Seed default features
    default_features = [
        (1, 'Collections Feature', 'allot_collections', 'Organize papers into collections'),
        (2, 'Custom Backgrounds', 'allot_backgrounds', 'Set custom CSS backgrounds for papers')
    ]
    for fid, name, flag, desc in default_features:
        try:
            c.execute('INSERT OR IGNORE INTO feature_flags (id, name, flag_key, description, created_at) VALUES (?, ?, ?, ?, ?)',
                      (fid, name, flag, desc, datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')))
        except Exception as e:
            pass

    c.execute('''CREATE TABLE IF NOT EXISTS page_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        comment TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''')

    # Alter collections table
    collection_cols = [
        ('visibility', "TEXT DEFAULT 'public'"),
        ('public_role', "TEXT DEFAULT 'view'"),
        ('banner_image', "TEXT DEFAULT NULL"),
        ('theme_color', "TEXT DEFAULT '#8b6a3e'")
    ]
    for col_name, col_type in collection_cols:
        try:
            c.execute(f'ALTER TABLE markdown_collections ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass

    # Migrate legacy allotments
    try:
        c.execute('SELECT id, allot_collections, allot_backgrounds FROM tenants')
        rows = c.fetchall()
        for row in rows:
            tenant_id, coll, bg = row
            if coll == 1:
                c.execute('INSERT OR IGNORE INTO tenant_features (tenant_id, feature_id) VALUES (?, 1)', (tenant_id,))
            if bg == 1:
                c.execute('INSERT OR IGNORE INTO tenant_features (tenant_id, feature_id) VALUES (?, 2)', (tenant_id,))
    except Exception as e:
        pass

    today_str = datetime.date.today().strftime('%Y-%m-%d')
    c.execute('INSERT OR IGNORE INTO system_domains (domain, email, created_at, status) VALUES (\'echo.oqens.me\', \'varshith.code@gmail.com\', ?, \'Active\')', (today_str,))
    c.execute('INSERT OR IGNORE INTO system_domains (domain, email, created_at, status) VALUES (\'host.echo.oqens.me\', \'varshith.code@gmail.com\', ?, \'Active\')', (today_str,))
    c.execute('INSERT OR IGNORE INTO system_domains (domain, email, created_at, status) VALUES (\'payments.oqens.me\', \'varshith.code@gmail.com\', ?, \'Active\')', (today_str,))
    c.execute('INSERT OR IGNORE INTO system_domains (domain, email, created_at, status) VALUES (\'papers.oqens.me\', \'varshith.code@gmail.com\', ?, \'Active\')', (today_str,))
    
    # Seed default mail config values if not present
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'mailman_access_code\', \'D5GV-MA9Z-Y26Z\')')
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'mailman_secret_key\', \'mailman_secret_key_placeholder\')')
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'mailman_base_url\', \'https://mailman.oqens.me\')')
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'host_email\', "varshith.code@gmail.com")')
    
    # Seed Cashfree credentials and mode
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'cashfree_app_id\', \'TEST10294101cc297120df077fae98f410149201\')')
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'cashfree_secret_key\', \'TEST6aef5e79beeead9c0e44cfd5df57c7f1a3a311db\')')
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'cashfree_mode\', \'sandbox\')')
    c.execute('INSERT OR IGNORE INTO system_config (key, value) VALUES (\'cos_engine_ip\', \'127.0.0.1\')')
    
    conn.commit()
    
    # Auto-migrate: populate cloud_id for any existing tenants that lack one
    c.execute('SELECT id, username FROM tenants WHERE cloud_id IS NULL OR cloud_id = ''')
    rows = c.fetchall()
    if rows:
        for row in rows:
            tid, username = row
            while True:
                cid = generate_cloud_id()
                c.execute('SELECT id FROM tenants WHERE cloud_id = ?', (cid,))
                if not c.fetchone():
                    break
            c.execute('UPDATE tenants SET cloud_id = ? WHERE id = ?', (cid, tid))
        conn.commit()
        
    # Auto-migrate next_billing_date and billing_status for existing tenants
    c.execute('SELECT id FROM tenants WHERE next_billing_date IS NULL OR next_billing_date = ''')
    rows_date = c.fetchall()
    if rows_date:
        default_next_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        for row in rows_date:
            c.execute('UPDATE tenants SET next_billing_date = ?, billing_status = \'Active\' WHERE id = ?', (default_next_date, row[0]))
        conn.commit()
        
    # Auto-migrate checkout_token for existing tenants
    c.execute('SELECT id, username FROM tenants WHERE checkout_token IS NULL OR checkout_token = ''')
    rows_token = c.fetchall()
    if rows_token:
        import secrets
        for row in rows_token:
            tid, username = row
            while True:
                token = secrets.token_hex(8)
                c.execute('SELECT id FROM tenants WHERE checkout_token = ?', (token,))
                if not c.fetchone():
                    break
            c.execute('UPDATE tenants SET checkout_token = ? WHERE id = ?', (token, tid))
        conn.commit()
        
    # Status Monitoring Tables
    c.execute('''CREATE TABLE IF NOT EXISTS status_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at TEXT UNIQUE,
        api_status TEXT,   -- 'operational', 'degraded', 'outage'
        cos_status TEXT,   -- 'operational', 'degraded', 'outage'
        nginx_status TEXT, -- 'operational', 'degraded', 'outage'
        db_status TEXT     -- 'operational', 'degraded', 'outage'
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS status_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        description TEXT,
        status TEXT,       -- 'Investigating', 'Identified', 'Monitoring', 'Resolved'
        created_at TEXT,
        resolved_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

def get_tenant_storage_dir():
    if session.get('role') == 'tenant':
        username = session.get('tenant_username')
    else:
        username = 'admin_files'
    
    # Safe guard
    if not username:
        username = 'unknown'
        
    tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
    os.makedirs(tenant_dir, exist_ok=True)
    return tenant_dir

def recalculate_tenant_usage(tenant_id, tenant_username):
    tenant_dir = os.path.join(STORAGE_BASE_DIR, tenant_username)
    total_size = 0
    if os.path.exists(tenant_dir):
        for root, dirs, files in os.walk(tenant_dir):
            for file in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, file))
                except Exception:
                    pass
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET storage_used_bytes = ? WHERE id = ?', (total_size, tenant_id))
    conn.commit()
    conn.close()
    return total_size

def update_tenant_usage(tenant_id, bytes_diff):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET storage_used_bytes = MAX(0, storage_used_bytes + ?) WHERE id = ?', (bytes_diff, tenant_id))
    conn.commit()
    conn.close()

def get_plan_config(plan_name):
    if plan_name == 'free':
        return {
            'rate': 0.0,
            'storage_limit': 100 * 1024 * 1024,
            'bandwidth_limit': 500 * 1024 * 1024,
            'pages_limit': 0
        }
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT monthly_rate, storage_limit_gb, bandwidth_limit_gb FROM subscription_plans WHERE plan_id = %s AND status = 'Active'", (plan_name,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {
            'rate': float(row[0]),
            'storage_limit': int(float(row[1]) * 1024 * 1024 * 1024),
            'bandwidth_limit': int(float(row[2]) * 1024 * 1024 * 1024),
            'storage_gb': float(row[1]),
            'bandwidth_gb': float(row[2]),
            'pages_limit': 50 if plan_name == 'starter' else 100
        }
    else:
        # Fallback values if plan not found
        return {
            'rate': 57.0 if plan_name == 'starter' else 137.0,
            'storage_limit': 5 * 1024 * 1024 * 1024 if plan_name == 'starter' else 10 * 1024 * 1024 * 1024,
            'bandwidth_limit': 10 * 1024 * 1024 * 1024 if plan_name == 'starter' else 25 * 1024 * 1024 * 1024,
            'storage_gb': 5.0 if plan_name == 'starter' else 10.0,
            'bandwidth_gb': 10.0 if plan_name == 'starter' else 25.0,
            'pages_limit': 50 if plan_name == 'starter' else 100
        }

def generate_invoice_pdf(tenant, amount, billing_date):
    import io
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
    except ImportError:
        return b""
        
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=40, leftMargin=40,
                            topMargin=40, bottomMargin=40)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'InvoiceTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=15
    )
    
    normal_style = ParagraphStyle(
        'InvoiceNormal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#334155')
    )
    
    bold_style = ParagraphStyle(
        'InvoiceBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#0f172a')
    )
    
    header_style = ParagraphStyle(
        'InvoiceHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=colors.white
    )

    story.append(Paragraph("OQENS INVOICE / RECEIPT", title_style))
    story.append(Spacer(1, 15))
    
    import datetime
    inv_num = f"INV-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    meta_data = [
        [Paragraph("<b>Bill To:</b>", normal_style), Paragraph("<b>Invoice Details:</b>", normal_style)],
        [Paragraph(f"Workspace Name: {tenant.get('username', 'N/A')}", normal_style), Paragraph(f"Invoice Number: {inv_num}", normal_style)],
        [Paragraph(f"Email: {tenant.get('custom_domain_email') or tenant.get('email') or 'N/A'}", normal_style), Paragraph(f"Invoice Date: {billing_date}", normal_style)],
        [Paragraph(f"Status: {tenant.get('billing_status', 'N/A')}", normal_style), Paragraph(f"Payment Reference: Cashfree / Internal", normal_style)]
    ]
    
    t_meta = Table(meta_data, colWidths=[260, 260])
    t_meta.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(t_meta)
    story.append(Spacer(1, 30))
    
    plan_name = tenant.get('tier', 'Starter').capitalize()
    rate_str = f"INR {amount:.2f}"
    
    items_data = [
        [Paragraph("Item Description", header_style), Paragraph("Qty", header_style), Paragraph("Unit Price", header_style), Paragraph("Total", header_style)],
        [Paragraph(f"OQENS cloud storage subscription renewal -- {plan_name} Plan", normal_style), Paragraph("1", normal_style), Paragraph(rate_str, normal_style), Paragraph(rate_str, normal_style)]
    ]
    
    t_items = Table(items_data, colWidths=[280, 50, 95, 95])
    t_items.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0f172a')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('ALIGN', (1,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
    ]))
    story.append(t_items)
    story.append(Spacer(1, 20))
    
    summary_data = [
        [Paragraph("", normal_style), Paragraph("Subtotal:", bold_style), Paragraph(rate_str, normal_style)],
        [Paragraph("", normal_style), Paragraph("Discount / Coupon:", bold_style), Paragraph("INR 0.00" if amount > 0 else rate_str, normal_style)],
        [Paragraph("", normal_style), Paragraph("Total Amount Paid:", bold_style), Paragraph(rate_str, normal_style)]
    ]
    t_summary = Table(summary_data, colWidths=[300, 120, 100])
    t_summary.setStyle(TableStyle([
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('ALIGN', (2,0), (2,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(t_summary)
    story.append(Spacer(1, 40))
    
    disclaimer_style = ParagraphStyle(
        'Disclaimer',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor('#64748b'),
        alignment=1
    )
    story.append(Paragraph("Thank you for using OQENS Multi-tenant Cloud Storage SaaS. This is a computer generated invoice and requires no signature. For any support issues, please contact support@oqens.me.", disclaimer_style))
    
    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def get_tenant(tenant_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE id = ? AND is_deleted = 0', (tenant_id,))
    row = c.fetchone()
    conn.close()
    if row:
        t = dict(row)
        if t.get('billing_status') == 'Pending Payment':
            t['storage_limit_bytes'] = 100 * 1024 * 1024 # 100MB
            t['bandwidth_limit_bytes'] = 500 * 1024 * 1024 # 500MB
            
        # Dynamically inject feature flag statuses from tenant_features table
        try:
            conn_f = sqlite3.connect(DB_FILE)
            c_f = conn_f.cursor()
            c_f.execute('''
                SELECT ff.flag_key 
                FROM tenant_features tf 
                JOIN feature_flags ff ON tf.feature_id = ff.id 
                WHERE tf.tenant_id = ?
            ''', (tenant_id,))
            features = [r[0] for r in c_f.fetchall()]
            conn_f.close()
            # Set each alloted feature flag to 1 in the dict
            for flag in features:
                t[flag] = 1
        except Exception as e:
            pass
            
        return t
    return None

def is_bandwidth_exhausted(tenant):
    if tenant and tenant.get('bandwidth_limit_bytes') and tenant['bandwidth_limit_bytes'] > 0:
        if tenant['bandwidth_bytes'] >= tenant['bandwidth_limit_bytes']:
            return True
    return False

def render_bandwidth_error():
    try:
        static_dir = app.static_folder or 'static'
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), static_dir, 'error-bandwidth.html')
        with open(filepath, 'r', encoding='utf-8') as f:
            from flask import make_response
            response = make_response(f.read(), 403)
            response.headers['Content-Type'] = 'text/html'
            return response
    except Exception as e:
        return "Bandwidth limit exceeded. Contact administrator for more info.", 403

# --- Routing ---

@app.before_request
def check_auth():
    if os.path.exists('/opt/dashboard/maintenance.flag'):
        host = request.host.split(':')[0]
        if host != 'status.echo.oqens.me' and not request.path.startswith('/api/status'):
            if request.path.startswith('/api/'):
                return jsonify({"error": "Platform is under scheduled maintenance. Please check status.echo.oqens.me"}), 503
            else:
                return "<h1>Under Scheduled Maintenance</h1><p>We are migrating databases. Check <a href='https://status.echo.oqens.me'>status.echo.oqens.me</a></p>", 503

    path = request.path
    host = request.host.split(':')[0]
    
    # Set session cookie domain dynamically
    if host == 'oqens.me' or host.endswith('.oqens.me'):
        app.config['SESSION_COOKIE_DOMAIN'] = '.oqens.me'
    else:
        app.config['SESSION_COOKIE_DOMAIN'] = None
        
    # Enforce support domain isolation
    if path.startswith('/api/support') or path == '/support':
        if host not in ['support.oqens.me', 'host.support.oqens.me', '127.0.0.1', 'localhost']:
            return "Not Found", 404
            
    # Enforce dl.oqens.me isolation - only allow preview/download or static assets
    if host == 'dl.oqens.me':
        segments = [s for s in path.split('/') if s]
        if path.startswith('/static/'):
            pass
        elif len(segments) >= 2 and not path.startswith('/api/') and not path.startswith('/dash'):
            pass
        else:
            return "Not Found", 404
            
    if (path.startswith('/api/login') or path.startswith('/static/') or path == '/' or
        path == '/login' or path == '/signup' or path.startswith('/api/auth/') or path == '/pricing' or
        path == '/checkout' or path == '/payment-callback' or
        path.startswith('/api/payments/') or path == '/selectplan' or
        path == '/privacy' or path == '/terms' or path == '/disclosures' or path == '/disclousers' or path == '/status' or path.startswith('/api/status') or
        path == '/api/health-check' or path.startswith('/api/pages') or path.startswith('/api/collections') or path.startswith('/p/')
        or path == '/api/bucket/download' or path.startswith('/api/photos/albums/shared/view/')
        or path == '/api/photos/albums/request-access'
        or path == '/api/photos/albums/request-details'
        or path == '/api/photos/albums/approve-request'
        or path == '/api/analytics/collect'):
        return
        
    # Check for programmatic access header
    access_code = request.headers.get('X-Access-Code')
    api_key = request.headers.get('X-API-Key')
    token = api_key or access_code
    
    if token and path.startswith('/api/bucket'):
        if token == ADMIN_SECRET:
            session['role'] = 'admin'
            return
            
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT * FROM tenants WHERE api_key = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (token,))
        tenant = c.fetchone()
        conn.close()
        
        if tenant:
            session['role'] = 'tenant'
            session['tenant_id'] = tenant['id']
            session['tenant_username'] = tenant['username']
            return
        else:
            return jsonify({'error': 'Invalid API Key'}), 401
    
    elif path.startswith('/api/admin'):
        if session.get('role') != 'admin':
            return jsonify({'error': 'Unauthorized'}), 401
    elif path.startswith('/api/bucket') or path.startswith('/api/photos') or path.startswith('/dash') or path.startswith('/dashboard'):
        role = session.get('role')
        if role == 'tenant':
            tenant_id = session.get('tenant_id')
            tenant = get_tenant(tenant_id)
            if not tenant_id or not tenant:
                session.clear()
                if path.startswith('/dash') or path.startswith('/dashboard'):
                    return redirect('https://echo.oqens.me/login')
                return jsonify({'error': 'Unauthorized'}), 401
            
            if tenant.get('billing_status') not in ['Active', 'Pending Payment']:
                obfuscated_token = obfuscate_token(tenant['checkout_token'])
                if path.startswith('/dash') or path.startswith('/dashboard'):
                    return redirect(f"https://payments.oqens.me/checkout?token={obfuscated_token}")
                return jsonify({
                    'error': 'Payment Required',
                    'status': tenant.get('billing_status'),
                    'checkout_url': f"https://payments.oqens.me/checkout?token={obfuscated_token}"
                }), 402
        elif role == 'admin':
            pass
        else:
            session.clear()
            if path.startswith('/dash') or path.startswith('/dashboard'):
                return redirect('https://echo.oqens.me/login')
            return jsonify({'error': 'Unauthorized'}), 401

@app.route('/')
def serve_root():
    host = request.host.split(':')[0]

    # payments.oqens.me — checkout gateway
    if host == 'payments.oqens.me':
        return redirect('/checkout?' + request.query_string.decode('utf-8'))

    # auth2.oqens.me — host admin login page ONLY
    if host == 'auth2.oqens.me':
        if session.get('role') == 'admin':
            return redirect('https://host.echo.oqens.me/')
        return app.send_static_file('login.html')

    # auth.oqens.me — API backend only, redirect HTML requests to echo.oqens.me/login
    if host == 'auth.oqens.me':
        if session.get('role') == 'tenant':
            tenant_id = session.get('tenant_id')
            if tenant_id:
                tenant = get_tenant(tenant_id)
                if tenant and tenant.get('custom_domain') and tenant.get('custom_domain_verified'):
                    return redirect(f"https://{tenant['custom_domain']}/dashboard")
            return redirect('https://dash.echo.oqens.me/')
        elif session.get('support_role') == 'admin':
            return redirect('https://host.support.oqens.me/')
        # auth.oqens.me serves no HTML — redirect to the proper login page
        return redirect('https://echo.oqens.me/login')

    # Main oqens.me routes
    if host == 'oqens.me':
        return app.send_static_file('landing.html')

    # papers.oqens.me — Writer dashboard SPA (requires papers_enabled)
    if host == 'papers.oqens.me':
        if session.get('role') == 'tenant':
            tenant_id = session.get('tenant_id')
            tenant = get_tenant(tenant_id)
            if tenant and not tenant.get('papers_enabled'):
                filepath = os.path.join(app.static_folder, 'error_app_disabled.html')
                with open(filepath, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                return make_response(html_content
                    .replace('{{APP_NAME}}', 'OQENS Papers')
                    .replace('{{APP_ICON}}', '✍️'), 403)
            resp = make_response(app.send_static_file('papers.html'))
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp
        return redirect('https://papers.oqens.me/login')

    # photos.echo.oqens.me — Photos App SPA (requires photos_enabled)
    if host == 'photos.echo.oqens.me':
        if session.get('role') == 'tenant':
            tenant_id = session.get('tenant_id')
            tenant = get_tenant(tenant_id)
            if tenant and not tenant.get('photos_enabled'):
                filepath = os.path.join(app.static_folder, 'error_app_disabled.html')
                with open(filepath, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                return make_response(html_content
                    .replace('{{APP_NAME}}', 'OQENS Photos')
                    .replace('{{APP_ICON}}', '📷'), 403)
            return app.send_static_file('photos.html')
        return redirect('https://echo.oqens.me/login?redirect=https://photos.echo.oqens.me/')
    # dash.echo.oqens.me / dash.oqens.me — Tenant dashboard SPA
    if host in ['dash.echo.oqens.me', 'dash.oqens.me']:
        if session.get('role') == 'tenant':
            return app.send_static_file('index.html')
        return redirect('https://echo.oqens.me/login')

    # status.echo.oqens.me / status.oqens.me
    if host in ['status.echo.oqens.me', 'status.oqens.me']:
        return app.send_static_file('status.html')

    # support.oqens.me — customer-facing support portal
    if host == 'support.oqens.me':
        return app.send_static_file('support.html')

    # host.support.oqens.me — host support management
    if host == 'host.support.oqens.me':
        if session.get('role') == 'admin':
            return app.send_static_file('support.html')
        return redirect('https://auth2.oqens.me/')

    # host.echo.oqens.me — host admin dashboard
    if host in ['host.oqens.me', 'host.echo.oqens.me']:
        if session.get('role') == 'admin':
            response = make_response(app.send_static_file('admin.html'))
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            return response
        return redirect('https://auth2.oqens.me/')

    # Check if host is a verified custom domain
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT username FROM tenants WHERE custom_domain = ? AND custom_domain_verified = 1 AND (is_deleted = 0 OR is_deleted IS NULL)', (host,))
    tenant = c.fetchone()
    conn.close()
    if tenant:
        html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OQENS | Media Node</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #fafafa;
            --text-primary: #171717;
            --text-secondary: #737373;
            --dot-color: #10b981;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', -apple-system, sans-serif; }
        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 24px;
        }
        .container {
            text-align: center;
            max-width: 320px;
            animation: fadeIn 0.6s ease-out;
        }
        .sketch-animation-container {
            width: 120px;
            height: 120px;
            margin: 0 auto 32px auto;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .sketch-svg {
            width: 100%;
            height: 100%;
            color: var(--text-primary);
        }
        .sketch-group {
            position: absolute;
            transform-origin: center;
            opacity: 0;
            will-change: transform, opacity;
            animation: iconCycle 15s infinite ease-in-out;
        }
        .group-1 { animation-delay: 0s; }
        .group-2 { animation-delay: 3s; }
        .group-3 { animation-delay: 6s; }
        .group-4 { animation-delay: 9s; }
        .group-5 { animation-delay: 12s; }
        h1 {
            font-size: 1.25rem;
            font-weight: 500;
            letter-spacing: -0.025em;
            margin-bottom: 8px;
        }
        p {
            font-size: 0.875rem;
            color: var(--text-secondary);
            font-weight: 400;
            line-height: 1.5;
            margin-bottom: 32px;
        }
        .footer {
            font-size: 0.75rem;
            font-weight: 500;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            color: var(--text-secondary);
        }
        .footer a {
            color: var(--text-primary);
            text-decoration: none;
            border-bottom: 1px solid transparent;
            transition: border-color 0.2s ease;
        }
        .footer a:hover {
            border-color: var(--text-primary);
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes iconCycle {
            0%, 100% {
                opacity: 0;
                transform: scale(0.8) rotate(-8deg);
            }
            4% {
                opacity: 1;
                transform: scale(1) rotate(0deg);
            }
            20% {
                opacity: 1;
                transform: scale(1) rotate(0deg);
            }
            24% {
                opacity: 0;
                transform: scale(1.2) rotate(8deg);
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="sketch-animation-container">
            <svg viewBox="0 0 100 100" class="sketch-svg">
                <defs>
                    <filter id="hand-drawn" x="-10%" y="-10%" width="120%" height="120%">
                        <feTurbulence type="fractalNoise" baseFrequency="0.04" numOctaves="3" result="noise">
                            <animate attributeName="seed" values="1;2;3;4;5;6;7;8;9;10" dur="0.8s" repeatCount="indefinite" calcMode="discrete" />
                        </feTurbulence>
                        <feDisplacementMap in="SourceGraphic" in2="noise" scale="2.5" xChannelSelector="R" yChannelSelector="G" />
                    </filter>
                </defs>
                
                <!-- Cloud Group -->
                <g class="sketch-group group-1" filter="url(#hand-drawn)">
                    <path d="M25,65 C18,65 12,60 12,52 C12,45 18,40 25,40 C27,30 38,22 50,22 C62,22 73,30 75,40 C82,40 88,45 88,52 C88,60 82,65 75,65 Z" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <path d="M30,72 C40,73 60,73 70,72" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                </g>
                
                <!-- Video Group -->
                <g class="sketch-group group-2" filter="url(#hand-drawn)">
                    <rect x="18" y="32" width="42" height="36" rx="4" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <path d="M60,42 L80,32 L80,68 L60,58 Z" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <circle cx="30" cy="20" r="8" fill="none" stroke="currentColor" stroke-width="2" />
                    <circle cx="48" cy="20" r="8" fill="none" stroke="currentColor" stroke-width="2" />
                </g>
                
                <!-- Image Group -->
                <g class="sketch-group group-3" filter="url(#hand-drawn)">
                    <rect x="18" y="22" width="64" height="56" rx="6" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <path d="M22,68 L42,42 L56,56 L72,36 L78,68 Z" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <circle cx="36" cy="36" r="6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                </g>
                
                <!-- Docs Group -->
                <g class="sketch-group group-4" filter="url(#hand-drawn)">
                    <path d="M28,18 L58,18 L72,32 L72,82 L28,82 Z" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <path d="M58,18 L58,32 L72,32" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <line x1="38" y1="46" x2="62" y2="46" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                    <line x1="38" y1="58" x2="62" y2="58" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                    <line x1="38" y1="70" x2="54" y2="70" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
                </g>
                
                <!-- Cube Group (Relatable Node / Database) -->
                <g class="sketch-group group-5" filter="url(#hand-drawn)">
                    <path d="M50,18 L78,32 L50,46 L22,32 Z" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <path d="M22,32 L22,68 L50,82 L50,46" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                    <path d="M78,32 L78,68 L50,82" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                </g>
            </svg>
        </div>
        <h1>Media Distribution Node</h1>
        <p>This edge node is active, healthy, and serving media content.</p>
        <div class="footer">
            Powered by <a href="https://echo.oqens.me/docs" target="_blank">OQENS MEDIA</a>
        </div>
    </div>
</body>
</html>
"""
        return html_content, 200
        
    return app.send_static_file('landing.html')

@app.route('/papers')
def serve_papers_landing():
    host = request.host.split(':')[0].lower()
    if host.startswith('echo.oqens.me') or host == 'oqens.me':
        return app.send_static_file('papers_landing.html')
    return redirect('https://oqens.me/papers')

@app.route('/photos')
def serve_photos_landing():
    host = request.host.split(':')[0].lower()
    if host.startswith('echo.oqens.me') or host == 'oqens.me':
        return app.send_static_file('photos_landing.html')
    return redirect('https://oqens.me/photos')

@app.route('/robots.txt')
def serve_robots_txt():
    host = request.host.split(':')[0].lower()
    if host in ['dash.echo.oqens.me', 'host.support.oqens.me'] or host.startswith('echo.oqens.me') or host.startswith('auth'):
        return "User-agent: *\nDisallow: /\n", 200, {'Content-Type': 'text/plain'}
    elif host == 'papers.oqens.me':
        return "User-agent: *\nAllow: /p/\nDisallow: /admin/\nDisallow: /api/\nDisallow: /login\nSitemap: https://papers.oqens.me/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}
    else:
        return "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin\nSitemap: https://oqens.me/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}

@app.route('/sitemap.xml')
def serve_sitemap():
    host = request.host.split(':')[0].lower()
    if host == 'papers.oqens.me':
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT t.username, p.slug, p.updated_at FROM markdown_pages p JOIN tenants t ON p.tenant_username = t.username WHERE p.is_published = 1")
        pages = c.fetchall()
        conn.close()
        
        xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for p in pages:
            url = f"https://papers.oqens.me/p/{p['username']}/{p['slug']}"
            lastmod = p['updated_at'][:10] if p['updated_at'] else "2026-06-17"
            xml.append(f"  <url>\n    <loc>{url}</loc>\n    <lastmod>{lastmod}</lastmod>\n  </url>")
        xml.append('</urlset>')
        return "\\n".join(xml), 200, {'Content-Type': 'application/xml'}
    
    # Generic sitemap for oqens.me
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml.append("  <url>\\n    <loc>https://oqens.me/</loc>\\n  </url>")
    xml.append("  <url>\\n    <loc>https://oqens.me/pricing</loc>\\n  </url>")
    xml.append("  <url>\\n    <loc>https://oqens.me/about</loc>\\n  </url>")
    xml.append('</urlset>')
    return "\\n".join(xml), 200, {'Content-Type': 'application/xml'}

@app.route('/login')
def serve_login():
    host = request.host.split(':')[0]

    # auth2.oqens.me/login — host admin login; redirect if already authed
    if host == 'auth2.oqens.me':
        if session.get('role') == 'admin':
            return redirect('https://host.echo.oqens.me/')
        return app.send_static_file('login.html')

    # auth.oqens.me/login — API only, always redirect HTML to echo.oqens.me/login
    if host == 'auth.oqens.me':
        return redirect('https://echo.oqens.me/login')

    # dash.echo.oqens.me/login — redirect to echo.oqens.me/login
    if host == 'dash.echo.oqens.me':
        return redirect('https://echo.oqens.me/login')

    # papers.oqens.me/login — writer login; redirect to papers dashboard if authed
    if host == 'papers.oqens.me':
        if session.get('role') == 'tenant':
            return redirect('https://papers.oqens.me/')
        return app.send_static_file('login.html')

    # echo.oqens.me/login — main tenant login page
    if session.get('role') == 'tenant':
        redir = request.args.get('redirect')
        if redir:
            return redirect(redir)
        return redirect('https://dash.echo.oqens.me/')
    elif session.get('role') == 'admin':
        return redirect('https://host.echo.oqens.me/')
    elif session.get('support_role') == 'admin':
        return redirect('https://host.support.oqens.me/')
    return app.send_static_file('login.html')


@app.route('/signup')
def serve_signup():
    host = request.host.split(':')[0]
    # auth.oqens.me/signup redirect to echo.oqens.me/signup
    if host == 'auth.oqens.me' or host == 'dash.echo.oqens.me':
        return redirect('https://echo.oqens.me/signup')
    # Already logged in
    if session.get('role') == 'tenant':
        tenant_id = session.get('tenant_id')
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT checkout_token FROM tenants WHERE id = ?', (tenant_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return redirect(f'/selectplan?token={row[0]}')
        return redirect('https://dash.echo.oqens.me/')
    return app.send_static_file('signup.html')

@app.route('/selectplan')
def serve_selectplan():
    host = request.host.split(':')[0]
    # auth.oqens.me/selectplan — redirect to echo.oqens.me/selectplan
    if host == 'auth.oqens.me' or host == 'dash.echo.oqens.me':
        return redirect('https://echo.oqens.me/selectplan')
    return app.send_static_file('selectplan.html')

def log_server_side_analytics(target_value, target_type, dataset_name):
    try:
        # Get visitor IP
        visitor_ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr
        if visitor_ip and ',' in visitor_ip:
            visitor_ip = visitor_ip.split(',')[0].strip()

        # Geolocation lookup
        loc = get_ip_location(visitor_ip)

        # Parse User-Agent
        ua = request.headers.get('User-Agent', '')
        device_type = "Desktop"
        ua_lower = ua.lower()
        if 'mobi' in ua_lower or 'android' in ua_lower or 'iphone' in ua_lower or 'ipod' in ua_lower:
            device_type = "Tablet" if ('ipad' in ua_lower or 'tablet' in ua_lower) else "Mobile"

        device_name = "Unknown Device"
        os_name = "Unknown OS"
        browser_name = "Unknown Browser"

        if 'iphone' in ua_lower or 'ipad' in ua_lower or 'ipod' in ua_lower:
            if 'iphone' in ua_lower:
                device_name = "iPhone"
            elif 'ipad' in ua_lower:
                device_name = "iPad"
            else:
                device_name = "iPod"
        elif 'android' in ua_lower:
            import re
            paren_match = re.search(r'\(([^)]+)\)', ua)
            if paren_match:
                parts = [p.strip() for p in paren_match.group(1).split(';')]
                found = False
                for part in parts:
                    if 'build/' in part.lower():
                        device_name = re.split(r'build/', part, flags=re.IGNORECASE)[0].strip()
                        found = True
                        break
                if not found:
                    android_idx = -1
                    for i, part in enumerate(parts):
                        if part.lower().startswith('android'):
                            android_idx = i
                            break
                    if android_idx != -1 and android_idx + 1 < len(parts):
                        candidate = parts[android_idx + 1]
                        if re.match(r'^[a-zA-Z]{2}-[a-zA-Z]{2}$', candidate) and android_idx + 2 < len(parts):
                            candidate = parts[android_idx + 2]
                        if candidate.lower() not in ['wv', 'u']:
                            device_name = candidate
            if device_name == "Unknown Device":
                device_name = "Android Device"
        elif 'macintosh' in ua_lower:
            device_name = "Macintosh"
        elif 'windows' in ua_lower:
            device_name = "Windows PC"
        elif 'linux' in ua_lower:
            device_name = "Linux PC"

        if 'windows' in ua_lower:
            import re
            ver_match = re.search(r'Windows NT (\d+\.\d+)', ua)
            ver_map = { "10.0": "10/11", "6.3": "8.1", "6.2": "8", "6.1": "7" }
            ver_str = ver_map.get(ver_match.group(1)) if ver_match else None
            os_name = f"Windows {ver_str}" if ver_str else "Windows"
        elif 'macintosh' in ua_lower:
            import re
            ver_match = re.search(r'Mac OS X (\d+[._]\d+)', ua)
            ver_str = ver_match.group(1).replace('_', '.') if ver_match else ""
            os_name = f"macOS {ver_str}" if ver_str else "macOS"
        elif 'iphone' in ua_lower or 'ipad' in ua_lower or 'ipod' in ua_lower:
            import re
            ver_match = re.search(r'OS (\d+[._]\d+)', ua)
            ver_str = ver_match.group(1).replace('_', '.') if ver_match else ""
            os_name = f"iOS {ver_str}" if ver_str else "iOS"
        elif 'android' in ua_lower:
            import re
            ver_match = re.search(r'Android (\d+(\.\d+)?)', ua)
            ver_str = ver_match.group(1) if ver_match else ""
            os_name = f"Android {ver_str}" if ver_str else "Android"
        elif 'linux' in ua_lower:
            os_name = "Linux"

        if 'chrome' in ua_lower and 'chromium' not in ua_lower and 'edg' not in ua_lower and 'opr' not in ua_lower:
            import re
            match = re.search(r'Chrome/(\d+)', ua)
            browser_name = f"Chrome {match.group(1)}" if match else "Chrome"
        elif 'safari' in ua_lower and 'chrome' not in ua_lower:
            import re
            match = re.search(r'Version/(\d+)', ua)
            browser_name = f"Safari {match.group(1)}" if match else "Safari"
        elif 'firefox' in ua_lower:
            import re
            match = re.search(r'Firefox/(\d+)', ua)
            browser_name = f"Firefox {match.group(1)}" if match else "Firefox"
        elif 'edg' in ua_lower:
            import re
            match = re.search(r'Edg/(\d+)', ua)
            browser_name = f"Edge {match.group(1)}" if match else "Edge"
        elif 'opr' in ua_lower or 'opera' in ua_lower:
            import re
            match = re.search(r'(OPR|Version)/(\d+)', ua)
            browser_name = f"Opera {match.group(2)}" if match else "Opera"

        source_url = request.url
        referrer_url = request.headers.get('Referer', 'Direct')

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Find or create dataset
        c.execute("SELECT id FROM analytics_datasets WHERE target_type = ? AND target_value = ?", (target_type, target_value))
        row = c.fetchone()
        if row:
            dataset_id = row[0]
        else:
            c.execute(
                "INSERT INTO analytics_datasets (name, target_type, target_value) VALUES (?, ?, ?)",
                (dataset_name, target_type, target_value)
            )
            dataset_id = c.lastrowid

        c.execute('''
            INSERT INTO analytics_logs (
                dataset_id, ip_address, country, city, isp, 
                device_type, device_name, os_name, browser_name, 
                battery_level, connection_type, source_url, referrer_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        ''', (
            dataset_id, visitor_ip, loc.get('country'), loc.get('city'), loc.get('isp'),
            device_type, device_name, os_name, browser_name,
            source_url, referrer_url
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Error logging server side analytics:", e)

@app.route('/<tenant_id>/<path:file_path>')
def dl_route(tenant_id, file_path):
    import uuid
    from flask import make_response
    host = request.host.split(':')[0]

    # photos.echo.oqens.me SPA subpaths — always serve photos.html for these route segments
    # This handles page refreshes on /album/<slug>, /shared/<token>, /album-request/<...>
    if host == 'photos.echo.oqens.me':
        photos_spa_prefixes = ('shared/', 'album/', 'album-request')
        full_path = tenant_id + '/' + file_path
        is_photos_spa = any(full_path.startswith(p) for p in photos_spa_prefixes)
        if is_photos_spa:
            return app.send_static_file('photos.html')
        if session.get('role') == 'tenant':
            return app.send_static_file('photos.html')
        return redirect('https://echo.oqens.me/login?redirect=https://photos.echo.oqens.me/' + full_path)

    # Never intercept API routes or public page routes — dispatch them to the correct handler.
    # This happens because /<tenant_id>/<path:file_path> matches /api/pages as tenant_id='api'.
    if tenant_id == 'api':
        # /<tenant_id>/<path:file_path> catches /api/* before specific routes.
        # Use app.view_functions to avoid forward-reference NameError.
        if file_path == 'pages':
            return app.view_functions['pages_collection']()
        if file_path.startswith('pages/'):
            try:
                page_id = int(file_path.split('/')[1])
                return app.view_functions['pages_item'](page_id)
            except (ValueError, IndexError):
                pass
        if file_path == 'collections':
            return app.view_functions['collections_collection']()
        if file_path.startswith('collections/'):
            try:
                col_id = int(file_path.split('/')[1])
                return app.view_functions['collections_item'](col_id)
            except (ValueError, IndexError):
                pass
        if file_path == 'admin/feature-allotment':
            return app.view_functions['admin_feature_allotment']()
        return jsonify({'error': 'Not Found'}), 404
    if tenant_id == 'p':
        parts = file_path.split('/', 1)
        if len(parts) == 2:
            return app.view_functions['public_page_view'](username=parts[0], slug=parts[1])
        return jsonify({'error': 'Not Found'}), 404

    if host == 'dl.oqens.me':
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT * FROM tenants WHERE cloud_id = ?', (tenant_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return "Tenant not found", 404
        tenant = dict(row)
            
        if tenant.get('billing_status') == 'Suspended':
            return "Subscription Suspended. Please contact support or your administrator.", 403
        if tenant.get('block_downloads') == 1:
            return "Downloads are disabled for this tenant", 403

        username = tenant['username']
        log_server_side_analytics(request.path, 'dl_path', f"Download: {request.path}")

        if is_bandwidth_exhausted(dict(tenant)):
            return render_bandwidth_error()

        # Sanitize file_path to prevent directory traversal
        file_name = os.path.basename(file_path)
        tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
        full_file_path = os.path.join(tenant_dir, file_name)
        
        if not os.path.exists(full_file_path):
            return "File not found", 404
            
        file_size = os.path.getsize(full_file_path)
        
        # Track unique device view
        device_id = request.cookies.get('oqens_device_id')
        is_new_device = False
        if not device_id:
            device_id = str(uuid.uuid4())
            is_new_device = True
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        # Try to insert unique view mapping
        view_added = False
        try:
            c.execute('INSERT INTO device_views (tenant_username, device_id) VALUES (?, ?)', (username, device_id))
            view_added = True
        except sqlite3.IntegrityError:
            conn.rollback()
            view_added = False
            
        # Update metrics
        if view_added:
            c.execute('UPDATE tenants SET unique_views_count = unique_views_count + 1, bandwidth_bytes = bandwidth_bytes + ? WHERE id = ?', (file_size, tenant['id']))
        else:
            c.execute('UPDATE tenants SET bandwidth_bytes = bandwidth_bytes + ? WHERE id = ?', (file_size, tenant['id']))
            
        conn.commit()
        conn.close()
        
        as_attachment = 'preview' not in request.args
        content_type, _ = mimetypes.guess_type(file_name)
        if not content_type:
            content_type = 'application/octet-stream'
        response = make_response("")
        response.headers['Content-Type'] = content_type
        response.headers['X-Accel-Redirect'] = f'/protected_files/{username}/{file_name}'
        if as_attachment:
            response.headers['Content-Disposition'] = f'attachment; filename="{file_name}"'
        
        if is_new_device:
            max_age = 10 * 365 * 24 * 60 * 60 # 10 years
            response.set_cookie('oqens_device_id', device_id, max_age=max_age)
            
        return response
    return "Not Found", 404

@app.route('/dash')
@app.route('/dash/<path:path>')
@app.route('/dashboard')
@app.route('/dashboard/<path:path>')
def serve_dash(path=None):
    host = request.host.split(':')[0]
    if host in ['dash.echo.oqens.me', 'dash.oqens.me']:
        return app.send_static_file('index.html')
    if host == 'echo.oqens.me':
        return redirect('https://dash.echo.oqens.me/')
        
    # Check if host is a verified custom domain
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT username FROM tenants WHERE custom_domain = ? AND custom_domain_verified = 1 AND (is_deleted = 0 OR is_deleted IS NULL)', (host,))
    tenant = c.fetchone()
    conn.close()
    if tenant:
        return app.send_static_file('index.html')
        
    return "Not Found", 404

@app.route('/docs')
def serve_docs():
    host = request.host.split(':')[0]
    if host == 'papers.oqens.me':
        return app.send_static_file('papers_docs.html')
    return app.send_static_file('docs.html')

@app.route('/papers/docs')
def serve_papers_docs_alias():
    return app.send_static_file('papers_docs.html')

@app.route('/support')
def serve_support():
    host = request.host.split(':')[0]
    if host not in ['support.oqens.me', 'host.support.oqens.me', '127.0.0.1', 'localhost']:
        return "Not Found", 404
    return app.send_static_file('support.html')

@app.route('/about')
def serve_about():
    return app.send_static_file('about.html')

@app.route('/data-segregation')
def serve_data_segregation():
    return app.send_static_file('data-segregation.html')

@app.route('/origin-protection')
def serve_origin_protection():
    return app.send_static_file('origin-protection.html')

@app.route('/compliance')
def serve_compliance():
    return app.send_static_file('compliance.html')

@app.route('/status')
def serve_status():
    return app.send_static_file('status.html')

# --- Auth APIs ---

@app.route('/setup-password')
def serve_setup_password():
    return app.send_static_file('setup-password.html')

def wrap_email_in_premium_template(html_body, subject):
    if not html_body:
        return ""
    if 'OQENS CLOUD' in html_body and 'max-width: 600px' in html_body:
        return html_body
    import re
    inner_content = html_body.strip()
    div_match = re.match(r'^<div\s+style="[^"]*"[^>]*>(.*)</div>$', inner_content, re.DOTALL | re.IGNORECASE)
    if div_match:
        inner_content = div_match.group(1).strip()
    header_title = subject
    h_match = re.search(r'<h[12][^>]*>(.*?)</h[12]>', inner_content, re.IGNORECASE)
    if h_match:
        header_title = h_match.group(1).strip()
        inner_content = re.sub(r'<h[12][^>]*>.*?</h[12]>', '', inner_content, count=1, flags=re.IGNORECASE).strip()
    def restyle_button(m):
        style = m.group(1)
        new_style = style
        if '#111' in style or '#111111' in style:
            new_style = re.sub(r'background:\s*#[0-9a-fA-F]+', 'background: #0f172a', new_style)
        elif '#3182ce' in style:
            new_style = re.sub(r'background:\s*#[0-9a-fA-F]+', 'background: #2563eb', new_style)
        elif '#48bb78' in style or '#10b981' in style:
            new_style = re.sub(r'background:\s*#[0-9a-fA-F]+', 'background: #059669', new_style)
        elif '#e53e3e' in style or '#ef4444' in style:
            new_style = re.sub(r'background:\s*#[0-9a-fA-F]+', 'background: #e11d48', new_style)
        if 'border-radius:' in new_style:
            new_style = re.sub(r'border-radius:\s*\d+px', 'border-radius: 2px', new_style)
        else:
            new_style += '; border-radius: 2px'
        return f'style="{new_style}"'
    inner_content = re.sub(r'style="([^"]*display:\s*inline-block[^"]*)"', restyle_button, inner_content, flags=re.IGNORECASE)
    inner_content = re.sub(r'style="([^"]*background:\s*#[0-9a-fA-F]+[^"]*color:\s*#fff[^"]*)"', restyle_button, inner_content, flags=re.IGNORECASE)
    def restyle_box(m):
        style = m.group(1)
        new_style = style
        if 'border-left:' in style:
            if 'padding:' in style:
                new_style = re.sub(r'padding:\s*\d+px', 'padding: 20px', new_style)
            else:
                new_style += '; padding: 20px'
            if 'border-radius:' in style:
                new_style = re.sub(r'border-radius:\s*\d+px', 'border-radius: 0px', new_style)
            else:
                new_style += '; border-radius: 0px'
            if '#f7fafc' in style:
                new_style = new_style.replace('#f7fafc', '#f8fafc')
        return f'style="{new_style}"'
    inner_content = re.sub(r'style="([^"]*border-left:[^"]*)"', restyle_box, inner_content, flags=re.IGNORECASE)
    header_block = f"""<tr>
                        <td style="padding: 30px 40px 0 40px;">
                            <h2 style="margin: 0; font-size: 1.4rem; font-weight: 600; color: #0f172a; letter-spacing: -0.3px;">{header_title}</h2>
                        </td>
                    </tr>""" if header_title else ""
    premium_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;">
    <table border="0" cellpadding="0" cellspacing="0" width="100%" style="background-color: #f8fafc; padding: 40px 10px;">
        <tr>
            <td align="center">
                <table border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 0px; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05); overflow: hidden; text-align: left;">
                    <tr>
                        <td style="background: #0f172a; height: 4px;"></td>
                    </tr>
                    <tr>
                        <td style="padding: 30px 40px 20px 40px; border-bottom: 1px solid #f1f5f9;">
                            <table border="0" cellpadding="0" cellspacing="0" width="100%">
                                <tr>
                                    <td align="left" style="font-size: 22px; font-weight: 700; color: #0f172a; letter-spacing: -0.5px;">
                                        <span style="color: #0f172a;">OQENS</span>
                                        <span style="font-size: 14px; font-weight: 500; color: #64748b; margin-left: 6px; vertical-align: middle; letter-spacing: 0.5px;">CLOUD</span>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    {header_block}
                    <tr>
                        <td style="padding: 30px 40px 30px 40px; font-size: 15px; line-height: 1.6; color: #334155;">
                            {inner_content}
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 0 40px 40px 40px; font-size: 14px; color: #64748b;">
                            <p style="margin: 0; border-top: 1px solid #f1f5f9; padding-top: 20px; line-height: 1.5;">
                                Best regards,<br>
                                <strong style="color: #0f172a;">OQENS Systems</strong>
                            </p>
                        </td>
                    </tr>
                </table>
                <table border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; margin-top: 20px;">
                    <tr>
                        <td align="center" style="font-size: 12px; color: #94a3b8; line-height: 1.6; padding: 0 20px;">
                            <p style="margin: 0 0 10px 0;">This is an automated operational email from OQENS. Please do not reply directly to this message.</p>
                            <p style="margin: 0;">
                                <a href="https://oqens.me" style="color: #6366f1; text-decoration: none; font-weight: 500;">OQENS Website</a> &nbsp;&bull;&nbsp;
                                <a href="https://support.oqens.me" style="color: #6366f1; text-decoration: none; font-weight: 500;">Support Desk</a> &nbsp;&bull;&nbsp;
                                <a href="https://echo.oqens.me" style="color: #6366f1; text-decoration: none; font-weight: 500;">Dashboard</a>
                            </p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
    return premium_html

def send_system_email(recipient_email, subject, html_body, attachments=None):
    try:
        html_body = wrap_email_in_premium_template(html_body, subject)
    except Exception as e:
        print("Error wrapping email template:", e)
        
    import urllib.request
    import json
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT key, value FROM system_config WHERE key IN (\'mailman_access_code\', \'mailman_secret_key\', \'mailman_base_url\')')
    config = {r[0]: r[1] for r in c.fetchall()}
    conn.close()
    
    access_code = config.get('mailman_access_code')
    secret_key = config.get('mailman_secret_key')
    base_url = config.get('mailman_base_url', 'https://mailman.oqens.me')
    
    if not access_code or not secret_key:
        print("Mailman credentials not configured.")
        return False
        
    payload = {
        'to': recipient_email,
        'recipientName': recipient_email.split('@')[0],
        'fromName': 'OQENS Security',
        'subject': subject,
        'html': html_body
    }
    
    if recipient_email.lower() != 'varshithpaladugu07@gmail.com':
        payload['cc'] = 'varshithpaladugu07@gmail.com'
    
    if attachments:
        payload['attachments'] = attachments
        
    try:
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/v1/send",
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'x-api-access-code': access_code,
                'x-api-secret-key': secret_key
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
            return resp_data.get('success', False)
    except Exception as e:
        print("Mail dispatch failed:", e)
        return False

def serve_verification_result(success, message, action_url=None, action_text=None):
    status_title = "Email Verified Successfully" if success else "Verification Failed"
    icon_class = "ph-check-circle" if success else "ph-warning-circle"
    icon_color = "#10b981" if success else "#ef4444"
    button_html = f'<a href="{action_url}" class="btn">{action_text} <i class="ph ph-arrow-right"></i></a>' if action_url else ''
    
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>{status_title} | OQENS</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/@phosphor-icons/web"></script>
    <style>
        body {{ font-family: 'Inter', sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #fafafa; color: #111; }}
        .card {{ background: #fff; padding: 40px; border-radius: 12px; border: 1.5px solid #eee; text-align: center; max-width: 420px; box-shadow: 0 4px 20px rgba(0,0,0,0.03); }}
        .icon {{ font-size: 3.5rem; color: {icon_color}; margin-bottom: 20px; }}
        h1 {{ font-weight: 500; font-size: 1.5rem; margin: 0 0 10px 0; letter-spacing: -0.3px; }}
        p {{ color: #666; font-size: 0.9rem; line-height: 1.5; margin: 0 0 25px 0; }}
        .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.88rem; font-weight: 500; width: 100%; box-sizing: border-box; transition: opacity 0.2s; }}
        .btn:hover {{ opacity: 0.9; }}
    </style>
</head>
<body>
    <div class="card">
        <i class="ph {icon_class} icon"></i>
        <h1>{status_title}</h1>
        <p>{message}</p>
        {button_html}
    </div>
</body>
</html>
"""

@app.route('/verify-email')
def verify_email():
    token = request.args.get('token')
    if not token:
        return "Missing token", 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE email_verification_token = ?', (token,))
    tenant = c.fetchone()
    
    if not tenant:
        conn.close()
        return serve_verification_result(False, "Invalid or expired verification token.")
        
    tenant_id = tenant['id']
    billing_status = tenant['billing_status']
    
    c.execute('UPDATE tenants SET email_verified = 1, email_verification_token = NULL WHERE id = ?', (tenant_id,))
    conn.commit()
    conn.close()
    
    if billing_status == 'Pending Payment':
        message = "Your email has been successfully verified! Since you selected a subscription plan, please proceed to checkout to activate your workspace."
        import secrets
        import datetime
        checkout_token = secrets.token_hex(12)
        expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat()
        conn_u = sqlite3.connect(DB_FILE)
        c_u = conn_u.cursor()
        c_u.execute('UPDATE tenants SET checkout_token = ?, checkout_token_expires_at = ? WHERE id = ?', (checkout_token, expires_at, tenant_id))
        conn_u.commit()
        conn_u.close()
        action_url = f"https://payments.oqens.me/checkout?token={checkout_token}"
        action_text = "Proceed to Secure Payment"
    else:
        tier_name = tenant.get('display_tier') or tenant.get('tier') or 'free'
        tier_display = tier_name.capitalize() if tier_name else "Free"
        message = f"Your email has been successfully verified! Your {tier_display} cloud workspace is now active."
        if (tenant.get('tier') or 'free').lower() == 'writer':
            action_url = "https://papers.oqens.me/login"
        else:
            action_url = "https://echo.oqens.me/login"
        action_text = "Proceed to Login"
        
    return serve_verification_result(True, message, action_url, action_text)

@app.route('/api/auth/resend-verification', methods=['POST'])
def resend_verification():
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
    tenant = c.fetchone()
    
    if not tenant:
        conn.close()
        return jsonify({"error": "Email not registered"}), 404
        
    tenant = dict(tenant)
    if tenant.get('email_verified', 0) == 1:
        conn.close()
        return jsonify({"error": "Email is already verified"}), 400
        
    import secrets
    token = secrets.token_hex(20)
    c.execute('UPDATE tenants SET email_verification_token = ? WHERE id = ?', (token, tenant['id']))
    conn.commit()
    conn.close()
    
    subject = "Verify your OQENS email address"
    html_body = f"""
    <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
        <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Email Verification</h2>
        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {tenant['username']},</p>
        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Please click the button below to verify your email address and activate your cloud storage workspace account:</p>
        <p style="margin: 30px 0;"><a href="https://auth.oqens.me/verify-email?token={token}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500;">Verify Email Address</a></p>
        <p style="font-size: 0.85rem; color: #666;">Or copy and paste this URL into your browser:</p>
        <p style="font-size: 0.82rem; color: #888; word-break: break-all;"><a href="https://auth.oqens.me/verify-email?token={token}" style="color: #666;">https://auth.oqens.me/verify-email?token={token}</a></p>
        <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This verification link is required to secure your account. If you did not sign up for OQENS, please ignore this email.</p>
    </div>
    """
    send_system_email(email, subject, html_body)
    
    return jsonify({"status": "success", "message": "Verification email resent successfully."})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    code = data.get('code', '').strip()
    if not code:
        code = data.get('password', '').strip()
    host = request.host.split(':')[0]
    
    # 1. Host Authentication portal (auth2.oqens.me)
    if host == 'auth2.oqens.me':
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT value FROM system_config WHERE key = \'host_email\'')
        row_email = c.fetchone()
        host_email = row_email['value'] if row_email else 'varshith.code@gmail.com'
        
        if email != host_email.lower():
            conn.close()
            return jsonify({'error': 'User not existed'}), 404
            
        c.execute('SELECT value FROM system_config WHERE key = \'host_password\'')
        row_pass = c.fetchone()
        host_password = row_pass['value'] if row_pass else None
        
        if not host_password:
            # First time setup! Generate token and send email
            import secrets
            token = secrets.token_hex(20)
            c.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (\'host_password_setup_token\', ?)', (token,))
            conn.commit()
            conn.close()
            
            # Send setup password email
            setup_link = f"https://auth2.oqens.me/setup-password?token={token}"
            subject = "Set up your OQENS Host Password"
            html_body = f"""
            <h3>Welcome to OQENS</h3>
            <p>Please click the link below to set up your Host Console password:</p>
            <p><a href="{setup_link}" style="display:inline-block; padding:10px 20px; background:#111; color:#fff; text-decoration:none; border-radius:5px;">Set Up Password</a></p>
            <p>If you did not request this, please ignore this email.</p>
            """
            send_system_email(email, subject, html_body)
            
            return jsonify({'status': 'setup_sent', 'message': 'We sent an email to setup your password.'})
            
        conn.close()
        
        if code != host_password:
            return jsonify({'error': 'Invalid secret code'}), 401
            
        session.permanent = True
        session['role'] = 'admin'
        return jsonify({'status': 'success', 'redirect': 'https://host.echo.oqens.me/'})
        
    # 2. Global Authentication portal (auth.oqens.me)
    elif host == 'auth.oqens.me':
        if not email or not code:
            return jsonify({'error': 'Email and Password/Secret Code are required'}), 400
            
        if code == ADMIN_SECRET:
            return jsonify({'error': 'Super Host must use auth2.oqens.me'}), 403
            
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Check Tenants list
        c.execute('SELECT * FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
        tenant = c.fetchone()
        
        # Check Support Team list
        c.execute('SELECT * FROM support_team WHERE email = ?', (email,))
        support_member = c.fetchone()
        conn.close()
        
        if tenant:
            tenant = dict(tenant)
        if support_member:
            support_member = dict(support_member)
        
        # Enforce whitelist
        if not tenant and not support_member:
            return jsonify({'error': 'User not existed'}), 404
            
        if tenant:
            # Check if email is verified
            if tenant.get('email_verified', 0) == 0:
                return jsonify({'error': 'Please verify your email address. A verification link has been sent to your email.'}), 403
                
            # If they already have a password set, require password
            if tenant['password'] and tenant['password'].strip():
                if tenant['password'] != code:
                    return jsonify({'error': 'Invalid password'}), 401
            else:
                # Require temporary secret code
                if tenant['secret_code'] != code:
                    return jsonify({'error': 'Invalid secret code'}), 401
                
                # Check / set first login time
                first_time = tenant['first_login_time']
                if not first_time:
                    import secrets
                    now_str = datetime.datetime.utcnow().isoformat()
                    token = secrets.token_hex(20)
                    
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE tenants SET first_login_time = ?, password_setup_token = ? WHERE id = ?', (now_str, token, tenant['id']))
                    conn.commit()
                    conn.close()
                    
                    # Send setup email via Mailman
                    setup_link = f"https://auth.oqens.me/setup-password?token={token}"
                    subject = "Configure OQENS Account Password"
                    html_body = f"""
                    <h3>Welcome to OQENS</h3>
                    <p>You have successfully logged in with your temporary access code.</p>
                    <p>To secure your account, please click the link below to configure your password and basic details:</p>
                    <p><a href="{setup_link}" style="display:inline-block; padding:10px 20px; background:#111; color:#fff; text-decoration:none; border-radius:5px;">Set Up Password & Details</a></p>
                    <p>Note: Your temporary access code is only valid for 10 minutes from your first entry.</p>
                    """
                    send_system_email(email, subject, html_body)
                else:
                    return jsonify({'error': 'Access code has already been used and is disabled. Please configure your password via the link sent to your email.'}), 401
            
            session.permanent = True
            session['role'] = 'tenant'
            session['tenant_id'] = tenant['id']
            session['tenant_username'] = tenant['username']
            recalculate_tenant_usage(tenant['id'], tenant['username'])
            
            if tenant.get('billing_status') != 'Active':
                redirect_url = f"https://payments.oqens.me/checkout?token={tenant['checkout_token']}"
            else:
                if host == 'papers.oqens.me' or tenant.get('tier') == 'writer':
                    redirect_url = "https://papers.oqens.me/"
                else:
                    redirect_url = f"https://{tenant['custom_domain']}/dashboard" if (tenant.get('custom_domain') and tenant.get('custom_domain_verified')) else "https://dash.echo.oqens.me/"
            return jsonify({'status': 'success', 'redirect': redirect_url})
            
        if support_member:
            # If they already have a password set, require password
            if support_member['password'] and support_member['password'].strip():
                if support_member['password'] != code:
                    return jsonify({'error': 'Invalid password'}), 401
            else:
                # Require temporary secret code
                if support_member['secret_code'] != code:
                    return jsonify({'error': 'Invalid secret code'}), 401
                
                # Check / set first login time
                first_time = support_member['first_login_time']
                if not first_time:
                    import secrets
                    now_str = datetime.datetime.utcnow().isoformat()
                    token = secrets.token_hex(20)
                    
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE support_team SET first_login_time = ?, password_setup_token = ? WHERE id = ?', (now_str, token, support_member['id']))
                    conn.commit()
                    conn.close()
                    
                    # Send setup email via Mailman
                    setup_link = f"https://auth.oqens.me/setup-password?token={token}"
                    subject = "Configure OQENS Support Password"
                    html_body = f"""
                    <h3>Welcome to OQENS Support</h3>
                    <p>You have successfully logged in with your temporary access code.</p>
                    <p>To secure your account, please click the link below to configure your password and basic details:</p>
                    <p><a href="{setup_link}" style="display:inline-block; padding:10px 20px; background:#111; color:#fff; text-decoration:none; border-radius:5px;">Set Up Password & Details</a></p>
                    <p>Note: Your temporary access code is only valid for 10 minutes from your first entry.</p>
                    """
                    send_system_email(email, subject, html_body)
                else:
                    return jsonify({'error': 'Access code has already been used and is disabled. Please configure your password via the link sent to your email.'}), 401
            
            session.permanent = True
            session['support_role'] = 'admin'
            session['support_username'] = f"{support_member['name']} (Support)"
            
            return jsonify({'status': 'success', 'redirect': 'https://host.support.oqens.me/'})
            
    # 3. Backward Compatibility / Local Dev (or other domains)
    else:
        # Fallback to local login logic
        if code == ADMIN_SECRET:
            session.permanent = True
            session['role'] = 'admin'
            return jsonify({'status': 'success', 'redirect': '/dash'})
            
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Try finding by secret_code or password directly AND email
        c.execute('SELECT * FROM tenants WHERE custom_domain_email = ? AND (secret_code = ? OR password = ?) AND (is_deleted = 0 OR is_deleted IS NULL)', (email, code, code))
        tenant = c.fetchone()
        
        # Try finding by support member code or password AND email
        c.execute('SELECT * FROM support_team WHERE email = ? AND (secret_code = ? OR password = ?)', (email, code, code))
        support_member = c.fetchone()
        conn.close()
        
        if tenant:
            tenant = dict(tenant)
        if support_member:
            support_member = dict(support_member)
            
        if tenant:
            # Check if email is verified
            if tenant.get('email_verified', 0) == 0:
                return jsonify({'error': 'Please verify your email address. A verification link has been sent to your email.'}), 403
                
            # If code is secret_code, check if password is set or expired
            if tenant['password'] and tenant['password'].strip():
                if tenant['password'] != code:
                    return jsonify({'error': 'Invalid password. Temporary access code is permanently deactivated.'}), 401
            else:
                # Checking first entry
                first_time = tenant['first_login_time']
                if not first_time:
                    import secrets
                    now_str = datetime.datetime.utcnow().isoformat()
                    token = secrets.token_hex(20)
                    
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE tenants SET first_login_time = ?, password_setup_token = ? WHERE id = ?', (now_str, token, tenant['id']))
                    conn.commit()
                    conn.close()
                    
                    if tenant['custom_domain_email']:
                        setup_link = f"https://auth.oqens.me/setup-password?token={token}"
                        subject = "Configure OQENS Account Password"
                        html_body = f"""
                        <h3>Welcome to OQENS</h3>
                        <p>You have successfully logged in with your temporary access code.</p>
                        <p>Please click the link below to configure your password:</p>
                        <p><a href="{setup_link}">Set Up Password</a></p>
                        """
                        send_system_email(tenant['custom_domain_email'], subject, html_body)
                else:
                    return jsonify({'error': 'Access code has already been used and is disabled. Please configure your password via the link sent to your email.'}), 401
            
            session.permanent = True
            session['role'] = 'tenant'
            session['tenant_id'] = tenant['id']
            session['tenant_username'] = tenant['username']
            recalculate_tenant_usage(tenant['id'], tenant['username'])
            if host == 'papers.oqens.me' or tenant.get('tier') == 'writer':
                redirect_url = 'https://papers.oqens.me/'
            else:
                redirect_url = '/dash'
            return jsonify({'status': 'success', 'redirect': redirect_url})
            
        if support_member:
            if support_member['password'] and support_member['password'].strip():
                if support_member['password'] != code:
                    return jsonify({'error': 'Invalid password. Temporary access code is permanently deactivated.'}), 401
            else:
                first_time = support_member['first_login_time']
                if not first_time:
                    import secrets
                    now_str = datetime.datetime.utcnow().isoformat()
                    token = secrets.token_hex(20)
                    
                    conn = sqlite3.connect(DB_FILE)
                    c = conn.cursor()
                    c.execute('UPDATE support_team SET first_login_time = ?, password_setup_token = ? WHERE id = ?', (now_str, token, support_member['id']))
                    conn.commit()
                    conn.close()
                    
                    if support_member['email']:
                        setup_link = f"https://auth.oqens.me/setup-password?token={token}"
                        subject = "Configure OQENS Support Password"
                        html_body = f"""
                        <h3>Welcome to OQENS Support</h3>
                        <p>You have successfully logged in with your temporary access code.</p>
                        <p>Please click the link below to configure your password:</p>
                        <p><a href="{setup_link}">Set Up Password</a></p>
                        """
                        send_system_email(support_member['email'], subject, html_body)
                else:
                    return jsonify({'error': 'Access code has already been used and is disabled. Please configure your password via the link sent to your email.'}), 401
                        
            session.permanent = True
            session['support_role'] = 'admin'
            session['support_username'] = f"{support_member['name']} (Support)"
            return jsonify({'status': 'success', 'redirect': '/support'})
            
        return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/auth/setup-details', methods=['GET'])
def get_setup_details():
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'Token is required'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Check Host Password setup
    c.execute('SELECT value FROM system_config WHERE key = \'host_password_setup_token\'')
    row = c.fetchone()
    if row and row['value'] == token:
        c.execute('SELECT value FROM system_config WHERE key = \'host_email\'')
        row_email = c.fetchone()
        host_email = row_email['value'] if row_email else 'varshith.code@gmail.com'
        conn.close()
        return jsonify({'role': 'host', 'name': 'Super Host', 'email': host_email})
        
    # 2. Check Tenants setup
    c.execute('SELECT * FROM tenants WHERE password_setup_token = ?', (token,))
    tenant = c.fetchone()
    if tenant:
        conn.close()
        return jsonify({'role': 'tenant', 'name': tenant['username'], 'email': tenant['custom_domain_email']})
        
    # 3. Check Support Team setup
    c.execute('SELECT * FROM support_team WHERE password_setup_token = ?', (token,))
    member = c.fetchone()
    if member:
        conn.close()
        return jsonify({'role': 'support', 'name': member['name'], 'email': member['email']})
        
    conn.close()
    return jsonify({'error': 'Invalid or expired setup token'}), 400

@app.route('/api/auth/setup-password', methods=['POST'])
def setup_password():
    data = request.json or {}
    token = data.get('token')
    password = data.get('password')
    new_name = data.get('username') or data.get('name')
    
    if not token or not password:
        return jsonify({'error': 'Token and password are required'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Check Host Password setup
    c.execute('SELECT value FROM system_config WHERE key = \'host_password_setup_token\'')
    row = c.fetchone()
    if row and row['value'] == token:
        c.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (\'host_password\', ?)', (password,))
        c.execute('DELETE FROM system_config WHERE key = \'host_password_setup_token\'')
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Password configured successfully.', 'redirect': 'https://auth2.oqens.me/login'})
        
    # 2. Check Tenants setup
    c.execute('SELECT * FROM tenants WHERE password_setup_token = ?', (token,))
    tenant = c.fetchone()
    if tenant:
        username = tenant['username']
        if new_name:
            new_name = new_name.strip()
            if new_name and new_name != tenant['username']:
                c.execute('SELECT id FROM tenants WHERE username = ?', (new_name,))
                if c.fetchone():
                    conn.close()
                    return jsonify({'error': 'Username already taken'}), 400
                
                # Rename storage directory
                old_dir = os.path.join(STORAGE_BASE_DIR, tenant['username'])
                new_dir = os.path.join(STORAGE_BASE_DIR, new_name)
                try:
                    if os.path.exists(old_dir):
                        os.rename(old_dir, new_dir)
                    else:
                        os.makedirs(new_dir, exist_ok=True)
                except Exception as e:
                    conn.close()
                    return jsonify({'error': f'Failed to rename storage: {str(e)}'}), 500
                    
                # Update device views
                c.execute('UPDATE device_views SET tenant_username = ? WHERE tenant_username = ?', (new_name, tenant['username']))
                username = new_name
                
        c.execute('UPDATE tenants SET password = ?, username = ?, password_setup_token = NULL WHERE id = ?', (password, username, tenant['id']))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Account password and details configured successfully.', 'redirect': 'https://echo.oqens.me/login'})
        
    # 3. Check Support Team setup
    c.execute('SELECT * FROM support_team WHERE password_setup_token = ?', (token,))
    member = c.fetchone()
    if member:
        name = member['name']
        if new_name:
            new_name = new_name.strip()
            if new_name and new_name != member['name']:
                c.execute('SELECT id FROM support_team WHERE name = ?', (new_name,))
                if c.fetchone():
                    conn.close()
                    return jsonify({'error': 'Name already taken'}), 400
                name = new_name
                
        c.execute('UPDATE support_team SET password = ?, name = ?, password_setup_token = NULL WHERE id = ?', (password, name, member['id']))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Support agent password and details configured successfully.', 'redirect': 'https://echo.oqens.me/login'})
        
    conn.close()
    return jsonify({'error': 'Invalid or expired setup token'}), 400

@app.route('/api/auth/forgot-code', methods=['POST'])
def forgot_code():
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Check Tenants list
    c.execute('SELECT * FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
    tenant = c.fetchone()
    
    # Check Support Team list
    c.execute('SELECT * FROM support_team WHERE email = ?', (email,))
    support_member = c.fetchone()
    
    if not tenant and not support_member:
        conn.close()
        return jsonify({'error': 'User not existed'}), 404
        
    user_id = tenant['id'] if tenant else support_member['id']
    table = 'tenants' if tenant else 'support_team'
    name = tenant['username'] if tenant else support_member['name']
    secret_code = tenant['secret_code'] if tenant else support_member['secret_code']
    has_password = bool(tenant['password'] if tenant else support_member['password'])
    first_time = tenant['first_login_time'] if tenant else support_member['first_login_time']
    
    if has_password or first_time:
        import secrets
        token = secrets.token_hex(20)
        c.execute(f'UPDATE {table} SET password_setup_token = ? WHERE id = ?', (token, user_id))
        conn.commit()
        conn.close()
        
        setup_link = f"https://auth.oqens.me/setup-password?token={token}"
        subject = "Reset your OQENS Account Password"
        html_body = f"""
        <h3>Account Recovery</h3>
        <p>Hello {name},</p>
        <p>We received a request to recover your account credentials.</p>
        <p>Please click the link below to configure a new password for your account:</p>
        <p><a href="{setup_link}" style="display:inline-block; padding:10px 20px; background:#111; color:#fff; text-decoration:none; border-radius:5px;">Reset Password</a></p>
        <p>If you did not request this, please ignore this email.</p>
        """
        if send_system_email(email, subject, html_body):
            return jsonify({'status': 'success', 'message': 'A password reset link has been sent to your email.'})
        else:
            return jsonify({'error': 'Failed to send recovery email. Contact administrator.'}), 500
    else:
        conn.close()
        subject = "Your OQENS Access Credentials"
        html_body = f"""
        <h3>Account Recovery</h3>
        <p>Hello {name},</p>
        <p>Your temporary OQENS Secret Access Code is: <strong>{secret_code}</strong></p>
        <p>You can use this code to authenticate at <a href="https://auth.oqens.me/">auth.oqens.me</a>.</p>
        <p>Note: This temporary access code will be valid for 10 minutes starting from your first login entry.</p>
        """
        if send_system_email(email, subject, html_body):
            return jsonify({'status': 'success', 'message': 'Temporary access credentials sent to your email.'})
        else:
            return jsonify({'error': 'Failed to send recovery email. Contact administrator.'}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'success'})

@app.route('/api/session', methods=['GET'])
def get_session():
    # If logged in as tenant, return updated tenant details (storage used, limit, bandwidth, api key, cloud ID)
    sess_data = dict(session)
    if session.get('role') == 'tenant' and session.get('tenant_id'):
        tenant = get_tenant(session['tenant_id'])
        if tenant:
            sess_data['tenant_username'] = tenant['username']
            sess_data['email'] = tenant.get('custom_domain_email')
            sess_data['storage_used_bytes'] = tenant['storage_used_bytes']
            sess_data['storage_limit_bytes'] = tenant['storage_limit_bytes']
            sess_data['tenant_secret_code'] = tenant['secret_code']
            sess_data['cloud_id'] = tenant.get('cloud_id')
            sess_data['api_key'] = tenant.get('api_key')
            sess_data['bandwidth_bytes'] = tenant.get('bandwidth_bytes', 0)
            sess_data['bandwidth_limit_bytes'] = tenant.get('bandwidth_limit_bytes', 0)
            sess_data['unique_views_count'] = tenant.get('unique_views_count', 0)
            sess_data['downloads_count'] = tenant.get('downloads_count', 0)
            sess_data['custom_domain'] = tenant.get('custom_domain')
            sess_data['custom_domain_email'] = tenant.get('custom_domain_email')
            sess_data['custom_domain_verified'] = tenant.get('custom_domain_verified', 0)
            sess_data['monthly_rate'] = tenant.get('monthly_rate', 0.0)
            sess_data['auto_renew'] = tenant.get('auto_renew', 1)
            sess_data['next_billing_date'] = tenant.get('next_billing_date')
            sess_data['billing_status'] = tenant.get('billing_status', 'Active')
            sess_data['checkout_token'] = tenant.get('checkout_token')
            sess_data['checkout_token_obfuscated'] = obfuscate_token(tenant.get('checkout_token'))
            sess_data['tenant_id'] = tenant.get('id')
            sess_data['custom_message'] = tenant.get('custom_message')
            sess_data['custom_message_theme'] = tenant.get('custom_message_theme')
            sess_data['custom_message_icon'] = tenant.get('custom_message_icon')
            sess_data['custom_message_active'] = tenant.get('custom_message_active', 0)
            # Dynamically fetch all feature flags and merge their values
            try:
                conn_f = sqlite3.connect(DB_FILE)
                c_f = conn_f.cursor()
                c_f.execute('SELECT flag_key FROM feature_flags')
                all_flags = [r[0] for r in c_f.fetchall()]
                conn_f.close()
                for flag in all_flags:
                    sess_data[flag] = tenant.get(flag, 0)
            except Exception as e:
                sess_data['allot_collections'] = tenant.get('allot_collections', 0)
                sess_data['allot_backgrounds'] = tenant.get('allot_backgrounds', 0)
            # Return display_tier if set by host referral, otherwise real tier
            real_tier = tenant.get('tier', 'free')
            display_tier = tenant.get('display_tier') or None
            sess_data['tier'] = display_tier if display_tier else real_tier
            
            # Fetch global banner config
            try:
                conn_g = sqlite3.connect(DB_FILE)
                c_g = conn_g.cursor()
                c_g.execute('SELECT key, value FROM system_config WHERE key IN ("global_banner_active", "global_banner_message", "global_banner_theme", "global_banner_icon")')
                for row in c_g.fetchall():
                    sess_data[row[0]] = row[1]
                conn_g.close()
            except Exception:
                pass
        else:
            session.clear()
            sess_data = {'logged_in': False, 'role': None}
    return jsonify(sess_data)

@app.route('/api/support/login', methods=['POST'])
def support_login():
    data = request.json or {}
    code = data.get('code')
    if not code:
        return jsonify({'error': 'Secret code required'}), 400
    
    session.pop('support_logged_out', None)
    session.permanent = True
    
    # Check admin
    if code == ADMIN_SECRET:
        session['support_role'] = 'admin'
        session['support_username'] = 'System Administrator'
        return jsonify({'status': 'success', 'role': 'admin'})
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Check support team member
    c.execute('SELECT * FROM support_team WHERE secret_code = ?', (code,))
    member = c.fetchone()
    if member:
        conn.close()
        session['support_role'] = 'admin'
        session['support_username'] = f"{member['name']} (Support)"
        return jsonify({'status': 'success', 'role': 'admin', 'username': member['name']})
        
    c.execute('SELECT * FROM tenants WHERE secret_code = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (code,))
    tenant = c.fetchone()
    conn.close()
    
    if tenant:
        session['support_role'] = 'tenant'
        session['support_tenant_id'] = tenant['id']
        session['support_username'] = tenant['username']
        return jsonify({'status': 'success', 'role': 'tenant', 'username': tenant['username']})
    
    return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/support/session', methods=['GET'])
def support_session():
    if session.get('support_logged_out'):
        return jsonify({'logged_in': False})
        
    if 'support_role' in session:
        return jsonify({
            'logged_in': True,
            'role': session['support_role'],
            'username': session.get('support_username'),
            'tenant_id': session.get('support_tenant_id')
        })
    # Check if they are already logged in to OQENS dashboard in the same session
    if session.get('role') == 'admin':
        session['support_role'] = 'admin'
        session['support_username'] = 'System Administrator'
        return jsonify({
            'logged_in': True,
            'role': 'admin',
            'username': 'System Administrator'
        })
    elif session.get('role') == 'tenant' and session.get('tenant_id'):
        session['support_role'] = 'tenant'
        session['support_tenant_id'] = session['tenant_id']
        session['support_username'] = session.get('tenant_username')
        return jsonify({
            'logged_in': True,
            'role': 'tenant',
            'username': session.get('tenant_username'),
            'tenant_id': session['tenant_id']
        })
    return jsonify({'logged_in': False})

@app.route('/api/support/logout', methods=['POST'])
def support_logout():
    session.pop('support_role', None)
    session.pop('support_tenant_id', None)
    session.pop('support_username', None)
    session['support_logged_out'] = True
    return jsonify({'status': 'success'})

@app.route('/api/support/tickets', methods=['GET', 'POST'])
def support_tickets():
    role = session.get('support_role')
    
    if request.method == 'GET':
        if not role:
            return jsonify({'error': 'Unauthorized'}), 401
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if role == 'admin':
            c.execute('''
                SELECT t.*, tn.username as tenant_name 
                FROM support_tickets t 
                LEFT JOIN tenants tn ON t.tenant_id = tn.id 
                ORDER BY t.created_at DESC
            ''')
        else:
            c.execute('SELECT * FROM support_tickets WHERE tenant_id = ? ORDER BY created_at DESC', (session['support_tenant_id'],))
        tickets = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'tickets': tickets})
        
    elif request.method == 'POST':
        data = request.json or {}
        code = data.get('code')
        if code:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute('SELECT * FROM tenants WHERE secret_code = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (code,))
            tenant = c.fetchone()
            conn.close()
            if tenant:
                session['support_role'] = 'tenant'
                session['support_tenant_id'] = tenant['id']
                session['support_username'] = tenant['username']
                role = 'tenant'
            else:
                return jsonify({'error': 'Invalid secret code'}), 401
                
        if not role:
            return jsonify({'error': 'Unauthorized'}), 401
            
        if role != 'tenant':
            return jsonify({'error': 'Only tenants can create tickets'}), 403
        title = data.get('title')
        category = data.get('category', 'General')
        message = data.get('message')
        
        if not title or not message:
            return jsonify({'error': 'Title and message are required'}), 400
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO support_tickets (tenant_id, title, category, status) VALUES (?, ?, ?, \'Open\')', 
                  (session['support_tenant_id'], title, category))
        ticket_id = c.lastrowid
        c.execute('INSERT INTO support_messages (ticket_id, sender, message) VALUES (?, \'tenant\', ?)', 
                  (ticket_id, message))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'ticket_id': ticket_id})

@app.route('/api/support/tickets/<int:ticket_id>/messages', methods=['GET', 'POST'])
def support_ticket_messages(ticket_id):
    role = session.get('support_role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM support_tickets WHERE id = ?', (ticket_id,))
    ticket = c.fetchone()
    if not ticket:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
        
    if role == 'tenant' and ticket['tenant_id'] != session['support_tenant_id']:
        conn.close()
        return jsonify({'error': 'Unauthorized access to ticket'}), 403
        
    if request.method == 'GET':
        c.execute('SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY created_at ASC', (ticket_id,))
        messages = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({
            'ticket': dict(ticket),
            'messages': messages
        })
        
    elif request.method == 'POST':
        data = request.json or {}
        message = data.get('message')
        if not message:
            conn.close()
            return jsonify({'error': 'Message cannot be empty'}), 400
        
        c.execute('INSERT INTO support_messages (ticket_id, sender, message) VALUES (?, ?, ?)', 
                  (ticket_id, role, message))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})

@app.route('/api/support/tickets/<int:ticket_id>/resolve', methods=['POST'])
def support_ticket_resolve(ticket_id):
    role = session.get('support_role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if role == 'tenant':
        c.execute('SELECT tenant_id FROM support_tickets WHERE id = ?', (ticket_id,))
        t = c.fetchone()
        if not t or t[0] != session['support_tenant_id']:
            conn.close()
            return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.json or {}
    status = data.get('status', 'Resolved')
    if status not in ['Open', 'Resolved']:
        status = 'Resolved'
        
    c.execute('UPDATE support_tickets SET status = ? WHERE id = ?', (status, ticket_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/support/polls', methods=['GET', 'POST'])
def support_polls_api():
    role = session.get('support_role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
        
    import json
    if request.method == 'GET':
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT * FROM support_polls ORDER BY created_at DESC')
        rows = c.fetchall()
        conn.close()
        polls = []
        for r in rows:
            p = dict(r)
            try:
                p['options'] = json.loads(p['options'])
            except Exception:
                p['options'] = []
            polls.append(p)
        return jsonify({'polls': polls})
        
    elif request.method == 'POST':
        if role != 'admin':
            return jsonify({'error': 'Only support team members can create polls'}), 403
            
        data = request.json or {}
        question = data.get('question', '').strip()
        options = data.get('options', [])
        
        if not question or not options:
            return jsonify({'error': 'Question and options are required'}), 400
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO support_polls (question, options) VALUES (?, ?)', (question, json.dumps(options)))
        poll_id = c.lastrowid
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'poll_id': poll_id})

@app.route('/api/support/polls/<int:poll_id>', methods=['GET'])
def get_support_poll(poll_id):
    role = session.get('support_role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
        
    import json
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM support_polls WHERE id = ?', (poll_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Poll not found'}), 404
        
    poll = dict(row)
    try:
        poll['options'] = json.loads(poll['options'])
    except Exception:
        poll['options'] = []
        
    c.execute('SELECT selected_option, COUNT(*) FROM support_poll_votes WHERE poll_id = ? GROUP BY selected_option', (poll_id,))
    vote_rows = c.fetchall()
    votes = {r[0]: r[1] for r in vote_rows}
    
    poll['votes'] = {opt: votes.get(opt, 0) for opt in poll['options']}
    poll['total_votes'] = sum(poll['votes'].values())
    
    voter_id = session.get('support_username') or 'anonymous'
    c.execute('SELECT selected_option FROM support_poll_votes WHERE poll_id = ? AND voter_id = ?', (poll_id, voter_id))
    vote_row = c.fetchone()
    poll['user_voted_option'] = vote_row[0] if vote_row else None
    
    conn.close()
    return jsonify(poll)

@app.route('/api/support/polls/<int:poll_id>/vote', methods=['POST'])
def vote_support_poll(poll_id):
    role = session.get('support_role')
    if not role:
        return jsonify({'error': 'Unauthorized'}), 401
        
    import json
    data = request.json or {}
    option = data.get('option')
    if not option:
        return jsonify({'error': 'Option is required'}), 400
        
    voter_id = session.get('support_username') or 'anonymous'
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT options FROM support_polls WHERE id = ?', (poll_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Poll not found'}), 404
        
    try:
        options = json.loads(row[0])
    except Exception:
        options = []
        
    if option not in options:
        conn.close()
        return jsonify({'error': 'Invalid option'}), 400
        
    try:
        c.execute('INSERT OR REPLACE INTO support_poll_votes (poll_id, voter_id, selected_option) VALUES (?, ?, ?)',
                  (poll_id, voter_id, option))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500
        
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/tenant/generate-api', methods=['POST'])
def generate_api_key():
    if session.get('role') != 'tenant' or not session.get('tenant_id'):
        return jsonify({"error": "Unauthorized"}), 401
        
    import secrets
    new_api_key = "oqens_api_" + secrets.token_hex(24)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET api_key = ? WHERE id = ?', (new_api_key, session['tenant_id']))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "api_key": new_api_key})

# --- Admin APIs ---

@app.route('/api/admin/config', methods=['GET', 'POST'])
def admin_config():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        data = request.json or {}
        for k in ['cashfree_app_id', 'cashfree_secret_key', 'cashfree_mode', 'mailman_access_code', 'mailman_secret_key', 'mailman_base_url', 'host_email', 'global_banner_active', 'global_banner_message', 'global_banner_theme', 'global_banner_icon', 'host_max_storage_gb', 'host_max_tenants', 'host_max_bandwidth_gb']:
            if k in data:
                c.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)', (k, str(data[k])))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
        
    # GET config
    c.execute('SELECT key, value FROM system_config')
    rows = c.fetchall()
    conn.close()
    
    config = {row[0]: row[1] for row in rows}
    return jsonify(config)

@app.route('/api/admin/capacity-stats', methods=['GET'])
def admin_capacity_stats():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Get config limits
    c.execute("SELECT key, value FROM system_config WHERE key IN ('host_max_storage_gb', 'host_max_tenants', 'host_max_bandwidth_gb')")
    configs = {row['key']: row['value'] for row in c.fetchall()}
    host_max_storage_gb = float(configs.get('host_max_storage_gb') or 0)
    host_max_tenants = int(configs.get('host_max_tenants') or 0)
    host_max_bandwidth_gb = float(configs.get('host_max_bandwidth_gb') or 0)
    
    # Calculate current allocations
    c.execute('SELECT SUM(storage_limit_bytes) as total_bytes, SUM(bandwidth_limit_bytes) as total_bandwidth, COUNT(id) as total_tenants FROM tenants WHERE is_deleted = 0 OR is_deleted IS NULL')
    row = c.fetchone()
    allocated_bytes = int(row['total_bytes'] or 0)
    allocated_bandwidth_bytes = int(row['total_bandwidth'] or 0)
    active_tenants = int(row['total_tenants'] or 0)
    
    conn.close()
    
    return jsonify({
        "status": "success",
        "allocated_bytes": allocated_bytes,
        "allocated_bandwidth_bytes": allocated_bandwidth_bytes,
        "active_tenants": active_tenants,
        "host_max_storage_gb": host_max_storage_gb,
        "host_max_bandwidth_gb": host_max_bandwidth_gb,
        "host_max_tenants": host_max_tenants
    })

@app.route('/api/system/pricing', methods=['GET'])
def system_pricing():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT plan_id, name, monthly_rate, storage_limit_gb, bandwidth_limit_gb, features, status FROM subscription_plans WHERE status = 'Active' ORDER BY monthly_rate ASC")
    rows = c.fetchall()
    conn.close()
    
    plans = []
    for r in rows:
        plans.append({
            'plan_id': r[0],
            'plan_name': r[1],
            'name': r[1],
            'monthly_rate': float(r[2]),
            'storage_limit_gb': float(r[3]),
            'bandwidth_limit_gb': float(r[4]),
            'features': json.loads(r[5]) if r[5] else [],
            'features_json': r[5] or '[]',
            'status': r[6]
        })
    return jsonify({"status": "success", "plans": plans})

@app.route('/api/admin/plans', methods=['GET', 'POST'])
def admin_plans():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        data = request.json or {}
        plan_id = data.get('plan_id', '').strip().lower()
        name = data.get('plan_name', '').strip()
        rate = float(data.get('monthly_rate', 0.0))
        storage = float(data.get('storage_limit_gb', 0.0))
        bw = float(data.get('bandwidth_limit_gb', 0.0))
        features = data.get('features_json', '[]')
        status = data.get('status', 'Active')
        
        if not plan_id or not name:
            conn.close()
            return jsonify({"error": "Plan ID and Plan Name are required"}), 400
            
        try:
            c.execute('INSERT INTO subscription_plans (plan_id, name, monthly_rate, storage_limit_gb, bandwidth_limit_gb, features, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                     (plan_id, name, rate, storage, bw, features, status))
            conn.commit()
            conn.close()
            return jsonify({"status": "success"})
        except Exception as e:
            conn.rollback()
            conn.close()
            return jsonify({"error": f"Plan ID already exists or database error: {str(e)}"}), 400
            
    else:
        c.execute('SELECT id, plan_id, name, monthly_rate, storage_limit_gb, bandwidth_limit_gb, features, status FROM subscription_plans ORDER BY monthly_rate ASC')
        rows = c.fetchall()
        conn.close()
        plans = []
        for r in rows:
            plans.append({
                'id': r[0],
                'plan_id': r[1],
                'plan_name': r[2],
                'name': r[2],
                'monthly_rate': float(r[3]),
                'storage_limit_gb': float(r[4]),
                'bandwidth_limit_gb': float(r[5]),
                'features': json.loads(r[6]) if r[6] else [],
                'features_json': r[6] or '[]',
                'status': r[7]
            })
        return jsonify({"status": "success", "plans": plans})

@app.route('/api/admin/plans/<plan_id>', methods=['PUT', 'DELETE'])
def admin_plan_detail(plan_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'PUT':
        data = request.json or {}
        name = data.get('plan_name', '').strip()
        rate = float(data.get('monthly_rate', 0.0))
        storage = float(data.get('storage_limit_gb', 0.0))
        bw = float(data.get('bandwidth_limit_gb', 0.0))
        features = data.get('features_json', '[]')
        status = data.get('status', 'Active')
        
        if not name:
            conn.close()
            return jsonify({"error": "Plan Name is required"}), 400
            
        c.execute('UPDATE subscription_plans SET name=?, monthly_rate=?, storage_limit_gb=?, bandwidth_limit_gb=?, features=?, status=? WHERE plan_id=?',
                 (name, rate, storage, bw, features, status, plan_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
        
    elif request.method == 'DELETE':
        c.execute('DELETE FROM subscription_plans WHERE plan_id=?', (plan_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})

@app.route('/api/enquiries/submit', methods=['POST'])
def submit_enquiry():
    data = request.json or {}
    email = data.get('email', '').strip()
    mobile = data.get('mobile', '').strip()
    
    def clean_val(val):
        if val is None:
            return ""
        if isinstance(val, (int, float)):
            if float(val).is_integer():
                return str(int(val))
            return str(val)
        s = str(val).strip()
        try:
            # check if it represents a clean float/int
            f = float(s)
            if f.is_integer():
                return str(int(f))
            return str(f)
        except ValueError:
            return s

    storage = clean_val(data.get('storage_req') or data.get('req_storage_gb'))
    bw = clean_val(data.get('bandwidth_req') or data.get('req_bandwidth_gb'))
    budget = str(data.get('preferred_budget', '')).strip()
    msg = data.get('message', '').strip()
    
    if not email:
        return jsonify({"error": "Email is required"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO plan_enquiries (email, mobile, storage_req, bandwidth_req, message, preferred_budget) VALUES (?, ?, ?, ?, ?, ?)',
              (email, mobile, storage, bw, msg, budget))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

def send_rejection_email(email):
    subject = "Update regarding your OQENS custom plan enquiry"
    html_body = f"""
    <div style="font-family: 'Inter', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #0f172a; line-height: 1.5;">
        <h2 style="color: #0f172a; font-weight: 600; margin-bottom: 15px;">Plan Enquiry Update</h2>
        <p>Hi,</p>
        <p>Thank you for your interest in OQENS. We received your request for a custom storage plan.</p>
        <p>We regret to inform you that we are unable to allot a custom workspace with the exact specifications requested at this time.</p>
        <p>If you have any questions or would like to discuss alternative options, please feel free to reach out to our support team.</p>
        <p style="margin-top: 25px; border-top: 1px solid #e2e8f0; padding-top: 15px; font-size: 0.88rem; color: #64748b;">
            Best regards,<br>
            <strong>OQENS Team</strong>
        </p>
    </div>
    """
    send_system_email(email, subject, html_body)

@app.route('/api/admin/enquiries', methods=['GET', 'POST'])
def admin_enquiries():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'POST':
        data = request.json or {}
        eid = data.get('id')
        action = data.get('action')
        
        if action == 'mark_read':
            c.execute("UPDATE plan_enquiries SET status = 'Read' WHERE id = ?", (eid,))
        elif action == 'delete':
            c.execute('SELECT email FROM plan_enquiries WHERE id = ?', (eid,))
            row = c.fetchone()
            if row:
                try:
                    send_rejection_email(row[0])
                except Exception as e:
                    print("Failed to send rejection email:", e)
            c.execute('DELETE FROM plan_enquiries WHERE id = ?', (eid,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
        
    c.execute('SELECT id, email, mobile, storage_req, bandwidth_req, message, status, created_at, preferred_budget FROM plan_enquiries ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    
    enquiries = []
    for r in rows:
        enquiries.append({
            'id': r[0],
            'email': r[1],
            'mobile': r[2],
            'storage_req': r[3],
            'bandwidth_req': r[4],
            'message': r[5],
            'status': r[6],
            'created_at': r[7],
            'preferred_budget': r[8] if len(r) > 8 and r[8] else 'N/A'
        })
    return jsonify({"enquiries": enquiries})

@app.route('/api/admin/enquiries/<int:id>/read', methods=['POST'])
def admin_enquiry_read(id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE plan_enquiries SET status = 'Read' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/enquiries/<int:id>', methods=['DELETE'])
def admin_enquiry_delete(id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, email, status FROM plan_enquiries WHERE id = ?', (id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Enquiry not found"}), 404
        
    enquiry = dict(row)
    email = enquiry['email'].strip().lower()
    status = enquiry['status']
    
    if status == 'Approved':
        # Find the tenant associated with this email
        c.execute('SELECT id, username, password, tier, billing_status FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
        t_row = c.fetchone()
        if t_row:
            tenant = dict(t_row)
            # Only revert or delete if they haven't paid yet
            if tenant['billing_status'] in ['Pending Payment', 'Pending Verification']:
                if not tenant.get('password'):
                    # New tenant that has not set up account/password yet -> Delete them completely
                    c.execute('DELETE FROM tenants WHERE id = ?', (tenant['id'],))
                    tenant_dir = os.path.join(STORAGE_BASE_DIR, tenant['username'])
                    if os.path.exists(tenant_dir):
                        import shutil
                        shutil.rmtree(tenant_dir, ignore_errors=True)
                else:
                    # Upgraded tenant who hasn't paid for custom plan yet -> Revert to standard tier configs
                    tier = tenant.get('tier', 'free') or 'free'
                    plan_cfg = get_plan_config(tier)
                    c.execute('''UPDATE tenants SET storage_limit_bytes = ?, bandwidth_limit_bytes = ?, monthly_rate = ?,
                                 billing_status = 'Active', checkout_token = NULL, checkout_token_expires_at = NULL WHERE id = ?''',
                              (plan_cfg['storage_limit'], plan_cfg['bandwidth_limit'], plan_cfg['rate'], tenant['id']))
    else:
        # Only send rejection email if it was not approved
        try:
            send_rejection_email(email)
        except Exception as e:
            print("Failed to send rejection email:", e)
            
    c.execute('DELETE FROM plan_enquiries WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/enquiries/<int:id>/reject', methods=['POST'])
def admin_enquiry_reject(id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT email FROM plan_enquiries WHERE id = ?', (id,))
    row = c.fetchone()
    if row:
        try:
            send_rejection_email(row[0])
        except Exception as e:
            print("Failed to send rejection email:", e)
            
    c.execute("UPDATE plan_enquiries SET status = 'Rejected' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/enquiries/<int:id>/accept', methods=['POST'])
def admin_enquiry_accept(id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json or {}
    monthly_rate = float(data.get('monthly_rate', 150.0))
    allot_storage = float(data.get('allot_storage', 5.0))
    allot_bandwidth = float(data.get('allot_bandwidth', 20.0))
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT id, email, mobile, storage_req, bandwidth_req, message, status FROM plan_enquiries WHERE id = ?', (id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Enquiry not found"}), 404
        
    enquiry = {
        'id': row[0],
        'email': row[1],
        'mobile': row[2],
        'storage_req': row[3],
        'bandwidth_req': row[4],
        'message': row[5],
        'status': row[6]
    }
    
    # Convert custom limits to bytes
    storage_limit_bytes = int(allot_storage * 1024**3)
    bandwidth_limit_bytes = int(allot_bandwidth * 1024**3)
    
    # 2. Check if active tenant already exists — if so, upgrade their plan instead of creating new
    email = enquiry['email'].strip().lower()
    c.execute('SELECT id, username FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
    existing = c.fetchone()
    if existing:
        existing_id, existing_username = existing[0], existing[1]
        
        # Generate checkout token for upgrade
        import secrets
        import datetime
        checkout_token = secrets.token_hex(12)
        expires_at = (datetime.datetime.utcnow() + datetime.timedelta(days=7)).isoformat()
        
        # Upgrade existing tenant's storage/bandwidth/rate and set status/tokens
        c.execute('''UPDATE tenants SET storage_limit_bytes=?, bandwidth_limit_bytes=?, monthly_rate=?,
                     billing_status='Pending Payment', checkout_token=?, checkout_token_expires_at=? WHERE id=?''',
                  (storage_limit_bytes, bandwidth_limit_bytes, monthly_rate, checkout_token, expires_at, existing_id))
        c.execute("UPDATE plan_enquiries SET status='Approved' WHERE id=?", (id,))
        conn.commit()
        conn.close()
        
        # Generate checkout URL
        checkout_url = f"https://payments.oqens.me/checkout?token={checkout_token}"
        
        # Send confirmation email with payment link
        subject = "Your OQENS Custom Plan has been Approved!"
        html_body = f"""
        <div style="font-family: 'Inter', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #0f172a; line-height: 1.5;">
            <h2 style="color: #0f172a; font-weight: 600; margin-bottom: 15px;">Your Plan Has Been Approved!</h2>
            <p>Hi {existing_username},</p>
            <p>Your custom plan enquiry has been approved and your existing workspace has been upgraded.</p>
            <div style="background: #fafafa; border: 1.5px solid #eeeeee; padding: 20px; border-radius: 8px; margin: 20px 0;">
                <h4 style="margin-top: 0; margin-bottom: 10px; font-weight: 500;">Updated Allocation:</h4>
                <ul style="padding-left: 20px; margin: 0; line-height: 1.8;">
                    <li><strong>Storage:</strong> {allot_storage} GB</li>
                    <li><strong>Bandwidth:</strong> {allot_bandwidth} GB</li>
                    <li><strong>Monthly Rate:</strong> INR {monthly_rate:.2f}</li>
                </ul>
            </div>
            <p>To activate your upgraded plan, please complete the payment using the link below:</p>
            <p style="margin-bottom: 25px;"><a href="{checkout_url}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500;">Pay Invoice Now</a></p>
            <p>Log in to your dashboard to see your updated limits after payment.</p>
            <p style="margin-top: 25px; border-top: 1px solid #e2e8f0; padding-top: 15px; font-size: 0.88rem; color: #64748b;">
                Best regards,<br><strong>OQENS Team</strong>
            </p>
        </div>
        """
        try:
            send_system_email(email, subject, html_body)
        except Exception:
            pass
            
        return jsonify({
            "status": "success",
            "upgraded": True,
            "username": existing_username,
            "secret_code": "Already Registered",
            "checkout_url": checkout_url,
            "tenant_id": existing_id
        })
        
    username_base = email.split('@')[0]
    import re
    username = re.sub(r'[^a-zA-Z0-9_]', '', username_base)
    if not username:
        username = "tenant"
        
    base_username = username
    counter = 1
    while True:
        c.execute('SELECT id FROM tenants WHERE username = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (username,))
        if not c.fetchone():
            break
        username = f"{base_username}_{counter}"
        counter += 1
        
    # 3. Generate unique secret code
    import random
    import string
    while True:
        secret_code = "".join(random.choices(string.digits, k=4))
        c.execute('SELECT id FROM tenants WHERE secret_code = ?', (secret_code,))
        if not c.fetchone():
            break
            
    # 4. Generate unique cloud ID
    while True:
        cid = generate_cloud_id()
        c.execute('SELECT id FROM tenants WHERE cloud_id = ?', (cid,))
        if not c.fetchone():
            break
            
    # 5. Generate unique checkout token
    import secrets
    checkout_token = secrets.token_hex(8)
    while True:
        c.execute('SELECT id FROM tenants WHERE checkout_token = ?', (checkout_token,))
        if not c.fetchone():
            break
        checkout_token = secrets.token_hex(8)
        
    # 6. Insert tenant
    c.execute('''INSERT INTO tenants (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, cloud_id, custom_domain_email, monthly_rate, billing_status, checkout_token) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
              (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, cid, email, monthly_rate, 'Pending Payment', checkout_token))
    conn.commit()
    
    # Create directory
    tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
    os.makedirs(tenant_dir, exist_ok=True)
    
    # 7. Update enquiry status
    c.execute("UPDATE plan_enquiries SET status = 'Approved' WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    
    # 8. Send Email
    checkout_url = f"https://payments.oqens.me/checkout?token={checkout_token}"
    subject = "Your OQENS Custom Plan is Approved!"
    html_body = f"""
    <div style="font-family: 'Inter', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #0f172a; line-height: 1.5;">
        <h2 style="color: #0f172a; font-weight: 600; margin-bottom: 15px;">Your Custom Storage Plan is Ready!</h2>
        <p>Hi,</p>
        <p>We are pleased to inform you that your custom plan enquiry has been approved by the administrator.</p>
        
        <div style="background: #fafafa; border: 1.5px solid #eeeeee; padding: 20px; border-radius: 8px; margin: 20px 0;">
            <h4 style="margin-top: 0; margin-bottom: 10px; font-weight: 500;">Workspace Allocation Details:</h4>
            <ul style="padding-left: 20px; margin: 0; line-height: 1.8;">
                <li><strong>Username / Workspace:</strong> {username}</li>
                <li><strong>Secret Access Code:</strong> {secret_code}</li>
                <li><strong>Allotted Storage:</strong> {allot_storage} GB</li>
                <li><strong>Allotted Bandwidth:</strong> {allot_bandwidth} GB</li>
                <li><strong>Monthly Rate:</strong> INR {monthly_rate:.2f}</li>
            </ul>
        </div>
        
        <p>Please complete your payment using the link below to activate your custom storage workspace:</p>
        <p style="text-align: center; margin: 30px 0;">
            <a href="{checkout_url}" style="background: #111; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500; display: inline-block;">Complete Payment & Activate</a>
        </p>
        <p>Alternatively, you can copy and paste this URL into your browser:</p>
        <p style="font-size: 0.85rem; color: #64748b; word-break: break-all;">{checkout_url}</p>
        
        <p style="margin-top: 25px; border-top: 1px solid #e2e8f0; padding-top: 15px; font-size: 0.88rem; color: #64748b;">
            Best regards,<br>
            <strong>OQENS Team</strong>
        </p>
    </div>
    """
    send_system_email(email, subject, html_body)
    
    return jsonify({
        "status": "success",
        "checkout_url": checkout_url,
        "username": username,
        "secret_code": secret_code
    })

def verify_domain_ssl_core(domain, email):
    import os
    import tempfile
    import subprocess

    BASE_DOMAINS = {
        'echo.oqens.me', 'host.echo.oqens.me', 'dl.oqens.me', 
        'auth.oqens.me', 'auth2.oqens.me', 'payments.oqens.me', 
        'papers.oqens.me', 'support.oqens.me', 'host.support.oqens.me',
        'dash.echo.oqens.me', 'status.echo.oqens.me', 'dash.oqens.me', 'status.oqens.me'
    }

    if domain in BASE_DOMAINS:
        # Already handled by oqensbase. Ensure redundant individual files are removed.
        subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
        subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
        return {"status": "success", "note": "Base domain, handled by oqensbase"}

    ssl_cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
    ssl_key_path = f"/etc/letsencrypt/live/{domain}/privkey.pem"

    # Step 1: Ensure we have a valid certificate. If not, run certbot to obtain it.
    if not os.path.exists(ssl_cert_path):
        nginx_conf_port80 = f"""server {{
    listen 80;
    server_name {domain};
    include /etc/nginx/snippets/x-accel.conf;

    location / {{
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
        try:
            with tempfile.NamedTemporaryFile('w', delete=False) as f:
                f.write(nginx_conf_port80)
                temp_path = f.name
            
            subprocess.run(["sudo", "mv", temp_path, f"/etc/nginx/sites-available/{domain}"], check=True)
            subprocess.run(["sudo", "chmod", "644", f"/etc/nginx/sites-available/{domain}"], check=True)
            subprocess.run(["sudo", "ln", "-sf", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=True)
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=True)
            
            certbot_cmd = [
                "sudo", "certbot", "certonly", "--nginx",
                "-d", domain,
                "-m", email,
                "--agree-tos",
                "--non-interactive"
            ]
            res_cert = subprocess.run(certbot_cmd, capture_output=True, text=True)
            if res_cert.returncode != 0:
                subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
                subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
                return {"error": f"Certbot failed: {res_cert.stderr or res_cert.stdout}"}
        except Exception as e:
            subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
            return {"error": f"Failed to obtain certificate: {str(e)}"}

    # Step 2: Write/Overwrite the Nginx configuration with both HTTP redirect and HTTPS SSL blocks
    nginx_conf_ssl = f"""server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {domain};
    include /etc/nginx/snippets/x-accel.conf;

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {{
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
    try:
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write(nginx_conf_ssl)
            temp_path = f.name
        
        subprocess.run(["sudo", "mv", temp_path, f"/etc/nginx/sites-available/{domain}"], check=True)
        subprocess.run(["sudo", "chmod", "644", f"/etc/nginx/sites-available/{domain}"], check=True)
        subprocess.run(["sudo", "ln", "-sf", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=True)
        subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=True)
        return {"status": "success"}
    except Exception as e:
        return {"error": f"Failed to apply SSL Nginx configuration: {str(e)}"}

def auto_verify_all_domains():
    # Use module-level sqlite3 (which is pg_wrapper in prod)
    
    BASE_DOMAINS = {
        'echo.oqens.me', 'host.echo.oqens.me', 'dl.oqens.me', 
        'auth.oqens.me', 'auth2.oqens.me', 'payments.oqens.me', 
        'papers.oqens.me', 'support.oqens.me', 'host.support.oqens.me',
        'dash.echo.oqens.me', 'status.echo.oqens.me', 'dash.oqens.me', 'status.oqens.me'
    }
    
    # Clean redundant config files for base domains
    import subprocess
    for d in BASE_DOMAINS:
        subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-enabled/{d}", f"/etc/nginx/sites-available/{d}"], check=False)
    subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT id, domain, email FROM system_domains")
    sys_rows = c.fetchall()
    
    c.execute("SELECT id, custom_domain, custom_domain_email FROM tenants WHERE custom_domain IS NOT NULL AND custom_domain != '' AND (is_deleted = 0 OR is_deleted IS NULL)")
    tenant_rows = c.fetchall()
    conn.close()
    
    for row in sys_rows:
        domain_id = row['id']
        domain = row['domain'].strip().lower()
        email = row['email'].strip().lower()
        
        if domain in BASE_DOMAINS:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE system_domains SET status = 'Active' WHERE id = ?", (domain_id,))
            conn.commit()
            conn.close()
            continue
            
        res = verify_domain_ssl_core(domain, email)
        if "status" in res and res["status"] == "success":
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE system_domains SET status = 'Active' WHERE id = ?", (domain_id,))
            conn.commit()
            conn.close()
            
    for row in tenant_rows:
        tenant_id = row['id']
        domain = row['custom_domain'].strip().lower()
        email = row['custom_domain_email'].strip().lower()
        
        if domain in BASE_DOMAINS:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE tenants SET custom_domain_verified = 1 WHERE id = ?", (tenant_id,))
            conn.commit()
            conn.close()
            continue
            
        res = verify_domain_ssl_core(domain, email)
        if "status" in res and res["status"] == "success":
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE tenants SET custom_domain_verified = 1 WHERE id = ?", (tenant_id,))
            conn.commit()
            conn.close()

@app.route('/api/admin/system-domains', methods=['GET', 'POST'])
def admin_system_domains_endpoint():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    if request.method == 'POST':
        data = request.json or {}
        domain = data.get('domain', '').strip().lower()
        email = data.get('email', '').strip().lower()
        
        if not domain or not email:
            conn.close()
            return jsonify({"error": "Domain and Email are required"}), 400
            
        c = conn.cursor()
        c.execute('SELECT id FROM system_domains WHERE domain = ?', (domain,))
        if c.fetchone():
            conn.close()
            return jsonify({"error": "Domain already exists"}), 400
            
        # Insert domain as Pending
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        c.execute('INSERT INTO system_domains (domain, email, created_at, status) VALUES (?, ?, ?, ?)',
                  (domain, email, today_str, 'Pending Verification'))
        conn.commit()
        domain_id = c.lastrowid
        conn.close()
        
        # Run SSL certification using the core helper
        res = verify_domain_ssl_core(domain, email)
        if "status" in res and res["status"] == "success":
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE system_domains SET status = 'Active' WHERE id = ?", (domain_id,))
            conn.commit()
            conn.close()
            return jsonify({"status": "success"})
        else:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM system_domains WHERE id = ?", (domain_id,))
            conn.commit()
            conn.close()
            return jsonify({"error": res.get("error", "SSL verification failed")}), 400
            
    # GET method
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM system_domains ORDER BY id DESC')
    domains = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(domains)

@app.route('/api/admin/system-domains/verify-all', methods=['POST'])
def admin_verify_all_domains_endpoint():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    
    import threading
    thread = threading.Thread(target=auto_verify_all_domains)
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "success", "message": "Auto-verification of all domains started in the background."})

@app.route('/api/admin/system-domains/<int:domain_id>/verify', methods=['POST'])
def admin_verify_system_domain(domain_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT domain, email FROM system_domains WHERE id = ?', (domain_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Domain not found"}), 404
        
    domain, email = row
    conn.close()
    
    res = verify_domain_ssl_core(domain, email)
    if "status" in res and res["status"] == "success":
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE system_domains SET status = 'Active' WHERE id = ?", (domain_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    return jsonify(res), 400

@app.route('/api/admin/system-domains/<int:domain_id>', methods=['DELETE'])
def admin_delete_system_domain(domain_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT domain FROM system_domains WHERE id = ?', (domain_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Domain not found"}), 404
        
    domain = row[0]
    
    # Don't allow deleting base domains if they are hardcoded
    if domain in ['echo.oqens.me', 'host.echo.oqens.me', 'payments.oqens.me']:
        c.execute('DELETE FROM system_domains WHERE id = ?', (domain_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Removed tracking only. Core configuration file preserved."})
        
    import subprocess
    subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
    subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
    
    c.execute('DELETE FROM system_domains WHERE id = ?', (domain_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/tenants', methods=['GET', 'POST'])
def admin_tenants():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if request.method == 'POST':
        data = request.json
        username = data.get('username', '').strip()
        email = data.get('email', '').strip().lower()
        if not username or not email:
            conn.close()
            return jsonify({"error": "Username and Email are required"}), 400
            
        c.execute('SELECT id FROM tenants WHERE username = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (username,))
        if c.fetchone():
            conn.close()
            return jsonify({"error": "Username already exists"}), 400
            
        c.execute('SELECT id FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
        if c.fetchone():
            conn.close()
            return jsonify({"error": "Email is already registered"}), 400
            
        # Generate unique cloud ID
        while True:
            cid = generate_cloud_id()
            c.execute('SELECT id FROM tenants WHERE cloud_id = ?', (cid,))
            if not c.fetchone():
                break
                
        bandwidth_limit_val = data.get('bandwidth_limit_value')
        bandwidth_limit_unit = data.get('bandwidth_limit_unit', 'GB')
        if bandwidth_limit_val is None:
            bandwidth_limit_val = data.get('bandwidth_limit_gb', 0)
            bandwidth_limit_unit = 'GB'
            
        try:
            val = float(bandwidth_limit_val)
            if bandwidth_limit_unit == 'MB':
                bandwidth_limit_bytes = int(val * 1024**2)
            else:
                bandwidth_limit_bytes = int(val * 1024**3)
        except (TypeError, ValueError):
            bandwidth_limit_bytes = 0

        storage_limit_val = data.get('storage_limit_value')
        storage_limit_unit = data.get('storage_limit_unit', 'GB')
        if storage_limit_val is None:
            storage_limit_val = data.get('storage_limit_gb', 0)
            storage_limit_unit = 'GB'
            
        try:
            s_val = float(storage_limit_val)
            if storage_limit_unit == 'MB':
                storage_limit_bytes = int(s_val * 1024**2)
            else:
                storage_limit_bytes = int(s_val * 1024**3)
        except (TypeError, ValueError):
            storage_limit_bytes = 0
            
        email = data.get('email', '').strip().lower()
        block_downloads = int(data.get('block_downloads', 0))
        block_uploads = int(data.get('block_uploads', 0))
        
        monthly_rate = float(data.get('monthly_rate', 0.0))
        auto_renew = int(data.get('auto_renew', 1))
        next_billing_date = data.get('next_billing_date')
        if not next_billing_date:
            next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        billing_status = data.get('billing_status', 'Active')

        c.execute('''INSERT INTO tenants (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, cloud_id, custom_domain_email, block_downloads, block_uploads, monthly_rate, auto_renew, next_billing_date, billing_status) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                  (data['username'], data['secret_code'], storage_limit_bytes, bandwidth_limit_bytes, cid, email, block_downloads, block_uploads, monthly_rate, auto_renew, next_billing_date, billing_status))
        conn.commit()
        
        # Create tenant storage directory immediately
        tenant_dir = os.path.join(STORAGE_BASE_DIR, data['username'])
        os.makedirs(tenant_dir, exist_ok=True)
        
        conn.close()
        return jsonify({"status": "success"})
        
    c.execute('''
        SELECT t.*, r.referral_code
        FROM tenants t
        LEFT JOIN referral_uses r ON t.id = r.tenant_id
    ''')
    tenants = [dict(r) for r in c.fetchall()]
    conn.close()
    
    # Recalculate usage for all tenants dynamically when listed by admin
    for tenant in tenants:
        tenant.pop('password', None)
        tenant.pop('password_hash', None)
        tenant['storage_used_bytes'] = recalculate_tenant_usage(tenant['id'], tenant['username'])
        
    return jsonify(tenants)

@app.route('/api/admin/tenants/<int:tenant_id>', methods=['DELETE'])
def admin_delete_tenant(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
        
    username = tenant['username']
    email = tenant['custom_domain_email']
    new_username = f"{username}_deleted_{tenant_id}"
    new_email = f"{email}_deleted_{tenant_id}"
    domain = tenant.get('custom_domain')
    
    # Clean up Nginx custom domain config if exists
    if domain:
        try:
            subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
        except Exception:
            pass
    
    # Rename tenant directory
    tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
    new_dir = os.path.join(STORAGE_BASE_DIR, new_username)
    if os.path.exists(tenant_dir):
        try:
            os.rename(tenant_dir, new_dir)
        except Exception:
            pass
            
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET username = ?, custom_domain_email = ?, is_deleted = 1, billing_status = \'Deleted\', custom_domain = NULL, custom_domain_verified = 0, storage_limit_bytes = 0, bandwidth_limit_bytes = 0, tier = \'Free\' WHERE id = ?', 
              (new_username, new_email, tenant_id))
              
    # Update related tables that use tenant_username
    tables_to_update = ['device_views', 'file_tags', 'markdown_pages', 'referral_uses', 'markdown_collections', 'photos', 'photo_albums']
    for t_table in tables_to_update:
        try:
            c.execute(f"UPDATE {t_table} SET tenant_username = ? WHERE tenant_username = ?", (new_username, username))
        except Exception:
            pass
            
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/tenants/<int:tenant_id>/edit', methods=['POST'])
def admin_edit_tenant(tenant_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    data = request.json
    
    username = data.get('username')
    secret_code = data.get('secret_code')
    email = data.get('email', '').strip().lower()
    
    storage_limit_bytes = data.get('storage_limit_bytes')
    if storage_limit_bytes is None:
        storage_limit_val = data.get('storage_limit_value')
        storage_limit_unit = data.get('storage_limit_unit', 'GB')
        if storage_limit_val is None:
            storage_limit_val = data.get('storage_limit_gb')
            storage_limit_unit = 'GB'
            
        if storage_limit_val is not None:
            try:
                s_val = float(storage_limit_val)
                if storage_limit_unit == 'MB':
                    storage_limit_bytes = int(s_val * 1024**2)
                else:
                    storage_limit_bytes = int(s_val * 1024**3)
            except (TypeError, ValueError):
                storage_limit_bytes = 0
        else:
            storage_limit_bytes = None

    bandwidth_limit_bytes = data.get('bandwidth_limit_bytes')
    if bandwidth_limit_bytes is None:
        bandwidth_limit_val = data.get('bandwidth_limit_value')
        bandwidth_limit_unit = data.get('bandwidth_limit_unit', 'GB')
        if bandwidth_limit_val is None:
            bandwidth_limit_val = data.get('bandwidth_limit_gb', 0)
            bandwidth_limit_unit = 'GB'
            
        try:
            val = float(bandwidth_limit_val)
            if bandwidth_limit_unit == 'MB':
                bandwidth_limit_bytes = int(val * 1024**2)
            else:
                bandwidth_limit_bytes = int(val * 1024**3)
        except (TypeError, ValueError):
            bandwidth_limit_bytes = 0
            
    if not username or not secret_code or storage_limit_bytes is None:
        return jsonify({"error": "Missing fields"}), 400
        
    try:
        # Get old username to rename folder if needed
        c.execute('SELECT username FROM tenants WHERE id = ?', (tenant_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Tenant not found"}), 404
            
        old_username = row[0]
        
        block_downloads = int(data.get('block_downloads', 0))
        block_uploads = int(data.get('block_uploads', 0))
        papers_enabled = bool(int(data.get('papers_enabled', 0)))
        photos_enabled = bool(int(data.get('photos_enabled', 0)))
        
        monthly_rate = float(data.get('monthly_rate', 0.0))
        auto_renew = int(data.get('auto_renew', 1))
        next_billing_date = data.get('next_billing_date')
        if not next_billing_date:
            next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        billing_status = data.get('billing_status', 'Active')
        cloud_id = data.get('cloud_id', '')
        custom_domain = data.get('custom_domain', '')
        password = data.get('password', '')
        email_verified = int(data.get('email_verified', 0))
        custom_domain_verified = int(data.get('custom_domain_verified', 0))
        custom_message = data.get('custom_message', '')
        custom_message_theme = data.get('custom_message_theme', 'danger')
        custom_message_icon = data.get('custom_message_icon', 'ph-warning-circle')
        custom_message_active = int(data.get('custom_message_active', 0))

        # Update details
        if password:
            c.execute('''UPDATE tenants 
                         SET username = ?, secret_code = ?, storage_limit_bytes = ?, bandwidth_limit_bytes = ?, custom_domain_email = ?, block_downloads = ?, block_uploads = ?, monthly_rate = ?, auto_renew = ?, next_billing_date = ?, billing_status = ?, cloud_id = ?, custom_domain = ?, password = ?, email_verified = ?, custom_domain_verified = ?, custom_message = ?, custom_message_theme = ?, custom_message_icon = ?, custom_message_active = ?, papers_enabled = ?, photos_enabled = ?
                         WHERE id = ?''',
                      (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, email, block_downloads, block_uploads, monthly_rate, auto_renew, next_billing_date, billing_status, cloud_id, custom_domain, password, email_verified, custom_domain_verified, custom_message, custom_message_theme, custom_message_icon, custom_message_active, papers_enabled, photos_enabled, tenant_id))
        else:
            c.execute('''UPDATE tenants 
                         SET username = ?, secret_code = ?, storage_limit_bytes = ?, bandwidth_limit_bytes = ?, custom_domain_email = ?, block_downloads = ?, block_uploads = ?, monthly_rate = ?, auto_renew = ?, next_billing_date = ?, billing_status = ?, cloud_id = ?, custom_domain = ?, email_verified = ?, custom_domain_verified = ?, custom_message = ?, custom_message_theme = ?, custom_message_icon = ?, custom_message_active = ?, papers_enabled = ?, photos_enabled = ?
                         WHERE id = ?''',
                      (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, email, block_downloads, block_uploads, monthly_rate, auto_renew, next_billing_date, billing_status, cloud_id, custom_domain, email_verified, custom_domain_verified, custom_message, custom_message_theme, custom_message_icon, custom_message_active, papers_enabled, photos_enabled, tenant_id))

        
        # Rename storage directory if username changed
        if old_username != username:
            old_dir = os.path.join(STORAGE_BASE_DIR, old_username)
            new_dir = os.path.join(STORAGE_BASE_DIR, username)
            if os.path.exists(old_dir):
                os.rename(old_dir, new_dir)
            else:
                os.makedirs(new_dir, exist_ok=True)
                
            # Also update username in device_views table!
            c.execute('UPDATE device_views SET tenant_username = ? WHERE tenant_username = ?', (username, old_username))
            
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({"error": "Username or Secret Code already exists"}), 400
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/tenants/<int:tenant_id>/terminate', methods=['POST'])
def admin_terminate_tenant_service(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, username, custom_domain_email, cloud_id FROM tenants WHERE id = ?', (tenant_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Tenant not found"}), 404
        
    tenant = dict(row)
    
    # Update status to Suspended and clear resource limits
    import datetime
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    c.execute('UPDATE tenants SET billing_status = \'Suspended\', storage_limit_bytes = 0, bandwidth_limit_bytes = 0, tier = \'Free\' WHERE id = ?', (tenant_id,))
    c.execute('''
        INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email)
        VALUES (?, 0.0, ?, 'Cancelled', 'Subscription terminated manually by administrator.', 'admin_terminate', 'N/A', '')
    ''', (tenant_id, today_str))
    
    conn.commit()
    conn.close()
    
    # Send email notification
    try:
        tenant_email = tenant.get('custom_domain_email') or tenant.get('email')
        if tenant_email:
            subject = "OQENS Workspace Suspended: Service Terminated by Administrator"
            html_body = f"""
            <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px; color: #ef4444;">Workspace Suspended</h2>
                <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {tenant['username']},</p>
                <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Your OQENS workspace (Cloud ID: <strong>{tenant['cloud_id']}</strong>) has been suspended immediately by the administrator.</p>
                <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">As a result of this action, file access and uploads have been disabled. If you believe this is an error or would like to reactivate your subscription, please contact support.</p>
                <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This is an automated notification from OQENS Billing.</p>
            </div>
            """
            send_system_email(tenant_email, subject, html_body)
    except Exception as e:
        print("Manual suspension email failed:", e)
        
    return jsonify({"status": "success", "message": "Service terminated successfully."})

@app.route('/api/admin/tenants/<int:tenant_id>/reactivate', methods=['POST'])
def admin_reactivate_tenant_service(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, username, custom_domain_email, cloud_id FROM tenants WHERE id = ?', (tenant_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Tenant not found"}), 404
        
    tenant = dict(row)
    
    # Update status to Active
    import datetime
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    c.execute('UPDATE tenants SET billing_status = \'Active\' WHERE id = ?', (tenant_id,))
    c.execute('''
        INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email)
        VALUES (?, 0.0, ?, 'Success', 'Subscription reactivated manually by administrator.', 'admin_reactivate', 'N/A', '')
    ''', (tenant_id, today_str))
    
    conn.commit()
    conn.close()
    
    # Send email notification
    try:
        tenant_email = tenant.get('custom_domain_email') or tenant.get('email')
        if tenant_email:
            subject = "OQENS Workspace Reactivated"
            html_body = f"""
            <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px; color: #2ecc71;">Workspace Reactivated</h2>
                <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {tenant['username']},</p>
                <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Your OQENS workspace (Cloud ID: <strong>{tenant['cloud_id']}</strong>) has been reactivated by the administrator.</p>
                <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">You can now log in and access your files normally.</p>
                <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This is an automated notification from OQENS Support.</p>
            </div>
            """
            send_system_email(tenant_email, subject, html_body)
    except Exception as e:
        print("Manual reactivation email failed:", e)
        
    return jsonify({"status": "success", "message": "Service reactivated successfully."})

@app.route('/api/admin/tenants/<int:tenant_id>/send-password-reset', methods=['POST'])
def admin_send_password_reset(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
        
    import secrets
    token = secrets.token_hex(20)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET password_setup_token = ? WHERE id = ?', (token, tenant_id))
    conn.commit()
    conn.close()
    
    setup_link = f"https://auth.oqens.me/setup-password?token={token}"
    email = tenant.get('custom_domain_email') or tenant.get('email')
    
    subject = "Reset your OQENS Account Password"
    html_body = f"""
    <h3>Account Recovery</h3>
    <p>Hello {tenant['username']},</p>
    <p>Your administrator has requested a password reset for your account.</p>
    <p>Please click the link below to configure a new password for your account:</p>
    <p><a href="{setup_link}" style="display:inline-block; padding:10px 20px; background:#111; color:#fff; text-decoration:none; border-radius:5px;">Reset Password</a></p>
    <p>If you did not request this, please ignore this email.</p>
    """
    if send_system_email(email, subject, html_body):
        return jsonify({"status": "success", "message": "Password reset email sent."})
    return jsonify({"error": "Failed to send email."}), 500

@app.route('/api/admin/tenants/<int:tenant_id>/send-verification', methods=['POST'])
def admin_send_verification(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
        
    import secrets
    token = secrets.token_hex(20)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET email_verification_token = ? WHERE id = ?', (token, tenant_id))
    conn.commit()
    conn.close()
    
    verify_link = f"https://payments.oqens.me/verify-email?token={token}"
    email = tenant.get('custom_domain_email') or tenant.get('email')
    
    subject = "Verify your OQENS Email"
    html_body = f"""
    <h3>Email Verification</h3>
    <p>Hello {tenant['username']},</p>
    <p>Your administrator has requested that you verify your email address.</p>
    <p>Please click the link below to verify your email:</p>
    <p><a href="{verify_link}" style="display:inline-block; padding:10px 20px; background:#111; color:#fff; text-decoration:none; border-radius:5px;">Verify Email</a></p>
    """
    if send_system_email(email, subject, html_body):
        return jsonify({"status": "success", "message": "Verification email sent."})
    return jsonify({"error": "Failed to send email."}), 500

@app.route('/api/admin/tenants/<int:tenant_id>/regenerate-api-key', methods=['POST'])
def admin_regenerate_api_key(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    import secrets
    new_api_key = f"oq_{secrets.token_hex(16)}"
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET api_key = ? WHERE id = ?', (new_api_key, tenant_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "API Key regenerated.", "api_key": new_api_key})
@app.route('/api/admin/tenants/<int:tenant_id>/files')
def admin_tenant_files(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({"error": "Tenant not found"}), 404
        
    tenant_dir = os.path.join(STORAGE_BASE_DIR, tenant['username'])
    try:
        files = []
        if os.path.exists(tenant_dir):
            for filename in os.listdir(tenant_dir):
                full_path = os.path.join(tenant_dir, filename)
                if os.path.isfile(full_path):
                    stat = os.stat(full_path)
                    files.append({
                        "key": filename,
                        "size": stat.st_size,
                        "last_modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
                    })
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Tenant Storage APIs ---

@app.route('/api/bucket/list')
def list_files():
    if not session.get('role') in ['admin', 'tenant']:
        return jsonify({"error": "Unauthorized"}), 401
        
    tenant_dir = get_tenant_storage_dir()
    tenant_username = session.get('tenant_username') if session.get('role') == 'tenant' else 'admin_files'
    
    # Query database for all tags for this tenant_username
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT filename, tag FROM file_tags WHERE tenant_username = ?', (tenant_username,))
    tag_rows = c.fetchall()
    conn.close()
    
    file_tags_map = {}
    for filename, tag in tag_rows:
        if filename not in file_tags_map:
            file_tags_map[filename] = []
        file_tags_map[filename].append(tag)
        
    try:
        files = []
        added_keys = set()

        # 1. Fetch virtual .paper files from database for active tenant
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT slug, title, content, created_at, updated_at FROM markdown_pages WHERE tenant_username = ?', (tenant_username,))
        pages = [dict(r) for r in c.fetchall()]
        conn.close()

        for p in pages:
            filename = f"{p['slug']}.paper"
            added_keys.add(filename)
            content = p['content'] or ''
            size_bytes = len(content.encode('utf-8'))
            
            # Format datetime
            last_modified = p['updated_at'] if p['updated_at'] else (p['created_at'] if p['created_at'] else datetime.datetime.utcnow().isoformat())
            if ' ' in last_modified and 'T' not in last_modified:
                last_modified = last_modified.replace(' ', 'T')
                
            files.append({
                "key": filename,
                "raw_key": filename,
                "size": size_bytes,
                "last_modified": last_modified,
                "tags": file_tags_map.get(filename, [])
            })

        # 2. Fetch actual files from local storage
        if os.path.exists(tenant_dir):
            for filename in os.listdir(tenant_dir):
                if filename in added_keys:
                    continue
                full_path = os.path.join(tenant_dir, filename)
                if os.path.isfile(full_path):
                    stat = os.stat(full_path)
                    files.append({
                        "key": filename,
                        "raw_key": filename,
                        "size": stat.st_size,
                        "last_modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "tags": file_tags_map.get(filename, [])
                    })
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bucket/upload', methods=['POST'])
def upload_file():
    if not session.get('role') in ['admin', 'tenant']:
        return jsonify({"error": "Unauthorized"}), 401
        
    if session.get('role') == 'tenant':
        tenant = get_tenant(session['tenant_id'])
        if not tenant:
            return jsonify({"error": "Tenant not found"}), 400
        if tenant.get('billing_status') == 'Suspended':
            return jsonify({"error": "Subscription Suspended. Please contact support or your administrator."}), 403
        if tenant.get('block_uploads') == 1:
            return jsonify({"error": "Uploads are disabled by the administrator"}), 403
            
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
        
    # Check Quota
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    if session.get('role') == 'tenant':
        if is_bandwidth_exhausted(tenant):
            return jsonify({"error": "Bandwidth limit exceeded. Contact administrator for more info."}), 400
        # Use cached DB usage for instant quota check
        if tenant['storage_used_bytes'] + file_size > tenant['storage_limit_bytes']:
            return jsonify({"error": "Storage limit exceeded. Contact administrator for more info."}), 400
            
    tenant_dir = get_tenant_storage_dir()
    filename = os.path.basename(file.filename)
    dest_path = os.path.join(tenant_dir, filename)
    
    try:
        file.save(dest_path)
        if session.get('role') == 'tenant':
            recalculate_tenant_usage(tenant['id'], tenant['username'])
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/bucket/delete', methods=['DELETE'])
def delete_file():
    if not session.get('role') in ['admin', 'tenant']:
        return jsonify({"error": "Unauthorized"}), 401
        
    key = request.json.get('key') # This is filename
    if not key:
        return jsonify({"error": "Invalid file key"}), 400
        
    filename = os.path.basename(key)
    tenant_dir = get_tenant_storage_dir()
    file_path = os.path.join(tenant_dir, filename)
    tenant_username = session.get('tenant_username') if session.get('role') == 'tenant' else 'admin_files'

    # Intercept virtual .paper deletes
    if filename.endswith('.paper'):
        slug = filename[:-6]
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('DELETE FROM markdown_pages WHERE tenant_username = ? AND slug = ?', (tenant_username, slug))
        c.execute('DELETE FROM file_tags WHERE tenant_username = ? AND filename = ?', (tenant_username, filename))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM file_tags WHERE tenant_username = ? AND filename = ?', (tenant_username, filename))
    # Also remove from photos table if it exists there
    c.execute('DELETE FROM photos WHERE tenant_username = ? AND filename = ?', (tenant_username, filename))
    # Remove from any albums it was added to (prevents ghost image slots in album view)
    c.execute('DELETE FROM album_photos WHERE filename = ?', (filename,))
    conn.commit()
    conn.close()
    
    if not os.path.exists(file_path):
        # File is already gone from disk, but we cleared DB. Return success so UI updates.
        return jsonify({"status": "success"})
        
    try:
        os.remove(file_path)

        if session.get('role') == 'tenant':
            tenant_id = session.get('tenant_id')
            recalculate_tenant_usage(tenant_id, tenant_username)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def is_safe_url(url):
    import urllib.parse
    import socket
    import ipaddress
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        ips = []
        try:
            addrinfo = socket.getaddrinfo(hostname, None)
            for item in addrinfo:
                ips.append(item[4][0])
        except Exception:
            return False
        if not ips:
            return False
        for ip_str in ips:
            try:
                if '%' in ip_str:
                    ip_str = ip_str.split('%')[0]
                ip = ipaddress.ip_address(ip_str)
                if (ip.is_loopback or 
                    ip.is_private or 
                    ip.is_link_local or 
                    ip.is_reserved or 
                    ip.is_multicast or
                    ip_str == '0.0.0.0' or
                    ip_str == '::'):
                    return False
            except ValueError:
                return False
        return True
    except Exception:
        return False


@app.route('/api/bucket/fetch-url', methods=['POST'])
def bucket_fetch_url():
    if session.get('role') not in ['admin', 'tenant']:
        return jsonify({'error': 'Unauthorized'}), 401
        
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    username = tenant['username']
    
    if tenant.get('billing_status') == 'Suspended':
        return jsonify({"error": "Subscription Suspended. Please contact support or your administrator."}), 403
    if tenant.get('block_uploads') == 1:
        return jsonify({"error": "Uploads are disabled by the administrator"}), 403
        
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'Missing url'}), 400
        
    if not is_safe_url(url):
        return jsonify({'error': 'Invalid or forbidden URL'}), 400
        
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
            content_type = response.headers.get('Content-Type', 'application/octet-stream')
            cd = response.headers.get('Content-Disposition', '')
            import re
            filename_match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
            if filename_match:
                filename = secure_filename(filename_match.group(1))
            else:
                filename = secure_filename(url.split('?')[0].split('/')[-1])
                
        if not filename or '.' not in filename:
            import mimetypes
            ext = mimetypes.guess_extension(content_type) or '.bin'
            filename = f"downloaded_file{ext}"
            
        if len(filename) > 200:
            name_part, ext_part = os.path.splitext(filename)
            filename = name_part[:190] + ext_part
            
        # Check Quota
        if is_bandwidth_exhausted(tenant):
            return jsonify({"error": "Bandwidth limit exceeded. Contact administrator for more info."}), 400
        if tenant['storage_used_bytes'] + len(content) > tenant['storage_limit_bytes']:
            return jsonify({"error": "Storage limit exceeded. Contact administrator for more info."}), 400
            
        tenant_dir = get_tenant_storage_dir()
        dest_path = os.path.join(tenant_dir, filename)
        
        with open(dest_path, 'wb') as dst:
            dst.write(content)
            
        recalculate_tenant_usage(tenant_id, username)
        return jsonify({'status': 'success', 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/bucket/tags', methods=['POST'])
def add_file_tag():
    if not session.get('role') in ['admin', 'tenant']:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json or {}
    key = data.get('key')
    tag = data.get('tag')
    
    if not key or not tag:
        return jsonify({"error": "File key and tag required"}), 400
        
    filename = os.path.basename(key)
    tag = tag.strip()
    if not tag:
        return jsonify({"error": "Invalid tag"}), 400
        
    tenant_username = session.get('tenant_username') if session.get('role') == 'tenant' else 'admin_files'
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO file_tags (tenant_username, filename, tag) VALUES (?, ?, ?)', 
                  (tenant_username, filename, tag))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
        
    # Get updated list of tags for this file
    c.execute('SELECT tag FROM file_tags WHERE tenant_username = ? AND filename = ?', (tenant_username, filename))
    tags = [r[0] for r in c.fetchall()]
    conn.close()
    
    return jsonify({"status": "success", "tags": tags})

@app.route('/api/bucket/tags/delete', methods=['POST'])
def delete_file_tag():
    if not session.get('role') in ['admin', 'tenant']:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json or {}
    key = data.get('key')
    tag = data.get('tag')
    
    if not key or not tag:
        return jsonify({"error": "File key and tag required"}), 400
        
    filename = os.path.basename(key)
    tenant_username = session.get('tenant_username') if session.get('role') == 'tenant' else 'admin_files'
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM file_tags WHERE tenant_username = ? AND filename = ? AND tag = ?', 
                  (tenant_username, filename, tag))
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
        
    # Get updated list of tags for this file
    c.execute('SELECT tag FROM file_tags WHERE tenant_username = ? AND filename = ?', (tenant_username, filename))
    tags = [r[0] for r in c.fetchall()]
    conn.close()
    
    return jsonify({"status": "success", "tags": tags})

@app.route('/api/bucket/download')
def download_file():
    key = request.args.get('key')
    if not key:
        return jsonify({"error": "Invalid file key"}), 400
        
    filename = os.path.basename(key)
    share_token = request.args.get('share_token')
    
    tenant_username = None
    is_shared = False
    
    if share_token:
        # Check if the requested file belongs to an album shared via this token
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT a.tenant_username FROM shared_albums s
            JOIN photo_albums a ON s.album_id = a.id
            JOIN album_photos ap ON a.id = ap.album_id
            WHERE s.share_token = ? AND ap.filename = ?
        ''', (share_token, filename))
        row = c.fetchone()
        conn.close()
        if row:
            is_shared = True
            tenant_username = row[0]

    if not is_shared and not session.get('role') in ['admin', 'tenant']:
        if 'text/html' in request.headers.get('Accept', ''):
            return """
            <html><head><title>Unauthorized</title>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
            <style>
                body { background: #f8fafc; color: #111; font-family: 'Inter', sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                .container { text-align: center; padding: 40px; background: #fff; border-radius: 12px; border: 1px solid #eee; box-shadow: 0 10px 30px rgba(0,0,0,0.05); }
                h2 { margin-bottom: 20px; font-weight: 500; }
                a { display: inline-block; padding: 10px 20px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 500; transition: background 0.2s; }
                a:hover { background: #333; }
            </style>
            </head><body>
            <div class="container">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-bottom: 15px; color: #666;"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                <h2>Please login to view photo</h2>
                <a href="https://echo.oqens.me/login">Go to Login</a>
            </div>
            </body></html>
            """, 401
        return jsonify({"error": "Unauthorized"}), 401
        
    if not tenant_username:
        tenant_username = session.get('tenant_username') if session.get('role') == 'tenant' else 'admin_files'
        
    tenant_dir = os.path.join(STORAGE_BASE_DIR, tenant_username)

    # Intercept virtual .paper downloads
    if filename.endswith('.paper'):
        slug = filename[:-6]
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT title, content FROM markdown_pages WHERE tenant_username = ? AND slug = ?', (tenant_username, slug))
        page = c.fetchone()
        conn.close()
        if not page:
            return "Paper not found", 404
            
        from flask import Response
        response = Response(page['content'] or '', mimetype='text/markdown')
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'
        return response
    
    # Send as attachment if explicitly requested
    as_attachment = request.args.get('attachment', 'false').lower() == 'true'
    
    # Check if this is a preview request (thumbnails for grid views or lightbox)
    preview = request.args.get('preview')
    target_dir = tenant_dir
    target_filename = filename
    
    if preview and filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        if preview in ['1', 'true', 'thumbnail']:
            # Small Thumbnail: 300x300
            thumb_sub = '.thumbnails'
            max_size = (300, 300)
        else:
            # Medium Preview: 1200x1200
            thumb_sub = '.thumbnails_medium'
            max_size = (1200, 1200)
            
        thumb_dir = os.path.join(tenant_dir, thumb_sub)
        os.makedirs(thumb_dir, exist_ok=True)
        thumb_path = os.path.join(thumb_dir, filename)
        full_path = os.path.join(tenant_dir, filename)
        
        if os.path.exists(full_path):
            if not os.path.exists(thumb_path):
                try:
                    from PIL import Image
                    with Image.open(full_path) as img:
                        img.thumbnail(max_size)
                        if img.mode in ('RGBA', 'LA', 'P'):
                            img = img.convert('RGB')
                        img.save(thumb_path, 'JPEG', quality=80)
                except Exception as e:
                    pass
            
            if os.path.exists(thumb_path):
                target_dir = thumb_dir
                target_filename = filename

    # Increment download count and bandwidth if logged in as tenant
    if session.get('role') == 'tenant' and session.get('tenant_id'):
        tenant = get_tenant(session['tenant_id'])
        if not tenant:
            return jsonify({"error": "Tenant not found"}), 400
        if tenant.get('block_downloads') == 1:
            return jsonify({"error": "Downloads are disabled by the administrator"}), 403
        if is_bandwidth_exhausted(tenant):
            return render_bandwidth_error()
            
        target_path = os.path.join(target_dir, target_filename)
        if os.path.exists(target_path):
            file_size = os.path.getsize(target_path)
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''UPDATE tenants 
                         SET downloads_count = downloads_count + 1, 
                             bandwidth_bytes = bandwidth_bytes + ? 
                         WHERE id = ?''', (file_size, session['tenant_id']))
            conn.commit()
            conn.close()
            
    content_type, _ = mimetypes.guess_type(target_filename)
    if not content_type:
        content_type = 'application/octet-stream'
    response = make_response("")
    response.headers['Content-Type'] = content_type
    response.headers['X-Accel-Redirect'] = f'/protected_files/{tenant_username}/{target_filename}'
    if as_attachment:
        response.headers['Content-Disposition'] = f'attachment; filename="{target_filename}"'
    if preview:
        response.headers['Cache-Control'] = 'public, max-age=604800'  # Cache thumbnails for 7 days
    else:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@app.route('/api/tenant/custom-domain', methods=['POST'])
def set_custom_domain():
    if session.get('role') != 'tenant' or not session.get('tenant_id'):
        return jsonify({"error": "Unauthorized"}), 401
        
    tenant = get_tenant(session['tenant_id'])
    if tenant and tenant.get('monthly_rate', 0.0) == 0.0:
        return jsonify({"error": "Custom domains are not available on the Free Plan. Please upgrade to the Starter Plan to map your custom domain."}), 400
        
    data = request.json
    domain = data.get('domain', '').strip().lower()
    email = data.get('email', '').strip()
    
    if not domain or not email:
        return jsonify({"error": "Domain and Email are required"}), 400
        
    if '.' not in domain or '@' not in email:
        return jsonify({"error": "Invalid Domain or Email address"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Ensure domain is not already registered by another tenant
    c.execute('SELECT id FROM tenants WHERE custom_domain = ? AND id != ? AND (is_deleted = 0 OR is_deleted IS NULL)', (domain, session['tenant_id']))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Domain already registered by another tenant"}), 400
        
    c.execute('''UPDATE tenants 
                 SET custom_domain = ?, custom_domain_email = ?, custom_domain_verified = 0 
                 WHERE id = ?''', (domain, email, session['tenant_id']))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/tenant/custom-domain', methods=['DELETE'])
def delete_custom_domain():
    if session.get('role') != 'tenant' or not session.get('tenant_id'):
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT custom_domain FROM tenants WHERE id = ?', (session['tenant_id'],))
    tenant = c.fetchone()
    
    if tenant and tenant['custom_domain']:
        domain = tenant['custom_domain']
        import subprocess
        try:
            subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
        except Exception:
            pass
            
    c.execute('''UPDATE tenants 
                 SET custom_domain = NULL, custom_domain_email = NULL, custom_domain_verified = 0 
                 WHERE id = ?''', (session['tenant_id'],))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/custom-domains', methods=['GET', 'POST'])
def admin_custom_domains():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    if request.method == 'POST':
        data = request.json or {}
        tenant_id = data.get('tenant_id')
        domain = data.get('domain', '').strip().lower()
        email = data.get('email', '').strip().lower()
        
        if not tenant_id or not domain or not email:
            conn.close()
            return jsonify({"error": "Tenant ID, Domain, and Email are required"}), 400
            
        c = conn.cursor()
        
        c.execute('SELECT id FROM tenants WHERE id = ?', (tenant_id,))
        if not c.fetchone():
            conn.close()
            return jsonify({"error": "Tenant not found"}), 404
            
        c.execute('SELECT id FROM tenants WHERE custom_domain = ? AND id != ? AND (is_deleted = 0 OR is_deleted IS NULL)', (domain, tenant_id))
        if c.fetchone():
            conn.close()
            return jsonify({"error": "This domain is already mapped to another tenant"}), 400
            
        c.execute('''UPDATE tenants 
                     SET custom_domain = ?, custom_domain_email = ?, custom_domain_verified = 0 
                     WHERE id = ?''', (domain, email, tenant_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
        
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, username, custom_domain, custom_domain_email, custom_domain_verified FROM tenants WHERE custom_domain IS NOT NULL AND custom_domain != '' AND (is_deleted = 0 OR is_deleted IS NULL)")
    rows = c.fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

def verify_domain_ssl_internal(tenant_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,))
    tenant = c.fetchone()
    conn.close()
    
    if not tenant or not tenant['custom_domain'] or not tenant['custom_domain_email']:
        return {"error": "No custom domain requested for this tenant"}
        
    domain = tenant['custom_domain']
    email = tenant['custom_domain_email']
    
    res = verify_domain_ssl_core(domain, email)
    if "status" in res and res["status"] == "success":
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('UPDATE tenants SET custom_domain_verified = 1 WHERE id = ?', (tenant_id,))
        conn.commit()
        conn.close()
        return {"status": "success"}
    return res

@app.route('/api/tenant/custom-domain/verify', methods=['POST'])
def tenant_verify_custom_domain():
    if session.get('role') != 'tenant' or not session.get('tenant_id'):
        return jsonify({"error": "Unauthorized"}), 401
        
    res = verify_domain_ssl_internal(session['tenant_id'])
    if "error" in res:
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/admin/custom-domains/<int:tenant_id>/reset', methods=['POST'])
def admin_reset_custom_domain(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM tenants WHERE id = ?', (tenant_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({"error": "Tenant not found"}), 404
    c.execute('UPDATE tenants SET custom_domain_verified = 0 WHERE id = ?', (tenant_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/custom-domains/<int:tenant_id>/verify', methods=['POST'])
def admin_verify_custom_domain(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    res = verify_domain_ssl_internal(tenant_id)
    if "error" in res:
        return jsonify(res), 400
    return jsonify(res)

@app.route('/api/admin/custom-domains/<int:tenant_id>', methods=['DELETE'])
def admin_delete_custom_domain(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT custom_domain FROM tenants WHERE id = ?', (tenant_id,))
    tenant = c.fetchone()
    
    if tenant and tenant['custom_domain']:
        domain = tenant['custom_domain']
        import subprocess
        try:
            subprocess.run(["sudo", "rm", "-f", f"/etc/nginx/sites-available/{domain}", f"/etc/nginx/sites-enabled/{domain}"], check=False)
            subprocess.run(["sudo", "systemctl", "reload", "nginx"], check=False)
        except Exception:
            pass
            
    c.execute('''UPDATE tenants 
                 SET custom_domain = NULL, custom_domain_email = NULL, custom_domain_verified = 0 
                 WHERE id = ?''', (tenant_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/support-team', methods=['GET', 'POST'])
def admin_support_team():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    if request.method == 'GET':
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT * FROM support_team ORDER BY created_at DESC')
        members = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({'members': members})
        
    elif request.method == 'POST':
        data = request.json or {}
        name = data.get('name')
        secret_code = data.get('secret_code')
        email = data.get('email', '').strip().lower()
        if not name or not secret_code or not email:
            return jsonify({'error': 'Name, access code, and email are required'}), 400
            
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('INSERT INTO support_team (name, secret_code, email) VALUES (?, ?, ?)', (name, secret_code, email))
            conn.commit()
            conn.close()
            return jsonify({'status': 'success'})
        except sqlite3.IntegrityError:
            return jsonify({'error': 'Member name or code already exists'}), 400

@app.route('/api/admin/support-team/<int:member_id>', methods=['DELETE'])
def admin_delete_support_member(member_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM support_team WHERE id = ?', (member_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/mail/config', methods=['GET', 'POST'])
def admin_mail_config():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    if request.method == 'GET':
        c.execute('SELECT key, value FROM system_config WHERE key IN (\'mailman_access_code\', \'mailman_secret_key\', \'mailman_base_url\')')
        rows = c.fetchall()
        conn.close()
        
        config = {r[0]: r[1] for r in rows}
        access_code = config.get('mailman_access_code', '')
        secret_key = config.get('mailman_secret_key', '')
        base_url = config.get('mailman_base_url', 'https://mailman.oqens.me')
        
        # Mask secret key
        if secret_key:
            masked_key = secret_key[:8] + '...' if len(secret_key) > 8 else '********'
        else:
            masked_key = ''
            
        return jsonify({
            'mailman_access_code': access_code,
            'mailman_secret_key': masked_key,
            'mailman_base_url': base_url
        })
        
    elif request.method == 'POST':
        data = request.json or {}
        access_code = data.get('mailman_access_code', '').strip()
        secret_key = data.get('mailman_secret_key', '').strip()
        base_url = data.get('mailman_base_url', '').strip()
        
        # Upsert base url
        c.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (\'mailman_base_url\', ?)', (base_url or 'https://mailman.oqens.me',))
        
        # Upsert access code
        c.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (\'mailman_access_code\', ?)', (access_code,))
        
        # Upsert secret key if it's not the masked value or empty
        if secret_key:
            if not secret_key.endswith('...') and '***' not in secret_key:
                c.execute('INSERT OR REPLACE INTO system_config (key, value) VALUES (\'mailman_secret_key\', ?)', (secret_key,))
        else:
            c.execute('DELETE FROM system_config WHERE key = \'mailman_secret_key\'')
            
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})

def replace_placeholders(text, tenant_row=None, email=None):
    if not text:
        return ""
    
    name = "Valued Customer"
    plan = "N/A"
    email_val = email or ""
    expiry = "N/A"
    status = "N/A"
    cloud_id = "N/A"
    
    if tenant_row:
        t = dict(tenant_row)
        name = t.get('username') or name
        email_val = t.get('custom_domain_email') or email_val or ""
        expiry = t.get('next_billing_date') or expiry
        status = t.get('billing_status') or status
        cloud_id = t.get('cloud_id') or cloud_id
        
        limit_bytes = t.get('storage_limit_bytes', 0) or 0
        if limit_bytes >= 1024**3:
            plan_str = f"{limit_bytes / (1024**3):.1f} GB"
        else:
            plan_str = f"{limit_bytes / (1024**2):.1f} MB"
        rate = t.get('monthly_rate', 0.0) or 0.0
        plan = f"{plan_str} Storage (₹{rate}/mo)"
        
    elif email_val:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT * FROM tenants WHERE custom_domain_email = ?', (email_val,))
        row = c.fetchone()
        conn.close()
        if row:
            return replace_placeholders(text, tenant_row=row, email=email_val)
        else:
            name = email_val.split('@')[0]
            
    res = text
    res = res.replace('{{name}}', name)
    res = res.replace('{{plan}}', plan)
    res = res.replace('{{email}}', email_val)
    res = res.replace('{{dateofexpiry}}', expiry)
    res = res.replace('{{paymentstatus}}', status)
    res = res.replace('{{cloud_id}}', cloud_id)
    return res

@app.route('/api/admin/mail/templates', methods=['GET'])
def admin_get_mail_templates():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM mail_templates ORDER BY id DESC')
    templates = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(templates)

@app.route('/api/admin/mail/templates', methods=['POST'])
def admin_create_mail_template():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    template_id = data.get('id')
    name = data.get('name', '').strip()
    subject = data.get('subject', '').strip()
    body = data.get('body', '').strip()
    
    if not name or not subject or not body:
        return jsonify({'error': 'Template name, subject, and body are required.'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        if template_id:
            c.execute('UPDATE mail_templates SET name = ?, subject = ?, body = ? WHERE id = ?', (name, subject, body, template_id))
        else:
            c.execute('INSERT INTO mail_templates (name, subject, body) VALUES (?, ?, ?)', (name, subject, body))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': 'Template saved successfully.'})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Template name already exists.'}), 400
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/mail/templates/<int:template_id>', methods=['DELETE'])
def admin_delete_mail_template(template_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM mail_templates WHERE id = ?', (template_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/mail/logs', methods=['GET'])
def admin_get_mail_logs():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM mail_logs ORDER BY id DESC LIMIT 200')
    logs = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(logs)

@app.route('/api/admin/mail/send', methods=['POST'])
def admin_mail_send():
    import urllib.request
    import json
    
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    recipient_type = data.get('recipientType')  # 'all', 'tenant', 'custom'
    recipient_tenant_id = data.get('tenantId')   # if 'tenant'
    custom_recipient = data.get('customRecipient')  # if 'custom'
    
    subject = data.get('subject', '').strip()
    from_name = data.get('fromName', '').strip()
    mode = data.get('mode', 'raw')  # 'raw', 'prebuilt', 'template'
    html_body = data.get('html', '').strip()
    template_id = data.get('templateId', '').strip()
    variables_str = data.get('variables', '{}').strip()
    
    # Validate basics
    if mode == 'raw' and not subject:
        return jsonify({'error': 'Subject is required for raw emails'}), 400
    if mode == 'raw' and not html_body:
        return jsonify({'error': 'HTML body is required for raw emails'}), 400
    if mode == 'template' and not template_id:
        return jsonify({'error': 'Template ID is required for Mailman templates'}), 400
    if mode == 'prebuilt' and not template_id:
        return jsonify({'error': 'Template selection is required for prebuilt templates'}), 400
        
    try:
        variables = json.loads(variables_str) if variables_str else {}
    except Exception as e:
        return jsonify({'error': f'Invalid JSON in variables: {str(e)}'}), 400
        
    # Get config
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT key, value FROM system_config WHERE key IN (\'mailman_access_code\', \'mailman_secret_key\', \'mailman_base_url\')')
    rows = c.fetchall()
    config = {r[0]: r[1] for r in rows}
    
    access_code = config.get('mailman_access_code', '').strip()
    secret_key = config.get('mailman_secret_key', '').strip()
    base_url = config.get('mailman_base_url', '').strip() or 'https://mailman.oqens.me'
    
    if not access_code or not secret_key:
        conn.close()
        return jsonify({'error': 'Mailman API credentials are not configured.'}), 400
        
    # Fetch prebuilt template if chosen
    template_subject = ""
    template_body = ""
    if mode == 'prebuilt':
        c.execute('SELECT subject, body FROM mail_templates WHERE id = ? OR name = ?', (template_id, template_id))
        row_tmpl = c.fetchone()
        if not row_tmpl:
            conn.close()
            return jsonify({'error': 'Selected prebuilt template not found.'}), 404
        template_subject, template_body = row_tmpl
        
    # Resolve recipients to list of (email, name, tenant_row)
    recipients = []
    if recipient_type == 'all':
        c.execute('SELECT * FROM tenants WHERE custom_domain_email IS NOT NULL AND custom_domain_email != ''')
        for row in c.fetchall():
            recipients.append((row['custom_domain_email'], row['username'], dict(row)))
    elif recipient_type == 'tenant':
        c.execute('SELECT * FROM tenants WHERE id = ?', (recipient_tenant_id,))
        row = c.fetchone()
        if row:
            row_dict = dict(row)
            email = row_dict.get('custom_domain_email')
            if email:
                recipients.append((email, row_dict['username'], row_dict))
            else:
                conn.close()
                return jsonify({'error': f'Selected tenant "{row_dict["username"]}" does not have a custom domain email registered.'}), 400
        else:
            conn.close()
            return jsonify({'error': 'Tenant not found.'}), 400
    elif recipient_type == 'custom':
        if not custom_recipient:
            conn.close()
            return jsonify({'error': 'Recipient email address is required.'}), 400
        emails = [e.strip() for e in custom_recipient.split(',') if e.strip()]
        for email in emails:
            # Check if there is an associated tenant in the system
            c.execute('SELECT * FROM tenants WHERE custom_domain_email = ?', (email,))
            row = c.fetchone()
            row_dict = dict(row) if row else None
            name = email.split('@')[0]
            recipients.append((email, name, row_dict))
    else:
        conn.close()
        return jsonify({'error': 'Invalid recipient type.'}), 400
        
    conn.close()
    
    if not recipients:
        return jsonify({'error': 'No valid recipients found with registered emails.'}), 400
        
    # Queue emails via Mailman for Off-Peak processing
    now_utc = datetime.datetime.utcnow()
    now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
    
    next_ist = now_ist.replace(hour=2, minute=0, second=0, microsecond=0)
    if now_ist.hour >= 2:
        next_ist += datetime.timedelta(days=1)
        
    next_utc = next_ist - datetime.timedelta(hours=5, minutes=30)
    
    payload_dict = {
        'recipients': recipients,
        'base_url': base_url,
        'from_name': from_name,
        'mode': mode,
        'subject': subject,
        'html_body': html_body,
        'template_subject': template_subject,
        'template_body': template_body,
        'access_code': access_code,
        'secret_key': secret_key,
        'template_id': template_id
    }
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO scheduled_tasks (task_type, payload, execute_after) VALUES (?, ?, ?)",
              ('mailman_bulk', json.dumps(payload_dict), next_utc.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()
    
    return jsonify({
        'status': 'success',
        'message': f'Emails queued successfully. They will be sent at {next_ist.strftime("%Y-%m-%d 02:00 %p")} IST.',
        'queued_count': len(recipients)
    })

def execute_mailman_bulk_task(payload_dict):
    recipients = payload_dict['recipients']
    base_url = payload_dict['base_url']
    from_name = payload_dict['from_name']
    mode = payload_dict['mode']
    subject = payload_dict['subject']
    html_body = payload_dict['html_body']
    template_subject = payload_dict['template_subject']
    template_body = payload_dict['template_body']
    access_code = payload_dict['access_code']
    secret_key = payload_dict['secret_key']
    template_id = payload_dict['template_id']
    
    api_url = f"{base_url.rstrip('/')}/api/v1/send"
    
    for email, name, tenant_row in recipients:
        subj_processed = ""
        body_processed = ""
        
        payload = {
            'to': email,
            'recipientName': name,
            'fromName': from_name or 'OQENS Admin'
        }
        
        if mode == 'raw':
            subj_processed = replace_placeholders(subject, tenant_row=tenant_row, email=email)
            body_processed = replace_placeholders(html_body, tenant_row=tenant_row, email=email)
            payload['subject'] = subj_processed
            payload['html'] = body_processed
        elif mode == 'prebuilt':
            subj_processed = replace_placeholders(template_subject, tenant_row=tenant_row, email=email)
            body_processed = replace_placeholders(template_body, tenant_row=tenant_row, email=email)
            payload['subject'] = subj_processed
            payload['html'] = body_processed
        else: # template mode
            resolved_vars = {}
            resolved_vars['name'] = name
            resolved_vars['email'] = email
            resolved_vars['plan'] = 'N/A'
            resolved_vars['dateofexpiry'] = 'N/A'
            resolved_vars['paymentstatus'] = 'N/A'
            resolved_vars['cloud_id'] = 'N/A'
            if tenant_row:
                resolved_vars['name'] = tenant_row.get('username') or name
                resolved_vars['email'] = tenant_row.get('custom_domain_email') or email
                resolved_vars['dateofexpiry'] = tenant_row.get('next_billing_date') or 'N/A'
                resolved_vars['paymentstatus'] = tenant_row.get('billing_status') or 'N/A'
                resolved_vars['cloud_id'] = tenant_row.get('cloud_id') or 'N/A'
                limit_bytes = tenant_row.get('storage_limit_bytes', 0) or 0
                if limit_bytes >= 1024**3:
                    plan_str = f"{limit_bytes / (1024**3):.1f} GB"
                else:
                    plan_str = f"{limit_bytes / (1024**2):.1f} MB"
                rate = tenant_row.get('monthly_rate', 0.0) or 0.0
                resolved_vars['plan'] = f"{plan_str} (₹{rate}/mo)"
                
            payload['templateId'] = template_id
            payload['variables'] = resolved_vars
            
        is_success = False
        error_msg = None
        try:
            req = urllib.request.Request(
                api_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={
                    'Content-Type': 'application/json',
                    'x-api-access-code': access_code,
                    'x-api-secret-key': secret_key
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode('utf-8'))
                if resp_data.get('success'):
                    is_success = True
                else:
                    error_msg = resp_data.get('message', 'Unknown response state')
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode('utf-8'))
                error_msg = err_body.get('message') or err_body.get('error') or e.reason
            except Exception:
                error_msg = e.reason
        except Exception as e:
            error_msg = str(e)
            
        try:
            conn_l = sqlite3.connect(DB_FILE)
            c_l = conn_l.cursor()
            subj_logged = subj_processed or f"Mailman Template: {template_id}"
            c_l.execute('INSERT INTO mail_logs (recipient, subject, status, error_message) VALUES (?, ?, ?, ?)',
                        (email, subj_logged, 'success' if is_success else 'failed', error_msg))
            conn_l.commit()
            conn_l.close()
        except Exception as ex:
            print("Log insertion failed:", ex)

@app.route('/edit/<path:slug>')
def papers_spa_edit_route(slug):
    host = request.host.split(':')[0]
    if host == 'papers.oqens.me':
        if session.get('role') == 'tenant':
            resp = app.send_static_file('papers.html')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp
        return redirect('https://papers.oqens.me/login')
    # If not papers host, fallback to custom domain handler
    return custom_domain_dl_route('edit/' + slug)

@app.route('/p/<path:slug>')
def papers_spa_p_route(slug):
    host = request.host.split(':')[0]
    if host == 'papers.oqens.me':
        resp = app.send_static_file('papers.html')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        if session.get('role') == 'tenant':
            return resp
        return resp # Public pages also served via SPA
    return custom_domain_dl_route('p/' + slug)

@app.route('/<path:file_path>')
def custom_domain_dl_route(file_path):
    import uuid
    from flask import make_response
    host = request.host.split(':')[0]
    
    # Fallback catch-all routing for system hostnames on page refresh
    if host == 'status.echo.oqens.me':
        return app.send_static_file('status.html')
    if host == 'host.support.oqens.me' or host == 'support.oqens.me':
        return app.send_static_file('support.html')
    if host == 'host.echo.oqens.me':
        if session.get('role') == 'admin':
            return app.send_static_file('admin.html')
        return redirect('/login')
        
    # Fallback catch-all routing for main dashboard and photos SPA subpaths on page refresh
    if host in ['dash.echo.oqens.me', 'dash.oqens.me', 'photos.echo.oqens.me', 'localhost', '127.0.0.1']:
        dashboard_routes = ['storage', 'settings', 'usage', 'guide', 'billing']
        is_dashboard_route = file_path in dashboard_routes or file_path.startswith('dash/') or file_path == 'dash'
        
        photos_routes = ['album', 'manage', 'shared', 'album-request']
        is_photos_route = file_path in photos_routes or file_path.startswith('album/') or file_path.startswith('shared/') or file_path.startswith('album-request')
        
        if is_dashboard_route:
            if session.get('role') == 'tenant':
                return app.send_static_file('index.html')
            if host in ['localhost', '127.0.0.1']:
                return redirect('/login?redirect=/' + file_path)
            return redirect('https://echo.oqens.me/login?redirect=https://' + host + '/' + file_path)
            
        if is_photos_route or host == 'photos.echo.oqens.me':
            if file_path.startswith('shared/') or file_path.startswith('album-request') or file_path.startswith('api/photos/albums/shared/view/'):
                return app.send_static_file('photos.html')
            if session.get('role') == 'tenant':
                return app.send_static_file('photos.html')
            if host in ['localhost', '127.0.0.1']:
                return redirect('/login?redirect=/' + file_path)
            return redirect('https://echo.oqens.me/login?redirect=https://photos.echo.oqens.me/' + file_path)

    if host in ['echo.oqens.me', 'dl.oqens.me']:
        return "Not Found", 404

    # papers.oqens.me — serve papers SPA for sub-paths (editor deep links etc)
    if host == 'papers.oqens.me':
        if session.get('role') == 'tenant':
            resp = app.send_static_file('papers.html')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp
        return redirect('https://papers.oqens.me/login')
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE custom_domain = ? AND custom_domain_verified = 1 AND (is_deleted = 0 OR is_deleted IS NULL)', (host,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return "Custom domain configuration not found or pending verification", 404
    tenant = dict(row)
        
    if tenant.get('billing_status') == 'Suspended':
        return "Subscription Suspended. Please contact support or your administrator.", 403
    if tenant.get('block_downloads') == 1:
        return "Downloads are disabled for this tenant", 403

    username = tenant['username']
    
    if is_bandwidth_exhausted(dict(tenant)):
        return render_bandwidth_error()
        
    file_name = os.path.basename(file_path)
    tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
    full_file_path = os.path.join(tenant_dir, file_name)
    
    if not os.path.exists(full_file_path):
        return "File not found", 404
        
    file_size = os.path.getsize(full_file_path)
    
    device_id = request.cookies.get('oqens_device_id')
    is_new_device = False
    if not device_id:
        device_id = str(uuid.uuid4())
        is_new_device = True
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    view_added = False
    try:
        c.execute('INSERT INTO device_views (tenant_username, device_id) VALUES (?, ?)', (username, device_id))
        view_added = True
    except sqlite3.IntegrityError:
        conn.rollback()
        view_added = False
        
    if view_added:
        c.execute('UPDATE tenants SET unique_views_count = unique_views_count + 1, bandwidth_bytes = bandwidth_bytes + ? WHERE id = ?', (file_size, tenant['id']))
    else:
        c.execute('UPDATE tenants SET bandwidth_bytes = bandwidth_bytes + ? WHERE id = ?', (file_size, tenant['id']))
    conn.commit()
    conn.close()
    
    as_attachment = 'preview' not in request.args
    content_type, _ = mimetypes.guess_type(file_name)
    if not content_type:
        content_type = 'application/octet-stream'
    response = make_response("")
    response.headers['Content-Type'] = content_type
    response.headers['X-Accel-Redirect'] = f'/protected_files/{username}/{file_name}'
    if as_attachment:
        response.headers['Content-Disposition'] = f'attachment; filename="{file_name}"'
    
    if is_new_device:
        max_age = 10 * 365 * 24 * 60 * 60
        response.set_cookie('oqens_device_id', device_id, max_age=max_age)
        
    return response

# --- Billing, Pricing, Signups, and Cashfree Payments ---

@app.route('/pricing')
def serve_pricing():
    return app.send_static_file('pricing.html')





@app.route('/checkout')
def serve_checkout():
    token = request.args.get('token')
    if not token:
        return "Checkout token is required", 400
        
    # Check if logged in
    role = session.get('role')
    tenant_id = session.get('tenant_id')
    
    if role != 'tenant' or not tenant_id:
        # Redirect to login
        login_url = f"https://echo.oqens.me/login?redirect=https://payments.oqens.me/checkout?token={token}"
        return redirect(login_url)
        
    # Verify the logged-in tenant matches the token
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM tenants WHERE checkout_token = ?', (token,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return "Invalid checkout token", 404
        
    # Serve payments.html regardless of tenant match so that the frontend can handle the mismatch UI gracefully (e.g. show switch account buttons).
    # The actual security check is strictly enforced at the API level (/api/payments/checkout-details).
    return app.send_static_file('payments.html')

@app.route('/privacy')
def serve_privacy():
    return app.send_static_file('privacy.html')

@app.route('/terms')
def serve_terms():
    return app.send_static_file('terms.html')

@app.route('/api/auth/signup', methods=['POST'])
def tenant_signup():
    data = request.json or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    plan = data.get('plan', 'free').lower()
    
    if not username or not email or not password:
        return jsonify({"error": "All fields are required"}), 400
        
    if plan not in ['free', 'writer', 'starter', 'pro']:
        return jsonify({"error": "Invalid plan chosen"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Check if username exists
    c.execute('SELECT id FROM tenants WHERE username = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (username,))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Username already exists"}), 400
        
    # Check if email exists
    c.execute('SELECT id FROM tenants WHERE custom_domain_email = ? AND (is_deleted = 0 OR is_deleted IS NULL)', (email,))
    if c.fetchone():
        conn.close()
        return jsonify({"error": "Email is already registered"}), 400
        
    # Plan parameters allocation
    if plan == 'free':
        storage_limit_bytes = 100 * 1024 * 1024 # 100MB
        bandwidth_limit_bytes = 500 * 1024 * 1024 # 500MB
        monthly_rate = 0.0
        billing_status = 'Active'
    else:
        plan_cfg = get_plan_config(plan)
        # Allocate free tier limits initially until payment is verified
        storage_limit_bytes = 100 * 1024 * 1024
        bandwidth_limit_bytes = 500 * 1024 * 1024
        monthly_rate = plan_cfg['rate']
        billing_status = 'Pending Payment'
        
    # Check host capacity limits
    c.execute("SELECT key, value FROM system_config WHERE key IN ('host_max_storage_gb', 'host_max_tenants', 'host_max_bandwidth_gb')")
    configs = {row[0]: row[1] for row in c.fetchall()}
    host_max_storage_gb = float(configs.get('host_max_storage_gb') or 0)
    host_max_tenants = int(configs.get('host_max_tenants') or 0)
    host_max_bandwidth_gb = float(configs.get('host_max_bandwidth_gb') or 0)

    c.execute('SELECT SUM(storage_limit_bytes) as total_bytes, SUM(bandwidth_limit_bytes) as total_bandwidth, COUNT(id) as total_tenants FROM tenants WHERE is_deleted = 0 OR is_deleted IS NULL')
    row = c.fetchone()
    allocated_bytes = int(row[0] or 0)
    allocated_bandwidth_bytes = int(row[1] or 0)
    active_tenants = int(row[2] or 0)

    if host_max_tenants > 0 and active_tenants >= host_max_tenants:
        conn.close()
        return jsonify({"error": "Registration closed: Host capacity reached (Tenant limit exceeded)"}), 403

    if host_max_storage_gb > 0:
        max_storage_bytes = host_max_storage_gb * 1024 * 1024 * 1024
        if allocated_bytes + storage_limit_bytes > max_storage_bytes:
            conn.close()
            return jsonify({"error": "Registration closed: Host capacity reached (Storage limit exceeded)"}), 403

    if host_max_bandwidth_gb > 0:
        max_bandwidth_bytes = host_max_bandwidth_gb * 1024 * 1024 * 1024
        if allocated_bandwidth_bytes + bandwidth_limit_bytes > max_bandwidth_bytes:
            conn.close()
            return jsonify({"error": "Registration closed: Host capacity reached (Bandwidth limit exceeded)"}), 403
            
    # Generate unique Cloud ID
    while True:
        cid = generate_cloud_id()
        c.execute('SELECT id FROM tenants WHERE cloud_id = ?', (cid,))
        if not c.fetchone():
            break
            
    # Generate unique secret code (to satisfy unique constraint and backward compatibility)
    while True:
        secret_code = "".join(random.choices(string.digits, k=4))
        c.execute('SELECT id FROM tenants WHERE secret_code = ?', (secret_code,))
        if not c.fetchone():
            break
            
    next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
    auto_renew = 1
    
    verification_token = secrets.token_hex(20)
    
    # Generate unique checkout token
    while True:
        checkout_token = secrets.token_hex(12)
        c.execute('SELECT id FROM tenants WHERE checkout_token = ?', (checkout_token,))
        if not c.fetchone():
            break
            
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).isoformat()
            
    try:
        c.execute('''INSERT INTO tenants (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, 
                     cloud_id, custom_domain_email, password, monthly_rate, auto_renew, next_billing_date, billing_status, email_verified, email_verification_token, checkout_token, tier, checkout_token_expires_at) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)''',
                  (username, secret_code, storage_limit_bytes, bandwidth_limit_bytes, cid, email, password, 
                   monthly_rate, auto_renew, next_billing_date, billing_status, verification_token, checkout_token, plan, expires_at))
        conn.commit()
        
        # Get the ID of the new tenant
        c.execute('SELECT id FROM tenants WHERE username = ?', (username,))
        tenant_id = c.fetchone()[0]
        
        # Create directory
        tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
        os.makedirs(tenant_dir, exist_ok=True)
        
        # Apply referral if active
        _apply_referral_to_tenant(tenant_id, username, conn, c)
        
        # Re-fetch updated tenant details (referral may have activated and changed tokens/status)
        c.execute('SELECT billing_status, cloud_id, checkout_token FROM tenants WHERE id = ?', (tenant_id,))
        updated_row = c.fetchone()
        if updated_row:
            billing_status = updated_row[0]
            cid = updated_row[1]
            checkout_token = updated_row[2]
            
        # Send email verification
        subject = "Verify your OQENS email address"
        html_body = f"""
        <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
            <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Email Verification</h2>
            <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {username},</p>
            <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Thank you for registering! Please click the button below to verify your email address and activate your cloud storage workspace account:</p>
            <p style="margin: 30px 0;"><a href="https://auth.oqens.me/verify-email?token={verification_token}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500;">Verify Email Address</a></p>
            <p style="font-size: 0.85rem; color: #666;">Or copy and paste this URL into your browser:</p>
            <p style="font-size: 0.82rem; color: #888; word-break: break-all;"><a href="https://auth.oqens.me/verify-email?token={verification_token}" style="color: #666;">https://auth.oqens.me/verify-email?token={verification_token}</a></p>
            <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This verification link is required to secure your account. If you did not sign up for OQENS, please ignore this email.</p>
        </div>
        """
        send_system_email(email, subject, html_body)
        
        conn.close()
        
        resp_data = {
            "status": "success", 
            "tenant_id": tenant_id, 
            "checkout_token": checkout_token,
            "username": username, 
            "cloud_id": cid, 
            "billing_status": billing_status,
            "message": "Verification link sent! Please check your email to verify your account."
        }
        response = make_response(jsonify(resp_data))
        response.delete_cookie('_oqref', path='/', domain='.oqens.me')
        return response
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments/checkout-details', methods=['GET'])
def payments_checkout_details():
    token = request.args.get('token')
    if not token:
        return jsonify({"error": "token is required"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, username, custom_domain_email, monthly_rate, billing_status, checkout_token, tier, checkout_token_expires_at, custom_domain, custom_domain_verified FROM tenants WHERE checkout_token = ?', (token,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return jsonify({"error": "Tenant workspace not found"}), 404
    tenant = dict(row)
        
    import datetime
    expires_at_str = tenant.get("checkout_token_expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.datetime.fromisoformat(expires_at_str)
            if datetime.datetime.utcnow() > expires_at:
                return jsonify({"error": "Checkout token has expired. Please request a new checkout link by verifying email or logging in again."}), 400
        except Exception:
            pass
            
    dashboard_url = "https://dash.echo.oqens.me/"
    if tenant.get("custom_domain") and tenant.get("custom_domain_verified"):
        dashboard_url = f"https://{tenant['custom_domain']}/dashboard"
        
    return jsonify({
        "username": tenant["username"],
        "email": tenant["custom_domain_email"],
        "amount": tenant["monthly_rate"],
        "status": tenant["billing_status"],
        "tier": tenant.get("tier", "free"),
        "dashboard_url": dashboard_url
    })

@app.route('/api/payments/create-order', methods=['POST'])
def payments_create_order():
    data = request.json or {}
    token = data.get('token')
    
    if not token:
        return jsonify({"error": "Checkout token is required"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE checkout_token = ?', (token,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Tenant not found"}), 404
    tenant = dict(row)
    tenant_id = tenant['id']
    
    import datetime
    expires_at_str = tenant.get("checkout_token_expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.datetime.fromisoformat(expires_at_str)
            if datetime.datetime.utcnow() > expires_at:
                conn.close()
                return jsonify({"error": "Checkout token has expired. Please request a new checkout link by verifying email or logging in again."}), 400
        except Exception:
            pass
    
    # Retrieve Cashfree keys and mode
    c.execute('SELECT value FROM system_config WHERE key = \'cashfree_app_id\'')
    row_appid = c.fetchone()
    cf_app_id = row_appid['value'] if row_appid else ""
    
    c.execute('SELECT value FROM system_config WHERE key = \'cashfree_secret_key\'')
    row_secret = c.fetchone()
    cf_secret_key = row_secret['value'] if row_secret else ""
    
    c.execute('SELECT value FROM system_config WHERE key = \'cashfree_mode\'')
    row_mode = c.fetchone()
    cf_mode = row_mode['value'] if row_mode else "sandbox"
    
    conn.close()
    
    amount = tenant.get('monthly_rate', 0.0)
    if amount <= 0:
        return jsonify({"error": "This tenant has a free plan. No payment required."}), 400
        
    coupon_code = data.get('coupon', '').strip().upper()
    coupon_applied = None
    if coupon_code:
        import datetime
        conn_c = sqlite3.connect(DB_FILE)
        conn_c.row_factory = sqlite3.Row
        cc = conn_c.cursor()
        cc.execute('SELECT * FROM coupons WHERE code = ?', (coupon_code,))
        row_c = cc.fetchone()
        if row_c:
            coupon = dict(row_c)
            is_valid = True
            if coupon['status'] != 'Active':
                is_valid = False
            if coupon['expiry_date']:
                try:
                    expiry = datetime.datetime.strptime(coupon['expiry_date'], '%Y-%m-%d').date()
                    if datetime.date.today() > expiry:
                        is_valid = False
                except Exception:
                    pass
            if coupon['max_uses'] != -1 and coupon['used_count'] >= coupon['max_uses']:
                is_valid = False
                
            # Check targeting constraint
            c_target_type = coupon.get('target_type', 'global') or 'global'
            c_target_tenants = coupon.get('target_tenants', '') or ''
            tenant_username = tenant.get('username', '')
            if c_target_type == 'single':
                if tenant_username.strip().lower() != c_target_tenants.strip().lower():
                    is_valid = False
            elif c_target_type == 'selected':
                allowed_users = [u.strip().lower() for u in c_target_tenants.split(',') if u.strip()]
                if tenant_username.strip().lower() not in allowed_users:
                    is_valid = False
                    
            # Check plan constraint
            c_target_plan = coupon.get('target_plan', 'all') or 'all'
            if c_target_plan != 'all':
                c_tier = tenant.get('tier', 'free') or 'free'
                if c_tier.strip().lower() != c_target_plan.strip().lower():
                    plan_cfg = get_plan_config(c_target_plan)
                    if abs(amount - plan_cfg['rate']) > 0.01:
                        is_valid = False
                
            if is_valid:
                discount_val = coupon['discount_value']
                if coupon['discount_type'] == 'percentage':
                    discount_amount = (discount_val / 100.0) * amount
                else:
                    discount_amount = discount_val
                discount_amount = min(discount_amount, amount)
                amount = max(0.0, amount - discount_amount)
                coupon_applied = coupon_code
        conn_c.close()

    if amount <= 0.0:
        import datetime
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        
        conn_act = sqlite3.connect(DB_FILE)
        c_act = conn_act.cursor()
        
        # Upgrade limits for free activation
        tier = tenant.get('tier', 'free') or 'free'
        plan_cfg = get_plan_config(tier)
        
        c_act.execute('''UPDATE tenants SET billing_status = \'Active\', next_billing_date = ?, checkout_token = NULL, checkout_token_expires_at = NULL,
                         storage_limit_bytes = ?, bandwidth_limit_bytes = ?, monthly_rate = ? WHERE id = ?''', 
                      (next_billing_date, plan_cfg['storage_limit'], plan_cfg['bandwidth_limit'], plan_cfg['rate'], tenant_id))
        c_act.execute('INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  (tenant_id, 0.0, today_str, 'Paid', f'Subscription activated via coupon code {coupon_applied}.', f"free_coupon_{coupon_applied}", 'N/A', tenant.get('custom_domain_email') or tenant.get('email') or ''))
        if coupon_applied:
            c_act.execute('UPDATE coupons SET used_count = used_count + 1 WHERE code = ?', (coupon_applied,))
        conn_act.commit()
        conn_act.close()
        
        # Send clean, light email invoice for free activation
        try:
            email = tenant.get('custom_domain_email') or tenant.get('email')
            if email:
                subject = "OQENS Invoice: Subscription Activated Successfully"
                html_body = f"""
                <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                    <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Invoice / Receipt</h2>
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {tenant['username']},</p>
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Your workspace has been successfully activated using coupon code <strong>{coupon_applied}</strong>. Here is the receipt for your monthly subscription:</p>
                    
                    <div style="background: #fafafa; border: 1px solid #eeeeee; border-radius: 6px; padding: 20px; margin: 25px 0; font-size: 0.9rem;">
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Cloud ID</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">{tenant['cloud_id']}</td></tr>
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Amount Paid</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #10b981;">INR 0.00 (100% Coupon Applied)</td></tr>
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Status</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #10b981;">Activated / Free Promo</td></tr>
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Next Renewal Date</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">{next_billing_date}</td></tr>
                            <tr><td style="padding: 8px 0; color: #666;">Payment Method</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">100% Discount Coupon</td></tr>
                        </table>
                    </div>
                    
                    <p style="font-size: 0.9rem; color: #666; line-height: 1.5; margin-bottom: 25px;">You can access your workspace here: <a href="https://dash.echo.oqens.me/" style="color: #111; font-weight: 500; text-decoration: underline;">OQENS Dashboard</a></p>
                    <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This is an automated receipt. If you have any questions, please contact support.</p>
                </div>
                """
                import base64
                pdf_data = generate_invoice_pdf(tenant, 0.0, today_str)
                attachments = None
                if pdf_data:
                    pdf_b64 = base64.b64encode(pdf_data).decode('utf-8')
                    attachments = [
                        {
                            'filename': 'invoice.pdf',
                            'content': pdf_b64,
                            'contentType': 'application/pdf'
                        }
                    ]
                send_system_email(email, subject, html_body, attachments=attachments)
        except Exception as e:
            print("Email receipt failed for free activation:", e)
            
        return jsonify({
            "status": "free_activation",
            "message": "Coupon applied! Your workspace has been activated for free."
        })

    import time
    order_id = f"order_{tenant['checkout_token']}_{coupon_applied or 'NONE'}_{int(time.time())}"
    
    # Make API call to Cashfree
    import urllib.request
    import json
    
    url = "https://sandbox.cashfree.com/pg/orders" if cf_mode == "sandbox" else "https://api.cashfree.com/pg/orders"
    
    headers = {
        "Content-Type": "application/json",
        "x-api-version": "2023-08-01",
        "x-client-id": cf_app_id,
        "x-client-secret": cf_secret_key
    }
    
    payload = {
        "order_id": order_id,
        "order_amount": float(amount),
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"cust_{tenant_id}",
            "customer_phone": "9999999999",
            "customer_email": tenant.get('custom_domain_email', 'user@domain.com')
        },
        "order_meta": {
            "return_url": f"https://payments.oqens.me/payment-callback?order_id={order_id}"
        }
    }
    
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req) as res:
            res_data = json.loads(res.read().decode('utf-8'))
            payment_session_id = res_data.get('payment_session_id')
            return jsonify({
                "status": "success",
                "payment_session_id": payment_session_id,
                "order_id": order_id,
                "cf_mode": cf_mode
            })
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode('utf-8')
        try:
            err_json = json.loads(err_msg)
            return jsonify({"error": err_json.get('message', 'Cashfree error')}), 500
        except Exception:
            return jsonify({"error": f"Cashfree API Error: {err_msg}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/payments/upgrade-plan', methods=['POST'])
def payments_upgrade_plan():
    data = request.json or {}
    token = data.get('token')
    plan_name = data.get('plan')
    
    if not token or not plan_name:
        return jsonify({"error": "Token and plan selection are required."}), 400
        
    if plan_name not in ['writer', 'starter', 'pro']:
        return jsonify({"error": "Invalid plan selected."}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id, monthly_rate FROM tenants WHERE checkout_token = ?', (token,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Tenant workspace not found."}), 404
        
    tenant_id, current_rate = row
    
    # Map plan values
    plan_cfg = get_plan_config(plan_name)
    rate = plan_cfg['rate']
    storage_limit = plan_cfg['storage_limit']
    bandwidth_limit = plan_cfg['bandwidth_limit']
        
    # Prevent downgrades or switching to the same active plan
    if current_rate is not None and current_rate >= rate:
        conn.close()
        return jsonify({"error": "You are already subscribed to a plan with equal or higher limits. Downgrades are not allowed."}), 400
        
    # Update rate, limits and tier
    c.execute('''UPDATE tenants 
                 SET monthly_rate = ?, storage_limit_bytes = ?, bandwidth_limit_bytes = ?, tier = ?, billing_status = 'Pending Payment'
                 WHERE id = ?''', (rate, storage_limit, bandwidth_limit, plan_name, tenant_id))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "message": "Plan updated. Redirecting to checkout."})

@app.route('/payment-callback')
def payment_callback():
    order_id = request.args.get('order_id')
    if not order_id:
        return "Missing order_id", 400
        
    parts = order_id.split('_')
    if len(parts) < 3:
        return "Invalid order_id format", 400
    token = parts[1]
    coupon_code = None
    if len(parts) >= 4:
        coupon_code = parts[2]
        if coupon_code == 'NONE':
            coupon_code = None
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE checkout_token = ?', (token,))
    row_tenant = c.fetchone()
    if not row_tenant:
        conn.close()
        return "Tenant not found", 404
    tenant = dict(row_tenant)
    tenant_id = tenant['id']
    
    # Retrieve Cashfree keys and mode
    c.execute('SELECT value FROM system_config WHERE key = \'cashfree_app_id\'')
    row_appid = c.fetchone()
    cf_app_id = row_appid['value'] if row_appid else ""
    
    c.execute('SELECT value FROM system_config WHERE key = \'cashfree_secret_key\'')
    row_secret = c.fetchone()
    cf_secret_key = row_secret['value'] if row_secret else ""
    
    c.execute('SELECT value FROM system_config WHERE key = \'cashfree_mode\'')
    row_mode = c.fetchone()
    cf_mode = row_mode['value'] if row_mode else "sandbox"
    
    url = f"https://sandbox.cashfree.com/pg/orders/{order_id}" if cf_mode == "sandbox" else f"https://api.cashfree.com/pg/orders/{order_id}"
    
    import urllib.request
    import json
    
    headers = {
        "Content-Type": "application/json",
        "x-api-version": "2023-08-01",
        "x-client-id": cf_app_id,
        "x-client-secret": cf_secret_key
    }
    
    is_paid = False
    error_msg = ""
    order_amount = tenant.get('monthly_rate', 0.0)
    
    try:
        req = urllib.request.Request(url, headers=headers, method='GET')
        with urllib.request.urlopen(req) as res:
            res_data = json.loads(res.read().decode('utf-8'))
            order_status = res_data.get('order_status')
            order_amount = res_data.get('order_amount', order_amount)
            if order_status == 'PAID':
                is_paid = True
            else:
                error_msg = f"Order status is {order_status}"
    except Exception as e:
        error_msg = str(e)
        
    if is_paid:
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        
        # Fetch payment details from Cashfree payments endpoint
        cf_payment_id = None
        utr = None
        payment_email = tenant.get('custom_domain_email') or tenant.get('email')
        
        try:
            payments_url = f"https://sandbox.cashfree.com/pg/orders/{order_id}/payments" if cf_mode == "sandbox" else f"https://api.cashfree.com/pg/orders/{order_id}/payments"
            pay_req = urllib.request.Request(payments_url, headers=headers, method='GET')
            with urllib.request.urlopen(pay_req) as pay_res:
                pay_data = json.loads(pay_res.read().decode('utf-8'))
                if isinstance(pay_data, list) and len(pay_data) > 0:
                    # Get the successful payment or first payment
                    success_payment = next((p for p in pay_data if p.get('payment_status') == 'SUCCESS'), pay_data[0])
                    cf_payment_id = str(success_payment.get('cf_payment_id', ''))
                    utr = str(success_payment.get('bank_reference', ''))
                    cust_details = success_payment.get('customer_details', {})
                    if cust_details.get('customer_email'):
                        payment_email = cust_details.get('customer_email')
        except Exception as e:
            print("Failed to fetch Cashfree payments details:", e)
            
        # Upgrade limits upon successful payment
        tier = tenant.get('tier', 'free') or 'free'
        plan_cfg = get_plan_config(tier)
        c.execute('''UPDATE tenants SET billing_status = \'Active\', next_billing_date = ?, checkout_token = NULL, checkout_token_expires_at = NULL,
                     storage_limit_bytes = ?, bandwidth_limit_bytes = ?, monthly_rate = ? WHERE id = ?''', 
                  (next_billing_date, plan_cfg['storage_limit'], plan_cfg['bandwidth_limit'], plan_cfg['rate'], tenant_id))
        description = 'Cashfree subscription payment success.'
        if coupon_code:
            description = f'Cashfree subscription payment success (Coupon {coupon_code} applied).'
            c.execute('UPDATE coupons SET used_count = used_count + 1 WHERE code = ?', (coupon_code,))
        c.execute('INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  (tenant_id, order_amount, today_str, 'Paid', description, cf_payment_id or order_id, utr or 'N/A', payment_email or ''))
        conn.commit()
        conn.close()
        
        # Send clean, light email invoice
        try:
            email = tenant.get('custom_domain_email')
            if email:
                subject = "OQENS Invoice: Monthly Subscription Activated"
                html_body = f"""
                <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                    <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Invoice / Receipt</h2>
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {tenant['username']},</p>
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Your payment has been successfully processed. Here is the receipt for your monthly subscription:</p>
                    
                    <div style="background: #fafafa; border: 1px solid #eeeeee; border-radius: 6px; padding: 20px; margin: 25px 0; font-size: 0.9rem;">
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Cloud ID</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">{tenant['cloud_id']}</td></tr>
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Amount Paid</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #10b981;">INR {order_amount:.2f}</td></tr>
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Status</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #10b981;">Successful / Paid</td></tr>
                            <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Next Renewal Date</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">{next_billing_date}</td></tr>
                            <tr><td style="padding: 8px 0; color: #666;">Payment Method</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">Cashfree Gateway</td></tr>
                        </table>
                    </div>
                    
                    <p style="font-size: 0.9rem; color: #666; line-height: 1.5; margin-bottom: 25px;">You can access your workspace here: <a href="https://dash.echo.oqens.me/" style="color: #111; font-weight: 500; text-decoration: underline;">OQENS Dashboard</a></p>
                    <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This is an automated receipt. If you have any questions, please contact support.</p>
                </div>
                """
                import base64
                pdf_data = generate_invoice_pdf(tenant, float(order_amount), today_str)
                attachments = None
                if pdf_data:
                    pdf_b64 = base64.b64encode(pdf_data).decode('utf-8')
                    attachments = [
                        {
                            'filename': 'invoice.pdf',
                            'content': pdf_b64,
                            'contentType': 'application/pdf'
                        }
                    ]
                send_system_email(email, subject, html_body, attachments=attachments)
        except Exception as e:
            print("Email receipt failed:", e)
            
        html_success = f"""<!DOCTYPE html>
<html>
<head>
    <title>Payment Successful | OQENS</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/@phosphor-icons/web"></script>
    <style>
        body {{ font-family: 'Inter', sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #fafafa; color: #111; }}
        .card {{ background: #fff; padding: 40px; border-radius: 12px; border: 1.5px solid #eee; text-align: center; max-width: 420px; box-shadow: 0 4px 20px rgba(0,0,0,0.03); }}
        .icon {{ font-size: 3.5rem; color: #10b981; margin-bottom: 20px; }}
        h1 {{ font-weight: 500; font-size: 1.5rem; margin: 0 0 10px 0; letter-spacing: -0.3px; }}
        p {{ color: #666; font-size: 0.9rem; line-height: 1.5; margin: 0 0 25px 0; }}
        .details {{ text-align: left; background: #fafafa; border: 1.5px solid #eee; border-radius: 6px; padding: 15px; margin-bottom: 25px; font-size: 0.85rem; }}
        .details div {{ display: flex; justify-content: space-between; margin-bottom: 8px; }}
        .details div:last-child {{ margin-bottom: 0; }}
        .details strong {{ color: #111; }}
        .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.88rem; font-weight: 500; width: 100%; box-sizing: border-box; transition: opacity 0.2s; }}
        .btn:hover {{ opacity: 0.9; }}
    </style>
</head>
<body>
    <div class="card">
        <i class="ph ph-check-circle icon"></i>
        <h1>Payment Successful</h1>
        <p>Thank you! Your payment has been processed successfully, and your storage workspace is now fully active.</p>
        <div class="details">
            <div><span>Account Username</span><strong>{tenant['username']}</strong></div>
            <div><span>Cloud ID</span><strong>{tenant['cloud_id']}</strong></div>
            <div><span>Amount Paid</span><strong>INR {order_amount:.2f}</strong></div>
            <div><span>Billing Cycle</span><strong>Monthly</strong></div>
        </div>
        <a href="https://dash.echo.oqens.me/" class="btn">Proceed to Dashboard <i class="ph ph-arrow-right"></i></a>
    </div>
</body>
</html>
"""
        return html_success, 200
    else:
        conn.close()
        
        # Send payment failed email
        try:
            email = tenant.get('custom_domain_email')
            if email:
                subject = "OQENS Alert: Payment Failed & Workspace Suspended"
                html_body = f"""
                <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                    <h2 style="font-weight: 500; font-size: 1.4rem; color: #ef4444; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Payment Failed</h2>
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {tenant['username']},</p>
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">We were unable to process your monthly subscription payment of <strong>INR {order_amount:.2f}</strong>. As a result, your workspace has been temporarily suspended.</p>
                    
                    <div style="background: #fff5f5; border: 1px solid #fed7d7; border-radius: 6px; padding: 20px; margin: 25px 0; font-size: 0.9rem;">
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr style="border-bottom: 1px solid #fed7d7;"><td style="padding: 8px 0; color: #c53030;">Cloud ID</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #c53030;">{tenant['cloud_id']}</td></tr>
                            <tr style="border-bottom: 1px solid #fed7d7;"><td style="padding: 8px 0; color: #c53030;">Due Amount</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #c53030;">INR {order_amount:.2f}</td></tr>
                            <tr><td style="padding: 8px 0; color: #c53030;">Status</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #ef4444;">Failed / Suspended</td></tr>
                        </table>
                    </div>
                    
                    <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">To reactivate your workspace, please pay immediately here:</p>
                    <p style="margin-bottom: 25px;"><a href="https://payments.oqens.me/checkout?token={tenant['checkout_token']}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500;">Pay Invoice Now</a></p>
                    
                    <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">If you believe this is an error, please contact support.</p>
                </div>
                """
                send_system_email(email, subject, html_body)
        except Exception as e:
            print("Failed to send billing failure email:", e)
            
        html_failed = f"""<!DOCTYPE html>
<html>
<head>
    <title>Payment Failed | OQENS</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <script src="https://unpkg.com/@phosphor-icons/web"></script>
    <style>
        body {{ font-family: 'Inter', sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #fafafa; color: #111; }}
        .card {{ background: #fff; padding: 40px; border-radius: 12px; border: 1.5px solid #eee; text-align: center; max-width: 420px; box-shadow: 0 4px 20px rgba(0,0,0,0.03); }}
        .icon {{ font-size: 3.5rem; color: #ef4444; margin-bottom: 20px; }}
        h1 {{ font-weight: 500; font-size: 1.5rem; margin: 0 0 10px 0; letter-spacing: -0.3px; }}
        p {{ color: #666; font-size: 0.9rem; line-height: 1.5; margin: 0 0 25px 0; }}
        .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.88rem; font-weight: 500; width: 100%; box-sizing: border-box; transition: opacity 0.2s; }}
        .btn:hover {{ opacity: 0.9; }}
        .retry-link {{ display: block; margin-top: 15px; font-size: 0.85rem; color: #666; text-decoration: none; }}
        .retry-link:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <div class="card">
        <i class="ph ph-x-circle icon"></i>
        <h1>Payment Failed</h1>
        <p>Unfortunately, your payment verification failed. {error_msg}</p>
        <a href="https://payments.oqens.me/checkout?token={tenant['checkout_token']}" class="btn">Retry Payment <i class="ph ph-arrow-counter-clockwise"></i></a>
        <a href="https://echo.oqens.me/login" class="retry-link">Return to Login</a>
    </div>
</body>
</html>
"""
        return html_failed, 200

@app.route('/info/<int:tenant_id>')
def serve_tenant_info(tenant_id):
    if session.get('role') != 'admin':
        return redirect('https://auth2.oqens.me/')
    return app.send_static_file('tenant-info.html')

@app.route('/api/tenant/billing/toggle-auto-renew', methods=['POST'])
def tenant_toggle_auto_renew():
    if session.get('role') != 'tenant' or not session.get('tenant_id'):
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json or {}
    auto_renew = int(data.get('auto_renew', 1))
    if auto_renew not in [0, 1]:
        return jsonify({"error": "Invalid value for auto_renew"}), 400
        
    tenant_id = session['tenant_id']
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('UPDATE tenants SET auto_renew = ? WHERE id = ?', (auto_renew, tenant_id))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "auto_renew": auto_renew})

@app.route('/api/tenant/billing/transactions', methods=['GET'])
def tenant_transactions():
    if session.get('role') != 'tenant' or not session.get('tenant_id'):
        return jsonify({"error": "Unauthorized"}), 401
        
    tenant_id = session['tenant_id']
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM billing_transactions WHERE tenant_id = ? ORDER BY id DESC', (tenant_id,))
    txs = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify(txs)

@app.route('/api/admin/billing/run-cycle', methods=['POST'])
def admin_run_billing_cycle():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    try:
        processed, suspended = process_due_renewals()
        return jsonify({
            "status": "success",
            "processed": processed,
            "suspended": suspended
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/billing/transactions', methods=['GET'])
def admin_billing_transactions():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT tx.*, t.username as tenant_username, t.custom_domain_email as tenant_email 
                 FROM billing_transactions tx 
                 JOIN tenants t ON tx.tenant_id = t.id 
                 ORDER BY tx.id DESC''')
    txs = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify(txs)

@app.route('/api/admin/tenants/pending-payments', methods=['GET'])
def admin_pending_payments():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT id, username, custom_domain_email as email, monthly_rate, billing_status, next_billing_date 
                 FROM tenants 
                 WHERE billing_status IN ('Pending Payment', 'Pending Verification') 
                 ORDER BY id DESC''')
    tenants = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return jsonify(tenants)

@app.route('/api/admin/tenants/<int:tenant_id>/checkout-link', methods=['POST'])
def admin_generate_checkout_link(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,))
    tenant = c.fetchone()
    if not tenant:
        conn.close()
        return jsonify({"error": "Tenant not found"}), 404
        
    import secrets
    import datetime
    checkout_token = secrets.token_hex(12)
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(days=7)).isoformat()
    
    c.execute('UPDATE tenants SET checkout_token = ?, checkout_token_expires_at = ? WHERE id = ?', 
              (checkout_token, expires_at, tenant_id))
    conn.commit()
    conn.close()
    
    checkout_url = f"https://payments.oqens.me/checkout?token={checkout_token}"
    return jsonify({
        "status": "success",
        "username": tenant['username'],
        "secret_code": tenant['secret_code'],
        "checkout_url": checkout_url
    })

# --- Coupons & Offers APIs ---

@app.route('/api/admin/coupons', methods=['GET'])
def admin_get_coupons():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM coupons ORDER BY id DESC')
    coupons = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(coupons)

@app.route('/api/admin/coupons', methods=['POST'])
def admin_create_coupon():
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    code = data.get('code', '').strip().upper()
    discount_type = data.get('discount_type', 'percentage')
    discount_value = float(data.get('discount_value', 0))
    expiry_date = data.get('expiry_date', '').strip() or None
    max_uses = int(data.get('max_uses', -1))
    target_type = data.get('target_type', 'global').strip().lower()
    target_tenants = data.get('target_tenants', '').strip()
    target_plan = data.get('target_plan', 'all').strip().lower()
    
    if not code:
        return jsonify({"error": "Coupon code is required"}), 400
    if discount_type not in ['percentage', 'flat']:
        return jsonify({"error": "Invalid discount type"}), 400
    if discount_value <= 0:
        return jsonify({"error": "Discount value must be greater than zero"}), 400
    if target_type not in ['global', 'single', 'selected']:
        return jsonify({"error": "Invalid target type"}), 400
    if target_plan not in ['all', 'starter', 'pro']:
        return jsonify({"error": "Invalid target plan"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('''INSERT INTO coupons (code, discount_type, discount_value, expiry_date, max_uses, target_type, target_tenants, target_plan)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', (code, discount_type, discount_value, expiry_date, max_uses, target_type, target_tenants, target_plan))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "Coupon created successfully"})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Coupon code already exists"}), 400
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/coupons/<int:coupon_id>/toggle', methods=['POST'])
def admin_toggle_coupon(coupon_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT status FROM coupons WHERE id = ?', (coupon_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Coupon not found"}), 404
    new_status = 'Inactive' if row[0] == 'Active' else 'Active'
    c.execute('UPDATE coupons SET status = ? WHERE id = ?', (new_status, coupon_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "new_status": new_status})

@app.route('/api/admin/coupons/<int:coupon_id>', methods=['DELETE'])
def admin_delete_coupon(coupon_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM coupons WHERE id = ?', (coupon_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/admin/coupons/<int:coupon_id>/cancel-subscriptions', methods=['POST'])
def admin_cancel_coupon_subscriptions(coupon_id):
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized"}), 401
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT code FROM coupons WHERE id = ?', (coupon_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Coupon not found"}), 404
    coupon_code = row[0]
    
    # Find all transaction descriptions or payment IDs that match the coupon code
    pattern_desc1 = f'%coupon code {coupon_code}%'
    pattern_desc2 = f'%Coupon {coupon_code} applied%'
    pattern_payment = f'free_coupon_{coupon_code}'
    
    c.execute('''
        SELECT DISTINCT tenant_id FROM billing_transactions 
        WHERE description LIKE ? OR description LIKE ? OR payment_id = ?
    ''', (pattern_desc1, pattern_desc2, pattern_payment))
    
    tenant_ids = [r[0] for r in c.fetchall()]
    
    if not tenant_ids:
        conn.close()
        return jsonify({"status": "success", "message": "No tenants found who used this coupon.", "cancelled_count": 0})
        
    # Update billing status for all these tenants to Suspended
    placeholders = ','.join('?' for _ in tenant_ids)
    c.execute(f'UPDATE tenants SET billing_status = \'Suspended\' WHERE id IN ({placeholders})', tenant_ids)
    
    # Log a transaction/action for each tenant and send email
    import datetime
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    for tid in tenant_ids:
        c.execute('''
            INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email)
            VALUES (?, 0.0, ?, 'Cancelled', ?, 'admin_cancel', 'N/A', '')
        ''', (tid, today_str, f"Subscription cancelled/terminated coupon-wise (Coupon: {coupon_code}) by administrator."))
        
        # Fetch email & username to send notification
        conn_t = sqlite3.connect(DB_FILE)
        conn_t.row_factory = sqlite3.Row
        ct = conn_t.cursor()
        ct.execute('SELECT username, custom_domain_email, cloud_id FROM tenants WHERE id = ?', (tid,))
        row_t = ct.fetchone()
        conn_t.close()
        
        if row_t:
            tenant_email = row_t['custom_domain_email']
            if tenant_email:
                try:
                    subject = f"OQENS Workspace Suspended: Coupon '{coupon_code}' Revoked/Cancelled"
                    html_body = f"""
                    <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                        <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px; color: #ef4444;">Workspace Suspended</h2>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {row_t['username']},</p>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Your OQENS workspace (Cloud ID: <strong>{row_t['cloud_id']}</strong>) has been suspended immediately because the coupon code <strong>{coupon_code}</strong> has been cancelled or revoked by the administrator.</p>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">To restore your subscription access, please log in to your dashboard and complete payment for an active subscription plan.</p>
                        <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This is an automated notification from OQENS Billing.</p>
                    </div>
                    """
                    send_system_email(tenant_email, subject, html_body)
                except Exception as e:
                    print("Coupon cancellation email failed:", e)

    conn.commit()
    conn.close()
    
    return jsonify({
        "status": "success",
        "message": f"Successfully cancelled/terminated subscriptions for {len(tenant_ids)} tenants who used coupon '{coupon_code}'.",
        "cancelled_count": len(tenant_ids)
    })

@app.route('/api/payments/validate-coupon', methods=['GET'])
def payments_validate_coupon():
    code = request.args.get('code', '').strip().upper()
    token = request.args.get('token', '').strip()
    
    if not code or not token:
        return jsonify({"valid": False, "error": "Code and Token parameters are required."}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Verify tenant
    c.execute('SELECT username, monthly_rate, tier, checkout_token_expires_at FROM tenants WHERE checkout_token = ?', (token,))
    row_tenant = c.fetchone()
    if not row_tenant:
        conn.close()
        return jsonify({"valid": False, "error": "Workspace session not found."}), 404
    tenant = dict(row_tenant)
    monthly_rate = tenant['monthly_rate']
    username = tenant['username']
    
    import datetime
    expires_at_str = tenant.get("checkout_token_expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.datetime.fromisoformat(expires_at_str)
            if datetime.datetime.utcnow() > expires_at:
                conn.close()
                return jsonify({"valid": False, "error": "Checkout token has expired. Please request a new checkout link by verifying email or logging in again."}), 400
        except Exception:
            pass
    
    # Query coupon
    c.execute('SELECT * FROM coupons WHERE code = ?', (code,))
    row_coupon = c.fetchone()
    conn.close()
    
    if not row_coupon:
        return jsonify({"valid": False, "error": "Coupon code is invalid."}), 200
        
    coupon = dict(row_coupon)
    
    if coupon['status'] != 'Active':
        return jsonify({"valid": False, "error": "Coupon code is inactive."}), 200
        
    # Check targeting constraint
    c_target_type = coupon.get('target_type', 'global') or 'global'
    c_target_tenants = coupon.get('target_tenants', '') or ''
    if c_target_type == 'single':
        if username.strip().lower() != c_target_tenants.strip().lower():
            return jsonify({"valid": False, "error": "This coupon is only valid for a specific workspace."}), 200
    elif c_target_type == 'selected':
        allowed_users = [u.strip().lower() for u in c_target_tenants.split(',') if u.strip()]
        if username.strip().lower() not in allowed_users:
            return jsonify({"valid": False, "error": "This coupon is only valid for selected workspaces."}), 200
            
    # Check plan constraint
    c_target_plan = coupon.get('target_plan', 'all') or 'all'
    if c_target_plan != 'all':
        c_tier = row_tenant['tier'] or 'free'
        if c_tier.strip().lower() != c_target_plan.strip().lower():
            plan_cfg = get_plan_config(c_target_plan)
            if abs(monthly_rate - plan_cfg['rate']) > 0.01:
                return jsonify({"valid": False, "error": f"This coupon is only valid for the {c_target_plan.capitalize()} Plan."}), 200
        
    # Check expiry
    if coupon['expiry_date']:
        try:
            expiry = datetime.datetime.strptime(coupon['expiry_date'], '%Y-%m-%d').date()
            if datetime.date.today() > expiry:
                return jsonify({"valid": False, "error": "Coupon has expired."}), 200
        except Exception:
            pass
            
    # Check usage limit
    if coupon['max_uses'] != -1 and coupon['used_count'] >= coupon['max_uses']:
        return jsonify({"valid": False, "error": "Coupon usage limit reached."}), 200
        
    # Calculate discount
    discount_val = coupon['discount_value']
    if coupon['discount_type'] == 'percentage':
        discount_amount = (discount_val / 100.0) * monthly_rate
    else:
        discount_amount = discount_val
        
    discount_amount = min(discount_amount, monthly_rate)
    final_amount = max(0.0, monthly_rate - discount_amount)
    
    return jsonify({
        "valid": True,
        "code": code,
        "discount_type": coupon['discount_type'],
        "discount_value": discount_val,
        "discount_amount": discount_amount,
        "final_amount": final_amount
    })

def process_due_renewals():
    """Finds all tenants whose subscription renewal date has passed and processes them."""
    import datetime
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE next_billing_date <= ?', (today_str,))
    tenants = [dict(t) for t in c.fetchall()]
    
    processed = []
    suspended = []
    
    for t in tenants:
        tid = t['id']
        username = t['username']
        monthly_rate = t.get('monthly_rate', 0.0)
        auto_renew = t.get('auto_renew', 1)
        old_next_date = t.get('next_billing_date')
        email = t.get('custom_domain_email')
        cloud_id = t.get('cloud_id', '')
        checkout_token = t.get('checkout_token')
        
        if monthly_rate == 0:
            try:
                next_date = (datetime.datetime.strptime(old_next_date, '%Y-%m-%d').date() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
            except Exception:
                next_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
            c.execute('UPDATE tenants SET next_billing_date = ?, billing_status = \'Active\' WHERE id = ?', (next_date, tid))
            processed.append(username)
            continue
            
        if auto_renew == 1:
            try:
                next_date = (datetime.datetime.strptime(old_next_date, '%Y-%m-%d').date() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
            except Exception:
                next_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
                
            c.execute('UPDATE tenants SET next_billing_date = ?, billing_status = \'Active\' WHERE id = ?', (next_date, tid))
            c.execute('INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                      (tid, monthly_rate, today_str, 'Paid', 'Automatic monthly renewal subscription fee.', f"auto_renewal_{today_str}", 'N/A', email or ''))
            processed.append(username)
            
            # Send clean success receipt email
            try:
                if email:
                    subject = "OQENS Invoice: Monthly Subscription Renewed"
                    html_body = f"""
                    <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                        <h2 style="font-weight: 500; font-size: 1.4rem; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Invoice / Receipt</h2>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {username},</p>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Your payment has been successfully processed. Here is the receipt for your monthly subscription:</p>
                        
                        <div style="background: #fafafa; border: 1px solid #eeeeee; border-radius: 6px; padding: 20px; margin: 25px 0; font-size: 0.9rem;">
                            <table style="width: 100%; border-collapse: collapse;">
                                <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Cloud ID</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">{cloud_id}</td></tr>
                                <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Amount Paid</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #10b981;">INR {monthly_rate:.2f}</td></tr>
                                <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Status</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #10b981;">Successful / Paid</td></tr>
                                <tr style="border-bottom: 1px solid #eeeeee;"><td style="padding: 8px 0; color: #666;">Next Renewal Date</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">{next_date}</td></tr>
                                <tr><td style="padding: 8px 0; color: #666;">Payment Method</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #111;">Cashfree Gateway (Auto-Debit)</td></tr>
                            </table>
                        </div>
                        
                        <p style="font-size: 0.9rem; color: #666; line-height: 1.5; margin-bottom: 25px;">You can access your workspace here: <a href="https://echo.oqens.me/dash" style="color: #111; font-weight: 500; text-decoration: underline;">OQENS Dashboard</a></p>
                        <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">This is an automated receipt. If you have any questions, please contact support.</p>
                    </div>
                    """
                    import base64
                    pdf_data = generate_invoice_pdf(t, monthly_rate, today_str)
                    attachments = None
                    if pdf_data:
                        pdf_b64 = base64.b64encode(pdf_data).decode('utf-8')
                        attachments = [
                            {
                                'filename': 'invoice.pdf',
                                'content': pdf_b64,
                                'contentType': 'application/pdf'
                            }
                        ]
                    send_system_email(email, subject, html_body, attachments=attachments)
            except Exception as e:
                print("Failed to send billing email inside process_due_renewals:", e)
        else:
            try:
                next_date = (datetime.datetime.strptime(old_next_date, '%Y-%m-%d').date() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
            except Exception:
                next_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
                
            c.execute('UPDATE tenants SET next_billing_date = ?, billing_status = \'Suspended\' WHERE id = ?', (next_date, tid))
            c.execute('INSERT INTO billing_transactions (tenant_id, amount, date, status, description, payment_id, utr, email) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                      (tid, monthly_rate, today_str, 'Failed', 'Monthly subscription renewal failed: Auto-renewal disabled.', f"auto_renewal_failed_{today_str}", 'N/A', email or ''))
            suspended.append(username)
            
            # Send warning email
            try:
                if email:
                    subject = "OQENS Alert: Subscription Renewal Failed"
                    html_body = f"""
                    <div style="font-family: 'Inter', -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; background: #ffffff; color: #111111; border: 1px solid #eeeeee; border-radius: 8px;">
                        <h2 style="font-weight: 500; font-size: 1.4rem; color: #ef4444; border-bottom: 1.5px solid #eeeeee; padding-bottom: 15px; margin-bottom: 25px; letter-spacing: -0.3px;">OQENS Payment Failed</h2>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">Hi {username},</p>
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">We were unable to process your monthly subscription payment of <strong>INR {monthly_rate:.2f}</strong>. As a result, your workspace has been temporarily suspended.</p>
                        
                        <div style="background: #fff5f5; border: 1px solid #fed7d7; border-radius: 6px; padding: 20px; margin: 25px 0; font-size: 0.9rem;">
                            <table style="width: 100%; border-collapse: collapse;">
                                <tr style="border-bottom: 1px solid #fed7d7;"><td style="padding: 8px 0; color: #c53030;">Cloud ID</td><td style="padding: 8px 0; text-align: right; font-weight: 500; color: #c53030;">{cloud_id}</td></tr>
                                <tr style="border-bottom: 1px solid #fed7d7;"><td style="padding: 8px 0; color: #c53030;">Due Amount</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #c53030;">INR {monthly_rate:.2f}</td></tr>
                                <tr><td style="padding: 8px 0; color: #c53030;">Status</td><td style="padding: 8px 0; text-align: right; font-weight: 600; color: #ef4444;">Failed / Suspended</td></tr>
                            </table>
                        </div>
                        
                        <p style="font-size: 0.95rem; color: #444; line-height: 1.5;">To reactivate your workspace, please pay immediately here:</p>
                        <p style="margin-bottom: 25px;"><a href="https://payments.oqens.me/checkout?token={checkout_token}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500;">Pay Invoice Now</a></p>
                        
                        <p style="font-size: 0.85rem; color: #999; border-top: 1.5px solid #eeeeee; padding-top: 15px; margin-top: 25px;">If you believe this is an error, please contact support.</p>
                    </div>
                    """
                    send_system_email(email, subject, html_body)
            except Exception as e:
                print("Failed to send billing failure email inside process_due_renewals:", e)
            
    conn.commit()
    conn.close()
    return processed, suspended

def start_master_scheduler():
    import time
    import threading
    import datetime
    import json
    import urllib.request

    def scheduler_loop():
        time.sleep(30)
        last_billing_run_date = None
        
        while True:
            try:
                now_utc = datetime.datetime.utcnow()
                now_ist = now_utc + datetime.timedelta(hours=5, minutes=30)
                is_off_peak = 2 <= now_ist.hour < 4
                
                # 1. Run Billing Scheduler (Once per day during off-peak IST)
                if is_off_peak:
                    today_str = now_ist.strftime('%Y-%m-%d')
                    if last_billing_run_date != today_str:
                        try:
                            process_due_renewals()
                            expire_referral_benefits()
                            last_billing_run_date = today_str
                        except Exception as e:
                            print("Error in daily billing run:", e)
                            
                # 2. Process Scheduled Tasks
                conn = sqlite3.connect(DB_FILE)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                
                c.execute('''SELECT id, task_type, payload FROM scheduled_tasks 
                             WHERE status = 'Pending' AND execute_after <= CURRENT_TIMESTAMP 
                             ORDER BY execute_after ASC LIMIT 1''')
                task = c.fetchone()
                
                if task:
                    task_id = task['id']
                    task_type = task['task_type']
                    payload_data = json.loads(task['payload'])
                    
                    c.execute("UPDATE scheduled_tasks SET status = 'Processing' WHERE id = ?", (task_id,))
                    conn.commit()
                    conn.close()
                    
                    try:
                        if task_type == 'mailman_bulk':
                            execute_mailman_bulk_task(payload_data)
                            
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute("UPDATE scheduled_tasks SET status = 'Completed' WHERE id = ?", (task_id,))
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"Error executing task {task_id}:", e)
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        c.execute("UPDATE scheduled_tasks SET status = 'Failed' WHERE id = ?", (task_id,))
                        conn.commit()
                        conn.close()
                else:
                    conn.close()
            except Exception as e:
                print("Error in master scheduler loop:", e)
            
            time.sleep(60)
            
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()

start_master_scheduler()

# --- SYSTEM STATUS HEALTH CHECK ROUTINES ---

@app.route('/api/health-check', methods=['GET'])
def system_health_check_endpoint():
    return jsonify({"status": "healthy"})

@app.route('/api/status', methods=['GET'])
def get_system_status_api():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # 1. Get current status (last metric row)
    c.execute('SELECT * FROM status_metrics ORDER BY id DESC LIMIT 1')
    last_row = c.fetchone()
    current = dict(last_row) if last_row else {
        "api_status": "operational",
        "cos_status": "operational",
        "nginx_status": "operational",
        "db_status": "operational",
        "checked_at": None
    }
    
    # 2. Get 90 days timeline
    ninety_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).isoformat()
    c.execute('''
        SELECT SUBSTR(checked_at, 1, 10) as day,
               MAX(CASE api_status WHEN 'operational' THEN 0 WHEN 'degraded' THEN 1 WHEN 'outage' THEN 2 ELSE 0 END) as api_val,
               MAX(CASE cos_status WHEN 'operational' THEN 0 WHEN 'degraded' THEN 1 WHEN 'outage' THEN 2 ELSE 0 END) as cos_val,
               MAX(CASE nginx_status WHEN 'operational' THEN 0 WHEN 'degraded' THEN 1 WHEN 'outage' THEN 2 ELSE 0 END) as nginx_val,
               MAX(CASE db_status WHEN 'operational' THEN 0 WHEN 'degraded' THEN 1 WHEN 'outage' THEN 2 ELSE 0 END) as db_val
        FROM status_metrics
        WHERE checked_at >= ?
        GROUP BY day
        ORDER BY day ASC
    ''', (ninety_days_ago,))
    
    rows = c.fetchall()
    val_map = {0: 'operational', 1: 'degraded', 2: 'outage'}
    
    timeline = {}
    for r in rows:
        timeline[r['day']] = {
            "api": val_map.get(r['api_val'], 'operational'),
            "cos": val_map.get(r['cos_val'], 'operational'),
            "nginx": val_map.get(r['nginx_val'], 'operational'),
            "db": val_map.get(r['db_val'], 'operational')
        }
        
    # 3. Get incidents
    c.execute('SELECT * FROM status_incidents ORDER BY id DESC LIMIT 50')
    incidents = [dict(r) for r in c.fetchall()]
    
    conn.close()
    
    return jsonify({
        "current": current,
        "timeline": timeline,
        "incidents": incidents
    })

def perform_system_checks():
    import urllib.request
    import json
    import datetime
    
    # 1. Check Database Service
    db_ok = False
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT 1")
        c.fetchone()
        conn.close()
        db_ok = True
    except Exception:
        pass
    db_status = "operational" if db_ok else "outage"
    
    # 2. Check API Gateway
    api_ok = False
    try:
        req = urllib.request.Request("http://127.0.0.1:5000/api/health-check", headers={"Host": "auth.oqens.me"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                api_ok = True
    except Exception:
        pass
    api_status = "operational" if api_ok else "outage"
    
    # 3. Check Nginx Ingress via TCP socket
    nginx_ok = False
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 80))
        s.close()
        nginx_ok = True
    except Exception:
        pass
    nginx_status = "operational" if nginx_ok else "outage"
    
    # 4. Check COS Engine (VM2) via port 80 proxy
    cos_ok = False
    try:
        import os
        cos_ip = "127.0.0.1"
        try:
            conn_cfg = sqlite3.connect(DB_FILE)
            c_cfg = conn_cfg.cursor()
            c_cfg.execute("SELECT value FROM system_config WHERE key = 'cos_engine_ip'")
            row_cfg = c_cfg.fetchone()
            conn_cfg.close()
            if row_cfg and row_cfg[0]:
                cos_ip = row_cfg[0]
        except Exception:
            pass
        cos_ip = os.environ.get("COS_ENGINE_IP", cos_ip)

        req = urllib.request.Request(f"http://{cos_ip}/api/system")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode('utf-8'))
                if "cpu" in data:
                    cos_ok = True
    except Exception:
        pass
    cos_status = "operational" if cos_ok else "outage"
    
    now_str = datetime.datetime.utcnow().isoformat()
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Get last statuses to check for transitions
    c.execute('SELECT api_status, cos_status, nginx_status, db_status FROM status_metrics ORDER BY id DESC LIMIT 1')
    last_row = c.fetchone()
    
    if last_row:
        last_api, last_cos, last_nginx, last_db = last_row
    else:
        last_api, last_cos, last_nginx, last_db = "operational", "operational", "operational", "operational"
        
    components = [
        ("api", "API Gateway & Dashboard", api_status, last_api),
        ("cos", "Custom Origin Storage Engine (COS)", cos_status, last_cos),
        ("nginx", "Nginx Ingress Proxy Routing", nginx_status, last_nginx),
        ("db", "Database Services", db_status, last_db)
    ]
    
    for comp_id, comp_name, current_st, last_st in components:
        if last_st == "operational" and current_st == "outage":
            # Outage detected -> Add Incident
            c.execute('''INSERT INTO status_incidents (title, description, status, created_at)
                         VALUES (?, ?, ?, ?)''',
                      (f"Outage Detected: {comp_name}",
                       f"Our automated monitoring system has detected that the {comp_name} is currently offline or unresponsive. Our engineering team is investigating.",
                       "Investigating", now_str))
        elif last_st == "outage" and current_st == "operational":
            # Outage resolved -> Resolve Incident
            c.execute('''SELECT id FROM status_incidents WHERE title = ? AND status != 'Resolved' ORDER BY id DESC LIMIT 1''',
                      (f"Outage Detected: {comp_name}",))
            inc_row = c.fetchone()
            if inc_row:
                c.execute('''UPDATE status_incidents SET status = 'Resolved', description = ?, resolved_at = ? WHERE id = ?''',
                          (f"The issues affecting {comp_name} have been resolved, and the service is now fully operational. Thank you for your patience.", now_str, inc_row[0]))
                
    # Log the metrics
    c.execute('''INSERT INTO status_metrics (checked_at, api_status, cos_status, nginx_status, db_status)
                 VALUES (?, ?, ?, ?, ?)''', (now_str, api_status, cos_status, nginx_status, db_status))
    
    # Prune metrics older than 90 days
    ninety_days_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).isoformat()
    c.execute('DELETE FROM status_metrics WHERE checked_at < ?', (ninety_days_ago,))
    
    conn.commit()
    conn.close()

def start_health_checker():
    import threading
    import time
    def checker_loop():
        # Wait a moment for app to fully boot
        time.sleep(15)
        while True:
            try:
                perform_system_checks()
            except Exception as e:
                print("Error performing system status checks:", e)
            time.sleep(300) # Check every 5 minutes
            
    thread = threading.Thread(target=checker_loop, daemon=True)
    thread.start()

start_health_checker()

# ─── Referral System ──────────────────────────────────────────────────────────

import hashlib as _hashlib

def _referral_device_fingerprint(req):
    """SHA-256 of IP + User-Agent + Accept-Language — server-side only."""
    ip = req.headers.get('X-Forwarded-For', req.remote_addr or '').split(',')[0].strip()
    ua = req.headers.get('User-Agent', '')
    al = req.headers.get('Accept-Language', '')
    raw = f"{ip}|{ua}|{al}"
    return _hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _get_referral(code):
    """Fetch a referral link by code. Returns dict or None."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM referral_links WHERE code = ?', (code,))
    row = c.fetchone()
    conn.close()
    if row:
        r = dict(row)
        if r.get('status'):
            r['status'] = r['status'].strip('"\'')
        if r.get('granted_tier'):
            r['granted_tier'] = r['granted_tier'].strip('"\'')
        return r
    return None

def _referral_valid(ref):
    """Return True if referral is still usable."""
    if not ref:
        return False
    status = ref.get('status')
    if status:
        status = status.strip('"\'')
    if status != 'Active':
        return False
    if ref.get('link_expires_at'):
        try:
            exp = datetime.datetime.strptime(ref['link_expires_at'], '%Y-%m-%d').date()
            if datetime.date.today() > exp:
                return False
        except Exception:
            pass
    max_uses = ref.get('max_uses', -1)
    if max_uses != -1 and ref.get('used_count', 0) >= max_uses:
        return False
    return True

@app.route('/ref/<code>')
def referral_entry(code):
    """
    Referral entry point. Sets a secure HttpOnly cookie then redirects to
    /signup with a clean URL — the code never appears in the destination URL.
    """
    ref = _get_referral(code)
    # Always redirect to signup — never reveal whether code is valid
    response = redirect('https://echo.oqens.me/signup')
    if ref and _referral_valid(ref):
        response.set_cookie(
            '_oqref', code,
            max_age=7 * 24 * 3600,   # 7 days
            httponly=True,
            secure=True,
            samesite='Lax',
            path='/'
        )
    return response

def _apply_referral_to_tenant(tenant_id, username, conn, c):
    """
    Called immediately after a new tenant row is committed.
    Reads _oqref cookie, validates, applies grants, records use.
    Cookie is cleared via response header after this.
    """
    code = request.cookies.get('_oqref')
    if not code:
        return False

    ref = _get_referral(code)
    if not ref or not _referral_valid(ref):
        return False

    # Device fingerprint duplicate check
    fingerprint = _referral_device_fingerprint(request)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()

    c.execute('SELECT id FROM referral_uses WHERE referral_code = ? AND device_fingerprint = ?',
              (code, fingerprint))
    if c.fetchone():
        return False  # Same device already used this referral

    # Resolve granted limits
    granted_tier = ref.get('granted_tier') or 'starter'
    display_tier = ref.get('display_tier') or None  # Custom name shown to client

    if granted_tier == 'free':
        storage_bytes = 100 * 1024 * 1024
        bandwidth_bytes = 500 * 1024 * 1024
        monthly_rate = 0.0
    elif granted_tier == 'writer':
        cfg = get_plan_config('writer')
        storage_bytes = cfg['storage_limit']
        bandwidth_bytes = cfg['bandwidth_limit']
        monthly_rate = cfg['rate']
    elif granted_tier == 'starter':
        cfg = get_plan_config('starter')
        storage_bytes = cfg['storage_limit']
        bandwidth_bytes = cfg['bandwidth_limit']
        monthly_rate = cfg['rate']
    else:  # pro
        cfg = get_plan_config('pro')
        storage_bytes = cfg['storage_limit']
        bandwidth_bytes = cfg['bandwidth_limit']
        monthly_rate = cfg['rate']

    # Override storage/bandwidth if host specified custom limits
    if ref.get('granted_storage_gb') and float(ref['granted_storage_gb']) > 0:
        storage_bytes = int(float(ref['granted_storage_gb']) * 1024**3)
    if ref.get('granted_bandwidth_gb') and float(ref['granted_bandwidth_gb']) > 0:
        bandwidth_bytes = int(float(ref['granted_bandwidth_gb']) * 1024**3)

    # Compute benefit expiry
    duration_days = ref.get('granted_duration_days', -1)
    benefit_expires_at = None
    if duration_days and int(duration_days) > 0:
        benefit_expires_at = (datetime.date.today() + datetime.timedelta(days=int(duration_days))).strftime('%Y-%m-%d')

    now_str = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    # Apply grants to tenant
    c.execute('''UPDATE tenants SET
        tier = ?,
        display_tier = ?,
        storage_limit_bytes = ?,
        bandwidth_limit_bytes = ?,
        monthly_rate = ?,
        billing_status = \'Active\',
        referral_code = ?,
        referral_benefit_expires = ?
        WHERE id = ?''',
        (granted_tier, display_tier, storage_bytes, bandwidth_bytes,
         monthly_rate, code, benefit_expires_at, tenant_id))

    # Record use
    c.execute('''INSERT INTO referral_uses
        (referral_code, tenant_id, tenant_username, device_fingerprint, ip_address, activated_at, benefit_expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (code, tenant_id, username, fingerprint, ip, now_str, benefit_expires_at))

    # Increment used_count
    c.execute('UPDATE referral_links SET used_count = used_count + 1 WHERE code = ?', (code,))
    conn.commit()
    return True

# ── Admin Referral APIs ────────────────────────────────────────────────────────

@app.route('/api/admin/payments/<int:tenant_id>/approve', methods=['POST'])
def admin_payments_approve(tenant_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Tenant not found'}), 404
    tenant = dict(row)
    
    if tenant['billing_status'] != 'Pending Verification':
        conn.close()
        return jsonify({'error': 'Tenant is not pending manual verification.'}), 400
        
    import datetime
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
    
    # Upgrade limits upon manual approval
    tier = tenant.get('tier', 'free') or 'free'
    plan_cfg = get_plan_config(tier)
    c.execute('''UPDATE tenants SET billing_status = 'Active', next_billing_date = ?, checkout_token = NULL, checkout_token_expires_at = NULL,
                 storage_limit_bytes = ?, bandwidth_limit_bytes = ?, monthly_rate = ? WHERE id = ?''', 
              (next_billing_date, plan_cfg['storage_limit'], plan_cfg['bandwidth_limit'], plan_cfg['rate'], tenant_id))
    
    c.execute('''UPDATE billing_transactions SET status = 'Success', date = ?, description = description || ' (Approved by Admin)' 
                 WHERE tenant_id = ? AND status = 'Pending Verification' ''', (today_str, tenant_id))
                 
    conn.commit()
    conn.close()
    
    # Generate PDF Invoice and Send Email
    import base64
    attachments = None
    try:
        rate = float(tenant.get('monthly_rate', 0.0) or 0.0)
        pdf_data = generate_invoice_pdf(tenant, rate, today_str)
        if pdf_data:
            pdf_b64 = base64.b64encode(pdf_data).decode('utf-8')
            attachments = [{
                'filename': 'invoice.pdf',
                'content': pdf_b64,
                'contentType': 'application/pdf'
            }]
    except Exception as e:
        print("Error generating PDF invoice:", e)
        
    # Send Email
    subject = "Payment Verified & Invoice - OQENS"
    html_body = f"""
    <div style="font-family: 'Inter', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #0f172a; line-height: 1.5;">
        <h2 style="color: #10b981; font-weight: 600; margin-bottom: 15px;">Payment Verified Successfully</h2>
        <p>Hi {tenant['username']},</p>
        <p>Your manual subscription payment has been successfully verified by the administrator. Your account status is now updated to <strong>Active</strong>.</p>
        <p>We have attached the official PDF invoice / payment receipt to this email for your records.</p>
        <p style="margin-top: 25px; border-top: 1px solid #e2e8f0; padding-top: 15px; font-size: 0.88rem; color: #64748b;">
            Best regards,<br>
            <strong>OQENS Team</strong>
        </p>
    </div>
    """
    email = tenant.get('custom_domain_email') or tenant.get('email', '')
    if not email:
        print(f"[WARN] admin_payments_approve: No email found for tenant {tenant_id}, skipping invoice email.")
    else:
        send_system_email(email, subject, html_body, attachments=attachments)
    
    return jsonify({'status': 'success'})

@app.route('/api/admin/payments/transactions/<int:tx_id>/resend-invoice', methods=['POST'])
def admin_resend_invoice(tx_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('SELECT * FROM billing_transactions WHERE id = ?', (tx_id,))
    tx_row = c.fetchone()
    if not tx_row:
        conn.close()
        return jsonify({'error': 'Transaction not found'}), 404
        
    tx = dict(tx_row)
    tenant_id = tx['tenant_id']
    
    c.execute('SELECT * FROM tenants WHERE id = ?', (tenant_id,))
    tenant_row = c.fetchone()
    if not tenant_row:
        conn.close()
        return jsonify({'error': 'Tenant not found'}), 404
        
    tenant = dict(tenant_row)
    conn.close()
    
    # Generate PDF Invoice and Send Email
    import base64
    attachments = None
    tx_date = tx.get('date') or 'N/A'
    tx_amount = float(tx.get('amount', 0.0) or 0.0)
    
    try:
        # Generate the PDF with the amount of this specific transaction, using its date
        pdf_data = generate_invoice_pdf(tenant, tx_amount, tx_date)
        if pdf_data:
            pdf_b64 = base64.b64encode(pdf_data).decode('utf-8')
            attachments = [{
                'filename': 'invoice.pdf',
                'content': pdf_b64,
                'contentType': 'application/pdf'
            }]
    except Exception as e:
        print("Error generating PDF invoice for resend:", e)
        
    # Send Email
    subject = "Invoice Copy - OQENS"
    html_body = f"""
    <div style="font-family: 'Inter', sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #0f172a; line-height: 1.5;">
        <h2 style="color: #0284c7; font-weight: 600; margin-bottom: 15px;">Invoice / Payment Receipt Copy</h2>
        <p>Hi {tenant['username']},</p>
        <p>As requested, we have attached a copy of the official PDF invoice for your transaction on <strong>{tx_date}</strong> (Amount: INR {tx_amount:.2f}).</p>
        <p>Your subscription is currently <strong>{tenant.get('billing_status', 'N/A')}</strong>.</p>
        <p style="margin-top: 25px; border-top: 1px solid #e2e8f0; padding-top: 15px; font-size: 0.88rem; color: #64748b;">
            Best regards,<br>
            <strong>OQENS Team</strong>
        </p>
    </div>
    """
    
    # We should send the email to the tenant's registered email
    email = tenant.get('custom_domain_email') or tenant.get('email', '')
    if not email:
        return jsonify({'error': 'No email address found for this tenant.'}), 400
        
    try:
        send_system_email(email, subject, html_body, attachments=attachments)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'error': f"Failed to send email: {str(e)}"}), 500

@app.route('/api/admin/referrals', methods=['GET'])
def admin_referrals_list():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM referral_links ORDER BY created_at DESC')
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    for r in rows:
        if r.get('status'):
            r['status'] = r['status'].strip('"\'')
        if r.get('granted_tier'):
            r['granted_tier'] = r['granted_tier'].strip('"\'')
    return jsonify(rows)

@app.route('/api/admin/referrals', methods=['POST'])
def admin_referrals_create():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}

    label = (data.get('label') or '').strip()
    if not label:
        return jsonify({'error': 'Label is required'}), 400

    granted_tier = data.get('granted_tier', 'starter').lower()
    if granted_tier not in ['free', 'writer', 'starter', 'pro']:
        return jsonify({'error': 'Invalid tier'}), 400

    display_tier = (data.get('display_tier') or '').strip() or None
    granted_storage_gb = data.get('granted_storage_gb') or None
    granted_bandwidth_gb = data.get('granted_bandwidth_gb') or None
    granted_duration_days = int(data.get('granted_duration_days', -1))
    max_uses = int(data.get('max_uses', -1))
    link_expires_at = data.get('link_expires_at') or None

    custom_code = (data.get('code') or '').strip()
    import secrets as _sec
    
    if custom_code:
        code = custom_code
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT id FROM referral_links WHERE code = ?', (code,))
        if c.fetchone():
            conn.close()
            return jsonify({'error': 'Custom code already exists'}), 400
        conn.close()
    else:
        # Generate unique 12-char alphanumeric code
        while True:
            code = _sec.token_urlsafe(9)[:12]
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT id FROM referral_links WHERE code = ?', (code,))
            if not c.fetchone():
                break
            conn.close()

    now_str = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''INSERT INTO referral_links
        (code, label, granted_tier, display_tier, granted_storage_gb, granted_bandwidth_gb,
         granted_duration_days, max_uses, link_expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (code, label, granted_tier, display_tier, granted_storage_gb, granted_bandwidth_gb,
         granted_duration_days, max_uses, link_expires_at, now_str))
    conn.commit()
    link_id = c.lastrowid
    conn.close()

    return jsonify({
        'status': 'success',
        'id': link_id,
        'code': code,
        'url': f'https://echo.oqens.me/ref/{code}'
    })

@app.route('/api/admin/referrals/<int:ref_id>', methods=['PUT'])
def admin_referrals_update(ref_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json or {}
    code = (data.get('code') or '').strip()
    label = (data.get('label') or '').strip()
    granted_tier = data.get('granted_tier', 'starter').lower()
    granted_storage_gb = data.get('granted_storage_gb') or None
    granted_bandwidth_gb = data.get('granted_bandwidth_gb') or None
    granted_duration_days = int(data.get('granted_duration_days', -1))
    status = data.get('status', 'Active')
    
    if not code or not label:
        return jsonify({'error': 'Code and Label are required'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Check if code is taken by another campaign
    c.execute('SELECT id FROM referral_links WHERE code = ? AND id != ?', (code, ref_id))
    if c.fetchone():
        conn.close()
        return jsonify({'error': 'Code is already in use by another campaign'}), 400
        
    c.execute('''UPDATE referral_links SET
                 code = ?, label = ?, granted_tier = ?, granted_storage_gb = ?,
                 granted_bandwidth_gb = ?, granted_duration_days = ?, status = ?
                 WHERE id = ?''', 
              (code, label, granted_tier, granted_storage_gb, granted_bandwidth_gb, 
               granted_duration_days, status, ref_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/referrals/<int:ref_id>/toggle', methods=['POST'])
def admin_referrals_toggle(ref_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT status FROM referral_links WHERE id = ?', (ref_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    new_status = 'Disabled' if row['status'] == 'Active' else 'Active'
    c.execute('UPDATE referral_links SET status = ? WHERE id = ?', (new_status, ref_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'new_status': new_status})

@app.route('/api/admin/referrals/<int:ref_id>', methods=['DELETE'])
def admin_referrals_delete(ref_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM referral_links WHERE id = ?', (ref_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/referrals/<int:ref_id>/uses', methods=['GET'])
def admin_referrals_uses(ref_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT code FROM referral_links WHERE id = ?', (ref_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    code = row['code']
    c.execute('''SELECT tenant_username, ip_address, activated_at, benefit_expires_at
                 FROM referral_uses WHERE referral_code = ? ORDER BY activated_at DESC''', (code,))
    uses = []
    for r in c.fetchall():
        ip = r['ip_address'] or ''
        # Mask last octet: x.x.x.*
        parts = ip.split('.')
        masked_ip = '.'.join(parts[:3] + ['*']) if len(parts) == 4 else ip[:8] + '***'
        uses.append({
            'tenant_username': r['tenant_username'],
            'ip_address': masked_ip,
            'activated_at': r['activated_at'],
            'benefit_expires_at': r['benefit_expires_at']
        })
    conn.close()
    return jsonify(uses)

# ── Referral benefit expiry (called from billing scheduler) ───────────────────

def expire_referral_benefits():
    """Downgrade tenants whose referral benefit has expired."""
    today = datetime.date.today().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''SELECT id FROM tenants
                 WHERE referral_benefit_expires IS NOT NULL
                 AND referral_benefit_expires != ''
                 AND referral_benefit_expires < ?''', (today,))
    expired = c.fetchall()
    for row in expired:
        tid = row['id']
        # Revert to free plan
        free_storage = 100 * 1024 * 1024
        free_bandwidth = 500 * 1024 * 1024
        c.execute('''UPDATE tenants SET
            tier = 'free',
            display_tier = NULL,
            storage_limit_bytes = ?,
            bandwidth_limit_bytes = ?,
            monthly_rate = 0.0,
            billing_status = \'Active\',
            referral_benefit_expires = NULL
            WHERE id = ?''', (free_storage, free_bandwidth, tid))
    if expired:
        conn.commit()
    conn.close()
    return len(expired)



# ─── Markdown Pages API (Writer Plan) ────────────────────────────────────────

import re as _re

def _slugify(text):
    text = text.lower().strip()
    text = _re.sub(r'[^\w\s-]', '', text)
    text = _re.sub(r'[\s_-]+', '-', text)
    return text[:80]

@app.route('/api/payments/manual-order', methods=['POST'])
def payments_manual_order():
    data = request.json or {}
    token = data.get('token')
    
    if not token:
        return jsonify({"error": "Checkout token is required"}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tenants WHERE checkout_token = ?', (token,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Tenant not found"}), 404
    tenant = dict(row)
    tenant_id = tenant['id']
    
    conn.close()
    
    amount = tenant.get('monthly_rate', 0.0)
    if amount <= 0:
        return jsonify({"error": "This tenant has a free plan. No payment required."}), 400
        
    coupon_code = data.get('coupon', '').strip().upper()
    coupon_applied = None
    if coupon_code:
        import datetime
        conn_c = sqlite3.connect(DB_FILE)
        conn_c.row_factory = sqlite3.Row
        cc = conn_c.cursor()
        cc.execute('SELECT * FROM coupons WHERE code = ?', (coupon_code,))
        row_c = cc.fetchone()
        if row_c:
            coupon = dict(row_c)
            is_valid = True
            if coupon['status'] != 'Active':
                is_valid = False
            if coupon['expiry_date']:
                try:
                    expiry = datetime.datetime.strptime(coupon['expiry_date'], '%Y-%m-%d').date()
                    if datetime.date.today() > expiry:
                        is_valid = False
                except Exception:
                    pass
            if coupon['max_uses'] != -1 and coupon['used_count'] >= coupon['max_uses']:
                is_valid = False
                
            # Check targeting constraint
            c_target_type = coupon.get('target_type', 'global') or 'global'
            c_target_tenants = coupon.get('target_tenants', '') or ''
            tenant_username = tenant.get('username', '')
            if c_target_type == 'single':
                if tenant_username.strip().lower() != c_target_tenants.strip().lower():
                    is_valid = False
            elif c_target_type == 'selected':
                allowed_users = [u.strip().lower() for u in c_target_tenants.split(',') if u.strip()]
                if tenant_username.strip().lower() not in allowed_users:
                    is_valid = False
                    
            # Check plan constraint
            c_target_plan = coupon.get('target_plan', 'all') or 'all'
            if c_target_plan != 'all':
                c_tier = tenant.get('tier', 'free') or 'free'
                if c_tier.strip().lower() != c_target_plan.strip().lower():
                    plan_cfg = get_plan_config(c_target_plan)
                    if abs(amount - plan_cfg['rate']) > 0.01:
                        is_valid = False
                
            if is_valid:
                discount_val = coupon['discount_value']
                if coupon['discount_type'] == 'percentage':
                    discount_amount = (discount_val / 100.0) * amount
                else:
                    discount_amount = discount_val
                discount_amount = min(discount_amount, amount)
                amount = max(0.0, amount - discount_amount)
                coupon_applied = coupon_code
        conn_c.close()

    if amount <= 0.0:
        import datetime
        import secrets
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        next_billing_date = (datetime.date.today() + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        
        conn_act = sqlite3.connect(DB_FILE)
        c_act = conn_act.cursor()
        c_act.execute('UPDATE tenants SET billing_status = \'Active\', next_billing_date = ?, checkout_token = NULL, checkout_token_expires_at = NULL WHERE id = ?', (next_billing_date, tenant_id))
        
        order_id = "FREE_" + secrets.token_hex(8)
        
        c_act.execute('''INSERT INTO billing_transactions 
            (tenant_id, amount, date, status, description, payment_id, utr, email) 
            VALUES (?, ?, ?, 'Success', ?, ?, 'FREE_ACTIVATION', ?)''',
            (tenant_id, amount, today_str, f"Activated with 100% off coupon {coupon_applied}", order_id, tenant.get('custom_domain_email','')))
            
        if coupon_applied:
            c_act.execute('UPDATE coupons SET used_count = used_count + 1 WHERE code = ?', (coupon_applied,))
            
        conn_act.commit()
        conn_act.close()
        return jsonify({"status": "free_activation", "payment_session_id": "FREE"})
    else:
        import datetime
        import secrets
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        
        conn_act = sqlite3.connect(DB_FILE)
        c_act = conn_act.cursor()
        c_act.execute('UPDATE tenants SET billing_status = \'Pending Verification\' WHERE id = ?', (tenant_id,))
        
        order_id = "MANUAL_" + secrets.token_hex(8)
        desc = f"Manual Cash Pay pending verification"
        if coupon_applied:
            desc += f" (Coupon: {coupon_applied})"
            
        c_act.execute('''INSERT INTO billing_transactions 
            (tenant_id, amount, date, status, description, payment_id, utr, email) 
            VALUES (?, ?, ?, 'Pending Verification', ?, ?, 'MANUAL', ?)''',
            (tenant_id, amount, today_str, desc, order_id, tenant.get('custom_domain_email','')))
            
        if coupon_applied:
            c_act.execute('UPDATE coupons SET used_count = used_count + 1 WHERE code = ?', (coupon_applied,))
            
        conn_act.commit()
        conn_act.close()
        
        return jsonify({"status": "pending_verification"})



def _get_page_limit(tenant):
    """Return max pages allowed based on tenant tier or custom granted limit."""
    if tenant.get('granted_pages_limit') is not None:
        return int(tenant['granted_pages_limit'])
        
    tier = (tenant.get('tier') or 'free').lower()
    if tier == 'writer':
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT value FROM system_config WHERE key = \'plan_writer_pages\'')
        row = c.fetchone()
        conn.close()
        return int(row[0]) if row else 10
    elif tier == 'starter':
        return 50
    elif tier == 'pro':
        return 100
    return 0  # free plan — no pages

@app.route('/api/pages', methods=['GET', 'POST'])
def pages_collection():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    username = tenant['username']

    if request.method == 'GET':
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT id, title, slug, is_published, created_at, updated_at, collection_id, custom_background FROM markdown_pages WHERE tenant_username = ? ORDER BY updated_at DESC', (username,))
        pages = [dict(r) for r in c.fetchall()]
        conn.close()
        limit = _get_page_limit(tenant)
        return jsonify({'pages': pages, 'count': len(pages), 'limit': limit})

    # POST — create new page
    limit = _get_page_limit(tenant)
    if limit == 0:
        return jsonify({'error': 'Your plan does not support markdown pages.'}), 403

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT COUNT(*) as cnt FROM markdown_pages WHERE tenant_username = ?', (username,))
    cnt = c.fetchone()['cnt']
    if cnt >= limit:
        conn.close()
        return jsonify({'error': f'Page limit reached ({limit} pages max on your plan).'}), 403

    data = request.json or {}
    title = (data.get('title') or 'Untitled').strip()[:200]
    content = data.get('content', '')
    is_published = 1 if data.get('is_published') else 0
    slug = _slugify(data.get('slug') or title) or 'untitled'
    collection_id = data.get('collection_id')
    custom_background = ''
    if tenant.get('allot_backgrounds') == 1:
        custom_background = (data.get('custom_background') or '').strip()
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    base_slug = slug
    idx = 1
    while True:
        c.execute('SELECT id FROM markdown_pages WHERE tenant_username = ? AND slug = ?', (username, slug))
        if not c.fetchone():
            break
        slug = f'{base_slug}-{idx}'
        idx += 1

    c.execute('INSERT INTO markdown_pages (tenant_username, title, slug, content, is_published, created_at, updated_at, collection_id, custom_background) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
              (username, title, slug, content, is_published, now, now, collection_id, custom_background))
    page_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'id': page_id, 'slug': slug})

@app.route('/api/pages/<int:page_id>', methods=['GET', 'PUT', 'DELETE'])
def pages_item(page_id):
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    username = tenant['username']

    if request.method == 'DELETE':
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('DELETE FROM markdown_pages WHERE id = ? AND tenant_username = ?', (page_id, username))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM markdown_pages WHERE id = ? AND tenant_username = ?', (page_id, username))
    page = c.fetchone()

    if request.method == 'GET':
        conn.close()
        if not page:
            return jsonify({'error': 'Page not found'}), 404
        return jsonify(dict(page))

    # PUT — update
    if not page:
        conn.close()
        return jsonify({'error': 'Page not found'}), 404

    data = request.json or {}
    title = data.get('title', page['title']).strip()[:200]
    content = data.get('content', page['content'])
    is_published = 1 if data.get('is_published') else 0
    raw_slug = data.get('slug', page['slug'])
    slug = _slugify(raw_slug) or _slugify(title) or 'untitled'
    collection_id = data.get('collection_id', page['collection_id'])
    custom_background = page['custom_background']
    if tenant.get('allot_backgrounds') == 1:
        custom_background = (data.get('custom_background') or '').strip()
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    base_slug = slug
    idx = 1
    while True:
        c.execute('SELECT id FROM markdown_pages WHERE tenant_username = ? AND slug = ? AND id != ?', (username, slug, page_id))
        if not c.fetchone():
            break
        slug = f'{base_slug}-{idx}'
        idx += 1

    c.execute('UPDATE markdown_pages SET title = ?, slug = ?, content = ?, is_published = ?, updated_at = ?, collection_id = ?, custom_background = ? WHERE id = ? AND tenant_username = ?',
              (title, slug, content, is_published, now, collection_id, custom_background, page_id, username))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'slug': slug})

def serve_404_page():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Page Not Found — OQENS Papers</title>
<link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Lora',Georgia,serif;background:#f5f0e8;color:#5c4a32;display:flex;align-items:center;justify-content:center;height:100vh;}
.box{text-align:center;max-width:320px;}
.box h1{font-size:5rem;color:#d9c9a8;font-weight:600;line-height:1;margin-bottom:16px;}
.box p{color:#8c7055;font-size:0.95rem;line-height:1.7;}
.box a{color:#9b7b4a;text-decoration:none;border-bottom:1px solid #c8a97a;}
</style>
</head>
<body>
<div class="box">
  <h1>404</h1>
  <p>This page hasn't been published or doesn't exist.<br><br>
  <a href="https://papers.oqens.me">papers.oqens.me</a></p>
</div>
</body>
</html>''', 404

def serve_otp_verification_page(col_id, col_name, username, slug):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Private Collection Access Verification</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }}
body {{ background: #f5f0e8; color: #3b2e1e; display: flex; align-items: center; justify-content: center; height: 100vh; padding: 20px; }}
.card {{
    background: #faf6ed;
    border: 1px solid #d9cbb8;
    box-shadow: 0 4px 16px rgba(60,40,20,0.08);
    padding: 32px;
    width: 100%;
    max-width: 420px;
    border-radius: 0px;
}}
.title {{ font-size: 1.15rem; font-weight: 600; margin-bottom: 8px; color: #8b6a3e; text-transform: uppercase; letter-spacing: 0.5px; }}
.desc {{ font-size: 0.85rem; color: #8c7055; margin-bottom: 24px; line-height: 1.5; }}
.field {{ margin-bottom: 16px; }}
.field label {{ display: block; font-size: 0.72rem; font-weight: 600; text-transform: uppercase; color: #8c7055; margin-bottom: 6px; letter-spacing: 0.4px; }}
.field input {{
    width: 100%;
    padding: 10px 12px;
    border: 1.5px solid #d9cbb8;
    background: #faf6ed;
    color: #3b2e1e;
    font-size: 0.88rem;
    outline: none;
    border-radius: 0px;
}}
.field input:focus {{ border-color: #8b6a3e; background: #fff; }}
.btn {{
    width: 100%;
    padding: 10px;
    background: #8b6a3e;
    color: #fff;
    border: none;
    font-size: 0.88rem;
    font-weight: 600;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-radius: 0px;
    transition: background 0.15s;
}}
.btn:hover {{ background: #6b4e28; }}
.error-msg {{ color: #a84444; font-size: 0.8rem; margin-top: 10px; display: none; font-weight: 500; }}
.success-msg {{ color: #5a8a5a; font-size: 0.8rem; margin-top: 10px; display: none; font-weight: 500; }}
</style>
</head>
<body>
<div class="card">
    <div class="title">Verification Required</div>
    <div class="desc">The collection <strong>{col_name}</strong> is private. Please enter your whitelisted email address to verify your access.</div>
    
    <!-- Step 1: Email Form -->
    <div id="email-step">
        <div class="field">
            <label>Email Address</label>
            <input type="email" id="viewer-email" placeholder="you@example.com">
        </div>
        <button class="btn" onclick="requestOtp()">Send Code</button>
    </div>
    
    <!-- Step 2: OTP Form -->
    <div id="otp-step" style="display:none;">
        <div class="field">
            <label>Verification Code</label>
            <input type="text" id="viewer-otp" placeholder="Enter 6-digit code">
        </div>
        <button class="btn" onclick="verifyOtp()">Verify & Access</button>
    </div>
    
    <div class="error-msg" id="msg-error"></div>
    <div class="success-msg" id="msg-success"></div>
</div>

<script>
const colId = {col_id};

async function requestOtp() {{
    const email = document.getElementById('viewer-email').value.trim();
    const errorEl = document.getElementById('msg-error');
    const successEl = document.getElementById('msg-success');
    errorEl.style.display = 'none';
    successEl.style.display = 'none';
    
    if (!email) {{ errorEl.textContent = 'Email is required.'; errorEl.style.display = 'block'; return; }}
    
    try {{
        const res = await fetch('/api/collections/auth/request-otp', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ email, collection_id: colId }})
        }});
        const data = await res.json();
        if (res.ok) {{
            document.getElementById('email-step').style.display = 'none';
            document.getElementById('otp-step').style.display = 'block';
            successEl.textContent = data.message || 'Code sent successfully!';
            successEl.style.display = 'block';
        }} else {{
            errorEl.textContent = data.error || 'Failed to request code.';
            errorEl.style.display = 'block';
        }}
    }} catch(e) {{
        errorEl.textContent = 'Network error.';
        errorEl.style.display = 'block';
    }}
}}

async function verifyOtp() {{
    const code = document.getElementById('viewer-otp').value.trim();
    const errorEl = document.getElementById('msg-error');
    const successEl = document.getElementById('msg-success');
    errorEl.style.display = 'none';
    successEl.style.display = 'none';
    
    if (!code) {{ errorEl.textContent = 'Verification code is required.'; errorEl.style.display = 'block'; return; }}
    
    try {{
        const res = await fetch('/api/collections/auth/verify-otp', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ code, collection_id: colId }})
        }});
        const data = await res.json();
        if (res.ok) {{
            successEl.textContent = 'Verified! Redirecting...';
            successEl.style.display = 'block';
            setTimeout(() => window.location.reload(), 1000);
        }} else {{
            errorEl.textContent = data.error || 'Invalid code.';
            errorEl.style.display = 'block';
        }}
    }} catch(e) {{
        errorEl.textContent = 'Network error.';
        errorEl.style.display = 'block';
    }}
}}
</script>
</body>
</html>"""

@app.route('/p/<username>/<slug>/edit', methods=['GET'])
def collaborative_edit_view(username, slug):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM markdown_pages WHERE tenant_username = ? AND slug = ? AND is_published = 1', (username, slug))
    page = c.fetchone()
    
    if not page:
        conn.close()
        return serve_404_page()
        
    page = dict(page)
    col_id = page.get('collection_id')
    
    if not col_id:
        conn.close()
        return "Unauthorized", 403
        
    c.execute('SELECT * FROM markdown_collections WHERE id = ?', (col_id,))
    col = c.fetchone()
    if not col:
        conn.close()
        return "Collection not found", 404
        
    col = dict(col)
    
    # Check permissions
    has_edit_permission = False
    if session.get('role') == 'tenant' and session.get('tenant_username') == username:
        has_edit_permission = True
    elif col.get('visibility') == 'private':
        viewer_email = session.get('viewer_email')
        if viewer_email:
            c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (col_id, viewer_email))
            access = c.fetchone()
            if access and access[0] == 'edit':
                has_edit_permission = True
                
    conn.close()
    
    if not has_edit_permission:
        return "Unauthorized: Edit permission required.", 403
        
    title = page['title']
    content = page['content'] or ''
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Collaboration Mode — Edit: {title}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Lora:ital,wght@0,400;0,600;1,400&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Inter', sans-serif; background: #f5f0e8; color: #3b2e1e; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }}
.header {{
    background: #faf6ed;
    border-bottom: 1.5px solid #d9cbb8;
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}}
.logo-title {{ font-size: 0.95rem; font-weight: 700; letter-spacing: -0.2px; text-transform: uppercase; color: #8b6a3e; }}
.logo-sub {{ font-size: 0.75rem; color: #8c7055; margin-left: 10px; font-weight: 500; }}
.btn {{
    padding: 8px 16px;
    background: #8b6a3e;
    color: #fff;
    border: none;
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    transition: background 0.15s;
}}
.btn:hover {{ background: #6b4e28; }}
.btn-outline {{
    background: transparent;
    color: #3b2e1e;
    border: 1.5px solid #d9cbb8;
    margin-right: 10px;
}}
.btn-outline:hover {{ background: #ede4d3; }}
.editor-container {{ display: flex; flex: 1; overflow: hidden; }}
.pane {{ flex: 1; display: flex; flex-direction: column; height: 100%; }}
.editor-pane {{ border-right: 1.5px solid #d9cbb8; }}
#editor {{
    flex: 1;
    width: 100%;
    padding: 24px;
    border: none;
    outline: none;
    resize: none;
    font-family: 'Lora', Georgia, serif;
    font-size: 0.98rem;
    line-height: 1.8;
    background: #faf6ed;
    color: #3b2e1e;
    overflow-y: auto;
}}
.preview-pane {{ background: #fff; overflow-y: auto; padding: 32px; min-width: 0; }}
.preview-content img {{ max-width: 100%; height: auto; border-radius: 6px; margin: 12px 0; display: block; }}
.preview-content h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 12px; }}
.preview-content h2 {{ font-size: 1.4rem; font-weight: 600; margin: 28px 0 10px; }}
.preview-content p {{ margin-bottom: 16px; line-height: 1.8; font-family: 'Lora', serif; }}
.preview-content pre {{ background: #1e1e2e; color: #cdd6f4; padding: 16px; border-radius: 6px; overflow-x: auto; margin: 16px 0; }}
.toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: #1a1a1a;
    color: #fff;
    padding: 10px 18px;
    font-size: 0.82rem;
    box-shadow: 0 4px 16px rgba(0,0,0,0.15);
    display: none;
    z-index: 9999;
}}
</style>
</head>
<body>
<div class="header">
    <div style="display:flex; align-items:center;">
        <span class="logo-title">OQENS Papers</span>
        <span class="logo-sub">Collaboration Mode — Editing: <strong>{title}</strong></span>
    </div>
    <div>
        <button class="btn btn-outline" onclick="goBack()">View Page</button>
        <button class="btn" onclick="saveChanges()">Save Page</button>
    </div>
</div>

<div class="editor-container">
    <div class="pane editor-pane">
        <textarea id="editor" oninput="updatePreview()"></textarea>
    </div>
    <div class="pane preview-pane">
        <div class="preview-content" id="preview"></div>
    </div>
</div>

<div class="toast" id="toast">Changes saved successfully</div>

<script>
const initialContent = {json.dumps(content)};
const username = "{username}";
const slug = "{slug}";

document.getElementById('editor').value = initialContent;
updatePreview();

function updatePreview() {{
    const md = document.getElementById('editor').value;
    document.getElementById('preview').innerHTML = marked.parse(md);
}}

async function saveChanges() {{
    const content = document.getElementById('editor').value;
    try {{
        const res = await fetch(`/api/pages/public/${{username}}/${{slug}}`, {{
            method: 'PUT',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ content }})
        }});
        const data = await res.json();
        if (res.ok) {{
            showToast('Changes saved successfully');
        }} else {{
            alert(data.error || 'Failed to save.');
        }}
    }} catch(e) {{
        alert('Network error.');
    }}
}}

function goBack() {{
    window.location.href = `/p/${{username}}/${{slug}}`;
}}

function showToast(msg) {{
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.style.display = 'block';
    setTimeout(() => toast.style.display = 'none', 3000);
}}
</script>
</body>
</html>"""

@app.route('/p/<username>/f/<filename>')
def public_file_view(username, filename):
    filename = os.path.basename(filename)
    tenant_dir = os.path.join(STORAGE_BASE_DIR, username)
    full_path = os.path.join(tenant_dir, filename)
    if not os.path.exists(full_path):
        return "File not found", 404
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = 'application/octet-stream'
    response = make_response("")
    response.headers['Content-Type'] = content_type
    response.headers['X-Accel-Redirect'] = f'/protected_files/{username}/{filename}'
    return response


@app.route('/api/pages/public/<username>/<slug>/comments', methods=['GET'])
def get_page_comments(username, slug):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM markdown_pages WHERE tenant_username = ? AND slug = ? AND is_published = 1', (username, slug))
    page = c.fetchone()
    if not page:
        conn.close()
        return jsonify({'error': 'Page not found'}), 404
        
    page = dict(page)
    col_id = page.get('collection_id')
    
    access_level = 'view'
    if col_id:
        c.execute('SELECT * FROM markdown_collections WHERE id = ?', (col_id,))
        col = c.fetchone()
        if col:
            col = dict(col)
            if session.get('role') == 'tenant' and session.get('tenant_username') == username:
                access_level = 'edit'
            elif col['visibility'] == 'public':
                access_level = col['public_role']
            elif col['visibility'] == 'private':
                viewer_email = session.get('viewer_email')
                if viewer_email:
                    c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (col_id, viewer_email))
                    access = c.fetchone()
                    if access:
                        access_level = access[0]
                    else:
                        access_level = None
                else:
                    access_level = None
    
    if access_level is None:
        conn.close()
        return jsonify({'error': 'Access denied'}), 403
        
    c.execute('SELECT email, comment, created_at FROM page_comments WHERE page_id = ? ORDER BY id ASC', (page['id'],))
    comments = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'comments': comments})


@app.route('/api/pages/public/<username>/<slug>/comments', methods=['POST'])
def post_page_comment(username, slug):
    data = request.json or {}
    comment_text = data.get('comment', '').strip()
    email = data.get('email', '').strip().lower()
    
    if not comment_text:
        return jsonify({'error': 'Comment text is required'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM markdown_pages WHERE tenant_username = ? AND slug = ? AND is_published = 1', (username, slug))
    page = c.fetchone()
    if not page:
        conn.close()
        return jsonify({'error': 'Page not found'}), 404
        
    page = dict(page)
    col_id = page.get('collection_id')
    
    access_level = 'view'
    viewer_email = session.get('viewer_email')
    col = None
    
    if session.get('role') == 'tenant' and session.get('tenant_username') == username:
        access_level = 'edit'
        email = username + "@oqens.me"
    elif col_id:
        c.execute('SELECT * FROM markdown_collections WHERE id = ?', (col_id,))
        col_row = c.fetchone()
        if col_row:
            col = dict(col_row)
            if col['visibility'] == 'public':
                access_level = col['public_role']
                if not email:
                    conn.close()
                    return jsonify({'error': 'Email is required for commenting on public collections.'}), 400
            elif col['visibility'] == 'private':
                if viewer_email:
                    c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (col_id, viewer_email))
                    access = c.fetchone()
                    if access:
                        access_level = access[0]
                        email = viewer_email
                    else:
                        access_level = None
                else:
                    access_level = None
                    
    if access_level is None or access_level == 'view':
        if col_id and col:
            if col['visibility'] == 'private' and access_level not in ['comment', 'edit']:
                conn.close()
                return jsonify({'error': 'You do not have permission to leave comments on this collection.'}), 403
        else:
            conn.close()
            return jsonify({'error': 'Access denied.'}), 403
            
    if not email:
        conn.close()
        return jsonify({'error': 'Email is required.'}), 400
        
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('INSERT INTO page_comments (page_id, email, comment, created_at) VALUES (?, ?, ?, ?)',
              (page['id'], email, comment_text, now))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'comment': {'email': email, 'comment': comment_text, 'created_at': now}})


@app.route('/p/<username>/col/<col_slug>')
def public_collection_view(username, col_slug):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM markdown_collections WHERE tenant_username = ? AND slug = ?', (username, col_slug))
    col = c.fetchone()
    
    if not col:
        conn.close()
        return serve_404_page()
        
    col = dict(col)
    col_id = col['id']
    
    access_level = 'view'
    if session.get('role') == 'tenant' and session.get('tenant_username') == username:
        access_level = 'edit'
    elif col['visibility'] == 'public':
        access_level = col['public_role']
    elif col['visibility'] == 'private':
        viewer_email = session.get('viewer_email')
        if viewer_email:
            c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (col_id, viewer_email))
            access = c.fetchone()
            if access:
                access_level = access[0]
            else:
                access_level = None
        else:
            access_level = None

    if access_level is None:
        conn.close()
        return serve_otp_verification_page(col_id, col['name'], username, f"col/{col_slug}")

    c.execute('SELECT title, slug, is_published, updated_at FROM markdown_pages WHERE tenant_username = ? AND collection_id = ? AND is_published = 1 ORDER BY updated_at DESC', (username, col_id))
    pages = [dict(r) for r in c.fetchall()]
    conn.close()
    
    title = col['name']
    desc = col['description'] or 'No description.'
    banner = col.get('banner_image')
    theme_color = col.get('theme_color') or '#8b6a3e'
    
    banner_url = ""
    banner_html = ""
    if banner:
        if banner.startswith('/'):
            banner_url = f"https://{request.host}{banner}"
        else:
            banner_url = banner
        banner_html = f'''
        <div style="width:100%; height:220px; overflow:hidden; border-radius:8px; margin-bottom:24px; border:1px solid #d9cbb8;">
            <img src="{banner}" style="width:100%; height:100%; object-fit:cover;">
        </div>
        '''
        
    pages_list_html = ""
    if len(pages) == 0:
        pages_list_html = '<div style="text-align:center; padding:40px; color:#8c7055; font-style:italic;">No published papers in this collection yet.</div>'
    else:
        for p in pages:
            pages_list_html += f'''
            <div style="background:#faf6ed; border:1px solid #d9cbb8; padding:16px 20px; margin-bottom:12px; border-radius:8px; transition:border-color 0.2s;">
                <h3 style="font-size:1.05rem; font-weight:600; margin-bottom:6px;"><a href="/p/{username}/{p['slug']}" style="color:{theme_color}; text-decoration:none;">📄 {p['title']}</a></h3>
                <div style="font-size:0.75rem; color:#8c7055;">Last updated: {p['updated_at'][:10] if p['updated_at'] else ''} · <a href="/p/{username}/{p['slug']}" style="color:{theme_color}; text-decoration:underline;">Read Paper</a></div>
            </div>
            '''
            
    collection_url = f"https://{request.host}/p/{username}/col/{col_slug}"
    og_image_meta = ""
    twitter_card_meta = '<meta name="twitter:card" content="summary">'
    if banner_url:
        og_image_meta = f'''<meta property="og:image" content="{banner_url}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:image" content="{banner_url}">'''
        twitter_card_meta = '<meta name="twitter:card" content="summary_large_image">'
        
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Collection by {username}</title>
<meta name="description" content="{desc}">

<!-- Open Graph / Facebook -->
<meta property="og:type" content="website">
<meta property="og:url" content="{collection_url}">
<meta property="og:title" content="{title} — Collection by {username}">
<meta property="og:description" content="{desc}">
<meta property="og:site_name" content="OQENS Papers">
{og_image_meta}

<!-- Twitter -->
{twitter_card_meta}
<meta name="twitter:url" content="{collection_url}">
<meta name="twitter:title" content="{title} — Collection by {username}">
<meta name="twitter:description" content="{desc}">

<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }}
:root {{
  --radius: 8px;
}}
body {{ background: #f5f0e8; color: #3b2e1e; padding: 40px 20px; display: flex; justify-content: center; }}
.container {{ width: 100%; max-width: 650px; }}
.header {{ margin-bottom: 30px; border-bottom: 1px solid #d9cbb8; padding-bottom: 20px; }}
.title {{ font-size: 1.6rem; font-weight: 700; color: {theme_color}; margin-bottom: 8px; }}
.desc {{ font-size: 0.9rem; color: #8c7055; line-height: 1.5; }}
.meta {{ font-size: 0.75rem; color: #8c7055; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; margin-bottom: 12px; }}
</style>
</head>
<body>
<div class="container">
    {banner_html}
    <div class="header">
        <div class="meta">Collection by {username}</div>
        <h1 class="title">{title}</h1>
        <p class="desc">{desc}</p>
    </div>
    
    <div class="list">
        {pages_list_html}
    </div>
</div>
</body>
</html>"""


@app.route('/p/<username>/<slug>')
def public_page_view(username, slug):
    """Public view of a published markdown page — no auth required."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM markdown_pages WHERE tenant_username = ? AND slug = ? AND is_published = 1', (username, slug))
    page = c.fetchone()
    
    if not page:
        conn.close()
        return serve_404_page()
        
    page = dict(page)
    col_id = page.get('collection_id')
    allot_backgrounds = 0
    
    c.execute('SELECT allot_backgrounds FROM tenants WHERE username = ?', (username,))
    t_row = c.fetchone()
    if t_row:
        allot_backgrounds = t_row['allot_backgrounds']
        
    access_level = 'view'
    col_name = ""
    col = None
    banner_url = ""
    if col_id:
        c.execute('SELECT * FROM markdown_collections WHERE id = ?', (col_id,))
        col_row = c.fetchone()
        if col_row:
            col = dict(col_row)
            col_name = col['name']
            if col.get('banner_image'):
                if col['banner_image'].startswith('/'):
                    banner_url = f"https://{request.host}{col['banner_image']}"
                else:
                    banner_url = col['banner_image']
                    
            if session.get('role') == 'tenant' and session.get('tenant_username') == username:
                access_level = 'edit'
            elif col['visibility'] == 'public':
                access_level = col['public_role']
            elif col['visibility'] == 'private':
                viewer_email = session.get('viewer_email')
                if viewer_email:
                    c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (col_id, viewer_email))
                    access = c.fetchone()
                    if access:
                        access_level = access[0]
                    else:
                        access_level = None
                else:
                    access_level = None
                    
    conn.close()
    
    if access_level is None:
        return serve_otp_verification_page(col_id, col_name, username, slug)

    custom_bg = 'var(--cream)'
    if allot_backgrounds == 1 and page['custom_background']:
        custom_bg = page['custom_background'].strip()

    title = page['title']
    content_md = page['content'] or ''
    # Strip YAML frontmatter server-side
    import re as _re
    if content_md.startswith('---'):
        _fm_match = _re.match(r'^---[\s\S]*?\n---\s*\n?', content_md)
        if _fm_match:
            content_md = content_md[_fm_match.end():]
    author = username
    updated = page['updated_at'][:10] if page['updated_at'] else ''
    edit_link_html = ""
    if access_level == 'edit':
        edit_link_html = f'''
        <span class="sep">·</span>
        <span><a href="/p/{username}/{slug}/edit" style="color:var(--link);font-weight:600;text-decoration:none;border-bottom:1.5px solid var(--link);">Edit Paper</a></span>
        '''

    page_url = f"https://{request.host}/p/{username}/{slug}"
    og_image_meta = ""
    twitter_card_meta = '<meta name="twitter:card" content="summary">'
    if banner_url:
        og_image_meta = f'''<meta property="og:image" content="{banner_url}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:image" content="{banner_url}">'''
        twitter_card_meta = '<meta name="twitter:card" content="summary_large_image">'
        
    viewer_email_val = session.get('viewer_email') or ''
    is_public_col_val = 'true' if (col and col['visibility'] == 'public') else 'false'
    has_comment_access_val = 'true' if access_level in ['comment', 'edit'] else 'false'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{title} by {author}">

<!-- Open Graph / Facebook -->
<meta property="og:type" content="article">
<meta property="og:url" content="{page_url}">
<meta property="og:title" content="{title} by {author}">
<meta property="og:description" content="{title} — written by {author} on OQENS Papers">
<meta property="og:site_name" content="OQENS Papers">
{og_image_meta}

<!-- Twitter -->
{twitter_card_meta}
<meta name="twitter:url" content="{page_url}">
<meta name="twitter:title" content="{title} by {author}">
<meta name="twitter:description" content="{title} — written by {author} on OQENS Papers">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,500;0,600;1,400;1,500&family=IM+Fell+English:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/contrib/auto-render.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.8.0/styles/github.min.css">
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.8.0/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<link href="https://cdn.jsdelivr.net/npm/gridjs/dist/theme/mermaid.min.css" rel="stylesheet" />
<script src="https://cdn.jsdelivr.net/npm/gridjs/dist/gridjs.umd.js"></script>
<style>
*, *::before, *::after {{
  margin: 0; padding: 0; box-sizing: border-box;
}}

:root {{
  --cream:      #f7f2e8;
  --cream-mid:  #ede6d6;
  --cream-dark: #e0d5c0;
  --ink:        #3b2e1e;
  --ink-light:  #6b5340;
  --ink-faint:  #9c8066;
  --ink-rule:   #c9b99a;
  --link:       #7a5230;
  --link-hover: #4e3118;
  --code-bg:    #ede4d3;
  --quote-bar:  #c4a77d;
  --radius:     8px;
}}

html {{
  font-size: 18px;
  -webkit-font-smoothing: antialiased;
}}

body {{
  background: {custom_bg};
  color: var(--ink);
  font-family: 'Lora', Georgia, 'Times New Roman', serif;
  line-height: 1.85;
  min-height: 100vh;
}}

/* ── Top rule ── */
.top-rule {{
  width: 100%;
  height: 3px;
  background: linear-gradient(to right, transparent, var(--ink-rule), transparent);
  margin-bottom: 0;
}}

/* ── Page layout ── */
.page {{
  max-width: 900px;
  width: 90%;
  margin: 0 auto;
  padding: 56px 0 100px;
}}

/* ── Header ── */
.page-header {{
  margin-bottom: 44px;
  padding-bottom: 28px;
  border-bottom: 1px solid var(--ink-rule);
}}

.page-title {{
  font-family: 'Lora', serif;
  font-size: 2.1rem;
  font-weight: 600;
  line-height: 1.22;
  color: var(--ink);
  letter-spacing: -0.3px;
  margin-bottom: 14px;
}}

.page-meta {{
  font-size: 0.78rem;
  color: var(--ink-faint);
  font-family: 'Lora', serif;
  font-style: italic;
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  align-items: center;
}}

.page-meta strong {{
  font-style: normal;
  font-weight: 500;
  color: var(--ink-light);
}}

.page-meta .sep {{ color: var(--ink-rule); }}

/* ── Content typography ── */
.content {{
  color: var(--ink);
}}

.content p {{
  margin-bottom: 1.4em;
  text-align: justify;
  hyphens: auto;
}}

.content h1,
.content h2,
.content h3,
.content h4,
.content h5 {{
  font-family: 'Lora', serif;
  font-weight: 600;
  color: var(--ink);
  line-height: 1.25;
  margin-top: 2.2em;
  margin-bottom: 0.6em;
}}

.content h1 {{ font-size: 1.65rem; }}
.content h2 {{ font-size: 1.35rem; border-bottom: 1px solid var(--ink-rule); padding-bottom: 6px; }}
.content h3 {{ font-size: 1.1rem; }}
.content h4 {{ font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-light); }}

/* Links — underline in brownish, no bright colours */
.content a {{
  color: var(--link);
  text-decoration: underline;
  text-decoration-color: var(--ink-rule);
  text-underline-offset: 3px;
  transition: color 0.12s;
}}
.content a:hover {{ color: var(--link-hover); text-decoration-color: var(--link-hover); }}

/* Images */
.content img {{
  display: block;
  max-width: 100%;
  height: auto;
  margin: 2.2em auto;
  border-radius: var(--radius);
  box-shadow: 0 2px 12px rgba(60,40,20,0.10);
  border: 1px solid var(--cream-dark);
}}

/* Figure captions (alt text) */
.content img + em {{
  display: block;
  text-align: center;
  font-size: 0.78rem;
  color: var(--ink-faint);
  margin-top: -1.4em;
  margin-bottom: 1.8em;
}}

/* Blockquote */
.content blockquote {{
  border-left: 3px solid var(--quote-bar);
  margin: 1.8em 0;
  padding: 0.6em 0 0.6em 20px;
  color: var(--ink-light);
  font-style: italic;
  background: var(--cream-mid);
  border-radius: 0 var(--radius) var(--radius) 0;
}}
.content blockquote p {{ margin-bottom: 0; }}

/* Lists */
.content ul,
.content ol {{
  padding-left: 26px;
  margin-bottom: 1.4em;
}}
.content li {{ margin-bottom: 0.4em; }}
.content li::marker {{ color: var(--ink-faint); }}

/* Inline code */
.content code {{
  font-family: 'Courier New', Courier, monospace;
  font-size: 0.83em;
  background: var(--code-bg);
  color: var(--ink);
  padding: 2px 6px;
  border-radius: 3px;
  border: 1px solid var(--ink-rule);
}}

/* Code blocks */
.content pre {{
  background: #2c2015;
  color: #cdd6f4;
  padding: 20px;
  overflow-x: auto;
  border-radius: var(--radius);
  margin: 1.8em 0;
  border: 1.5px solid var(--ink-rule);
}}
.content pre code {{
  background: transparent;
  color: #ede4d3;
  padding: 0;
  font-family: 'Courier New', Courier, monospace;
  font-size: 0.82em;
  border: none;
}}

/* Footnotes */
.footnotes {{
  margin-top: 60px;
  border-top: 1px dashed var(--ink-rule);
  padding-top: 20px;
  font-size: 0.8rem;
  color: var(--ink-light);
}}
.footnotes ol {{ padding-left: 18px; }}

.content hr {{
  border: none;
  border-top: 1.5px solid var(--ink-rule);
  margin: 40px 0;
}}

.mermaid {{
  background: var(--cream-mid) !important;
  padding: 20px;
  border-radius: var(--radius);
  margin: 1.8em 0;
  border: 1px solid var(--ink-rule);
  display: flex;
  justify-content: center;
}}
.mermaid svg {{
  max-width: 100% !important;
  height: auto !important;
}}

/* ── Footer ── */
.page-footer {{
  margin-top: 56px;
  padding-top: 20px;
  border-top: 1px solid var(--ink-rule);
  font-size: 0.72rem;
  color: var(--ink-faint);
  font-style: italic;
  text-align: center;
  line-height: 1.7;
}}
.page-footer a {{
  color: var(--ink-faint);
  text-decoration: none;
  border-bottom: 1px solid var(--ink-rule);
}}
.page-footer a:hover {{ color: var(--link); }}

/* ── Reading width for mobile ── */
@media (max-width: 640px) {{
  html {{ font-size: 16px; }}
  .page {{ padding: 32px 20px 72px; }}
  .page-title {{ font-size: 1.7rem; }}
}}
</style>
</head>
<body>

<div class="top-rule"></div>

<div class="page">
  <div class="page-header">
    <h1 class="page-title">{title}</h1>
    <div class="page-meta">
      <span>By <strong>{author}</strong></span>
      <span class="sep">·</span>
      <span>Published {updated}</span>
      {edit_link_html}
    </div>
  </div>
  
  <div class="content" id="content"></div>

  <!-- Discussion Board -->
  <div id="comments-section" style="margin-top: 60px; border-top: 1.5px solid var(--ink-rule); padding-top: 36px; display: none;">
      <h3 style="font-family:'Lora',serif; font-size:1.25rem; font-weight:600; margin-bottom:24px; color:var(--ink);">Discussion</h3>
      <div id="comments-list" style="display:flex; flex-direction:column; gap:16px; margin-bottom:30px;"></div>
      
      <div id="comment-form" style="display:flex; flex-direction:column; gap:14px; background:var(--cream-mid); padding:24px; border-radius:var(--radius); border:1px solid var(--cream-dark); margin-top:20px;">
          <h4 style="font-size:0.8rem; text-transform:uppercase; letter-spacing:0.06em; color:var(--ink-light); margin-bottom:4px; font-weight:600;">Leave a Comment</h4>
          <div id="comment-email-field" style="display:none; flex-direction:column; gap:6px;">
              <label style="font-size:0.7rem; font-weight:600; text-transform:uppercase; color:var(--ink-light); letter-spacing:0.4px;">Your Email Address</label>
              <input type="email" id="commenter-email" placeholder="you@example.com" style="padding:10px 12px; border:1px solid var(--ink-rule); background:var(--cream); color:var(--ink); font-size:0.85rem; outline:none; border-radius:var(--radius);">
          </div>
          <div style="display:flex; flex-direction:column; gap:6px;">
              <label style="font-size:0.7rem; font-weight:600; text-transform:uppercase; color:var(--ink-light); letter-spacing:0.4px;">Comment</label>
              <textarea id="comment-text" placeholder="Write your comment..." rows="4" style="padding:10px 12px; border:1px solid var(--ink-rule); background:var(--cream); color:var(--ink); font-size:0.88rem; outline:none; resize:none; font-family:inherit; border-radius:var(--radius); line-height:1.5;"></textarea>
          </div>
          <button onclick="submitComment()" style="align-self:flex-end; padding:10px 24px; background:var(--ink); color:var(--cream); border:none; font-size:0.8rem; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; cursor:pointer; border-radius:var(--radius); transition:opacity 0.15s;">Post Comment</button>
      </div>
  </div>

  <footer class="page-footer">
    Published on <a href="https://papers.oqens.me">OQENS Papers</a> &nbsp;·&nbsp; <a href="https://oqens.me">oqens.me</a>
  </footer>
</div>

<script>
// 0. Configure marked FIRST before any parsing
marked.setOptions({{
  breaks: true,
  gfm: true
}});

// 1. Parse & render markdown content
const rawMd = {json.dumps(content_md)};
const previewEl = document.getElementById('content');
previewEl.innerHTML = marked.parse(rawMd);

// 2. Open external links in new tab
previewEl.querySelectorAll('a[href^="http"]').forEach(a => {{
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
}});

// 3. Convert mermaid code blocks
let hasMermaid = false;
previewEl.querySelectorAll('pre code.language-mermaid').forEach(codeEl => {{
  hasMermaid = true;
  const preEl = codeEl.parentElement;
  const newPre = document.createElement('pre');
  newPre.className = 'mermaid';
  newPre.textContent = codeEl.textContent;
  preEl.replaceWith(newPre);
}});

// 4. Syntax highlight code blocks
if (window.hljs) {{
  previewEl.querySelectorAll('pre code').forEach(block => {{
    try {{ hljs.highlightElement(block); }} catch(e) {{}}
  }});
}}

// 5. KaTeX math
if (typeof renderMathInElement === 'function') {{
  renderMathInElement(previewEl, {{
    delimiters: [
      {{left: '$$', right: '$$', display: true}},
      {{left: '$', right: '$', display: false}},
      {{left: '\\(', right: '\\)', display: false}},
      {{left: '\\[', right: '\\]', display: true}}
    ],
    throwOnError: false
  }});
}}

// 6. Mermaid diagrams
if (hasMermaid && typeof mermaid !== 'undefined') {{
  mermaid.initialize({{
    startOnLoad: false, theme: 'base',
    themeVariables: {{
      background: '#ede6d6', primaryColor: '#e0d5c0',
      primaryTextColor: '#3b2e1e', lineColor: '#6b5340',
      secondaryColor: '#f7f2e8', tertiaryColor: '#f7f2e8'
    }}
  }});
  try {{ mermaid.run({{ nodes: previewEl.querySelectorAll('.mermaid') }}); }}
  catch(err) {{ console.error('Mermaid error:', err); }}
}}

// 7. Chart.js
if (window.Chart) {{
  previewEl.querySelectorAll('pre code.language-chart, pre code.language-chartjs').forEach((codeEl, i) => {{
    const preEl = codeEl.parentElement;
    try {{
      const config = JSON.parse(codeEl.textContent);
      const canvas = document.createElement('canvas');
      canvas.id = 'chart-' + Date.now() + '-' + i;
      const wrapper = document.createElement('div');
      wrapper.style.cssText = 'width:100%;max-width:700px;margin:24px auto;';
      wrapper.appendChild(canvas);
      preEl.replaceWith(wrapper);
      new Chart(canvas, config);
    }} catch(e) {{ console.error('Invalid chart:', e); }}
  }});
}}

// 8. Grid.js tables
if (window.gridjs) {{
  previewEl.querySelectorAll('table').forEach(tableEl => {{
    if (tableEl.closest('.gridjs-wrapper')) return;
    const headers = Array.from(tableEl.querySelectorAll('th')).map(th => th.innerText);
    const data = Array.from(tableEl.querySelectorAll('tbody tr')).map(tr =>
      Array.from(tr.querySelectorAll('td')).map(td => td.innerText));
    if (headers.length && data.length) {{
      const wrapper = document.createElement('div');
      wrapper.style.margin = '24px 0';
      tableEl.parentNode.insertBefore(wrapper, tableEl);
      tableEl.style.display = 'none';
      new gridjs.Grid({{ columns: headers, data, search: true, sort: true, pagination: {{ limit: 10 }} }}).render(wrapper);
    }}
  }});
}}

// 9. Comments
const viewerEmail = "{viewer_email_val}";
const isPublicCol = {is_public_col_val};
const hasCommentAccess = {has_comment_access_val};

async function loadComments() {{
    if (!isPublicCol && !hasCommentAccess && !viewerEmail) return;
    try {{
        const res = await fetch(`/api/pages/public/${{username}}/${{slug}}/comments`);
        if (!res.ok) return;
        const data = await res.json();
        const list = document.getElementById('comments-list');
        list.innerHTML = '';
        if (data.comments && data.comments.length > 0) {{
            data.comments.forEach(c => {{
                const item = document.createElement('div');
                item.style.cssText = 'padding:14px 18px;background:var(--cream-mid);border:1px solid var(--cream-dark);border-radius:var(--radius);';
                item.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-size:0.75rem;color:var(--ink-faint);"><strong>${{escapeHTML(c.email)}}</strong><span>${{c.created_at.substring(0,16)}}</span></div><p style="font-size:0.88rem;line-height:1.5;color:var(--ink);margin-bottom:0;font-family:sans-serif;">${{escapeHTML(c.comment)}}</p>`;
                list.appendChild(item);
            }});
        }} else {{
            list.innerHTML = '<div style="color:var(--ink-faint);font-size:0.82rem;font-style:italic;">No comments yet.</div>';
        }}
        document.getElementById('comments-section').style.display = 'block';
        if (isPublicCol && !viewerEmail) document.getElementById('comment-email-field').style.display = 'flex';
    }} catch(e) {{ console.error('Comments error:', e); }}
}}

async function submitComment() {{
    const comment = document.getElementById('comment-text').value.trim();
    let email = '';
    if (isPublicCol && !viewerEmail) {{
        email = document.getElementById('commenter-email').value.trim();
        if (!email) {{ alert('Please enter your email.'); return; }}
    }}
    if (!comment) {{ alert('Please enter a comment.'); return; }}
    try {{
        const res = await fetch(`/api/pages/public/${{username}}/${{slug}}/comments`, {{
            method: 'POST', headers: {{'Content-Type':'application/json'}},
            body: JSON.stringify({{ comment, email }})
        }});
        const data = await res.json();
        if (res.ok) {{ document.getElementById('comment-text').value=''; loadComments(); }}
        else {{ alert(data.error || 'Failed to post.'); }}
    }} catch(e) {{ alert('Network error.'); }}
}}

function escapeHTML(str) {{
    return str.replace(/[&<>'"]/g, t => ({{'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}}[t]||t));
}}

loadComments();
</script>
</body>
</html>'''

# ─── Collections API ──────────────────────────────────────────────────────────

# ─── Collections API ──────────────────────────────────────────────────────────

def send_collection_invite_email(owner_username, col_name, guest_email, role, col_slug):
    subject = f"OQENS Papers — You have been granted access to {col_name}"
    url = f"https://papers.oqens.me/p/{owner_username}"
    role_str = "view and edit" if role == 'edit' else "view"
    html_body = f"""
    <div style="font-family:'Inter',sans-serif;max-width:550px;margin:30px auto;border:1px solid #d9cbb8;padding:30px;background:#faf6ed;color:#3b2e1e;">
        <h2 style="font-size:1.3rem;font-weight:600;margin-bottom:20px;border-bottom:1px solid #d9cbb8;padding-bottom:12px;color:#8b6a3e;">Private Collection Access Invitation</h2>
        <p>Hello,</p>
        <p>You have been granted access to the private collection <strong>{col_name}</strong> by author <strong>{owner_username}</strong> on OQENS Papers.</p>
        <p>Your permission level is set to: <strong>{role_str}</strong>.</p>
        <p>To access this collection, visit the author's public space at the link below and enter your email address to verify your access:</p>
        <p style="margin:24px 0;"><a href="{url}" style="display:inline-block;background:#8b6a3e;color:#fff;text-decoration:none;padding:10px 20px;font-weight:600;border-radius:4px;">Access Collection</a></p>
        <p style="font-size:0.8rem;color:#8c7055;margin-top:24px;border-top:1px solid #e8dece;padding-top:12px;">This is an automated security notification from OQENS.</p>
    </div>
    """
    send_system_email(guest_email, subject, html_body)

@app.route('/api/collections', methods=['GET', 'POST'])
def collections_collection():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    username = tenant['username']

    # Enforce allotment check
    if not tenant.get('allot_collections'):
        return jsonify({'error': 'Collections feature not alloted.'}), 403

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if request.method == 'GET':
        c.execute('SELECT * FROM markdown_collections WHERE tenant_username = ? ORDER BY id DESC', (username,))
        collections = [dict(r) for r in c.fetchall()]
        
        # Hydrate each collection with its allowed emails
        for col in collections:
            c.execute('SELECT email, role FROM collection_access WHERE collection_id = ?', (col['id'],))
            col['allowed_members'] = [dict(r) for r in c.fetchall()]
            
        conn.close()
        return jsonify({'collections': collections})

    # POST - create new collection
    data = request.json or {}
    name = (data.get('name') or 'Untitled Collection').strip()[:200]
    description = (data.get('description') or '').strip()
    visibility = data.get('visibility') or 'public'
    public_role = data.get('public_role') or 'view'
    banner_image = data.get('banner_image') or None
    theme_color = data.get('theme_color') or '#8b6a3e'
    allowed_members = data.get('allowed_members') or [] # list of {email, role}
    slug = _slugify(name) or 'untitled-collection'

    base_slug = slug
    idx = 1
    while True:
        c.execute('SELECT id FROM markdown_collections WHERE tenant_username = ? AND slug = ?', (username, slug))
        if not c.fetchone():
            break
        slug = f'{base_slug}-{idx}'
        idx += 1

    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''
        INSERT INTO markdown_collections 
        (tenant_username, name, description, slug, visibility, public_role, banner_image, theme_color, created_at) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (username, name, description, slug, visibility, public_role, banner_image, theme_color, now))
    col_id = c.lastrowid

    
    # Process whitelisted emails
    for member in allowed_members:
        email = member.get('email', '').strip().lower()
        role = member.get('role', 'view')
        if email:
            c.execute('INSERT OR IGNORE INTO collection_access (collection_id, email, role, notified) VALUES (?, ?, ?, 1)',
                      (col_id, email, role))
            try:
                send_collection_invite_email(username, name, email, role, slug)
            except Exception as ex:
                pass

    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'id': col_id, 'slug': slug})


@app.route('/api/collections/<int:col_id>', methods=['PUT', 'DELETE'])
def collections_item(col_id):
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    username = tenant['username']

    # Enforce allotment check
    if not tenant.get('allot_collections'):
        return jsonify({'error': 'Collections feature not alloted.'}), 403

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Verify ownership
    c.execute('SELECT id FROM markdown_collections WHERE id = ? AND tenant_username = ?', (col_id, username))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Collection not found'}), 404

    if request.method == 'DELETE':
        # Delete collection, set collection_id to null for pages
        c.execute('DELETE FROM markdown_collections WHERE id = ?', (col_id,))
        c.execute('DELETE FROM collection_access WHERE collection_id = ?', (col_id,))
        c.execute('UPDATE markdown_pages SET collection_id = NULL WHERE collection_id = ? AND tenant_username = ?', (col_id, username))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})

    # PUT - update
    data = request.json or {}
    name = (data.get('name') or 'Untitled Collection').strip()[:200]
    description = (data.get('description') or '').strip()
    visibility = data.get('visibility') or 'public'
    public_role = data.get('public_role') or 'view'
    banner_image = data.get('banner_image') or None
    theme_color = data.get('theme_color') or '#8b6a3e'
    allowed_members = data.get('allowed_members') or [] # list of {email, role}
    slug = _slugify(name) or 'untitled-collection'

    base_slug = slug
    idx = 1
    while True:
        c.execute('SELECT id FROM markdown_collections WHERE tenant_username = ? AND slug = ? AND id != ?', (username, slug, col_id))
        if not c.fetchone():
            break
        slug = f'{base_slug}-{idx}'
        idx += 1

    c.execute('''
        UPDATE markdown_collections 
        SET name = ?, description = ?, slug = ?, visibility = ?, public_role = ?, banner_image = ?, theme_color = ? 
        WHERE id = ?
    ''', (name, description, slug, visibility, public_role, banner_image, theme_color, col_id))

    
    # Update whitelisted members
    c.execute('SELECT email FROM collection_access WHERE collection_id = ?', (col_id,))
    existing_emails = {r[0].strip().lower() for r in c.fetchall()}
    
    c.execute('DELETE FROM collection_access WHERE collection_id = ?', (col_id,))
    
    for member in allowed_members:
        email = member.get('email', '').strip().lower()
        role = member.get('role', 'view')
        if email:
            c.execute('INSERT OR IGNORE INTO collection_access (collection_id, email, role) VALUES (?, ?, ?)',
                      (col_id, email, role))
            if email not in existing_emails:
                try:
                    send_collection_invite_email(username, name, email, role, slug)
                except Exception as ex:
                    pass
                
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/collections/auth/request-otp', methods=['POST'])
def collections_request_otp():
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    collection_id = data.get('collection_id')
    
    if not email or not collection_id:
        return jsonify({'error': 'Email and Collection ID are required.'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('SELECT name, tenant_username FROM markdown_collections WHERE id = ? AND visibility = \'private\'', (collection_id,))
    col = c.fetchone()
    if not col:
        conn.close()
        return jsonify({'error': 'Collection not found or not private.'}), 404
        
    c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (collection_id, email))
    access = c.fetchone()
    conn.close()
    
    if not access:
        return jsonify({'error': 'Access denied. Your email is not whitelisted.'}), 403
        
    otp = str(secrets.randbelow(900000) + 100000)
    
    session['viewer_otp'] = otp
    session['viewer_pending_email'] = email
    session['viewer_pending_col'] = collection_id
    session['viewer_otp_issued_at'] = time.time()
    session['viewer_otp_attempts'] = 0
    
    subject = "OQENS Papers — Your Verification Code"
    html_body = f"""
    <div style="font-family:'Inter',sans-serif;max-width:500px;margin:30px auto;border:1px solid #d9cbb8;padding:30px;background:#faf6ed;color:#3b2e1e;">
        <h2 style="font-size:1.2rem;font-weight:600;margin-bottom:20px;border-bottom:1px solid #d9cbb8;padding-bottom:12px;color:#8b6a3e;">Access Verification Code</h2>
        <p>Hello,</p>
        <p>You requested access to the private collection <strong>{col['name']}</strong>.</p>
        <p>Please enter the following 6-digit verification code on the verification page:</p>
        <p style="margin:24px 0;text-align:center;"><span style="font-size:1.8rem;font-weight:700;letter-spacing:4px;background:#e8dece;padding:10px 24px;border:1px solid #d9cbb8;color:#3b2e1e;">{otp}</span></p>
        <p>This code is valid for 15 minutes. If you did not request this code, you can ignore this email.</p>
    </div>
    """
    if send_system_email(email, subject, html_body):
        return jsonify({'status': 'success', 'message': 'Verification code sent.'})
    else:
        return jsonify({'error': 'Failed to send verification email.'}), 500


@app.route('/api/collections/auth/verify-otp', methods=['POST'])
def collections_verify_otp():
    data = request.json or {}
    code = data.get('code', '').strip()
    collection_id = data.get('collection_id')
    
    if not code or not collection_id:
        return jsonify({'error': 'Code and Collection ID are required.'}), 400
        
    saved_otp = session.get('viewer_otp')
    saved_email = session.get('viewer_pending_email')
    saved_col = session.get('viewer_pending_col')
    issued_at = session.get('viewer_otp_issued_at')
    attempts = session.get('viewer_otp_attempts', 0)
    
    if not saved_otp or not saved_email or str(saved_col) != str(collection_id) or issued_at is None:
        return jsonify({'error': 'No pending request found or session expired.'}), 400
        
    if attempts >= 5:
        session.pop('viewer_otp', None)
        session.pop('viewer_pending_email', None)
        session.pop('viewer_pending_col', None)
        session.pop('viewer_otp_issued_at', None)
        session.pop('viewer_otp_attempts', None)
        return jsonify({'error': 'Too many failed attempts. Please request a new verification code.'}), 429
        
    if time.time() - issued_at > 900:
        session.pop('viewer_otp', None)
        session.pop('viewer_pending_email', None)
        session.pop('viewer_pending_col', None)
        session.pop('viewer_otp_issued_at', None)
        session.pop('viewer_otp_attempts', None)
        return jsonify({'error': 'Verification code has expired. Please request a new one.'}), 400
        
    if code != saved_otp:
        session['viewer_otp_attempts'] = attempts + 1
        remaining = 5 - (attempts + 1)
        if remaining <= 0:
            session.pop('viewer_otp', None)
            session.pop('viewer_pending_email', None)
            session.pop('viewer_pending_col', None)
            session.pop('viewer_otp_issued_at', None)
            session.pop('viewer_otp_attempts', None)
            return jsonify({'error': 'Too many failed attempts. Please request a new verification code.'}), 429
        return jsonify({'error': f'Invalid verification code. {remaining} attempts remaining.'}), 400
        
    session['viewer_email'] = saved_email
    
    session.pop('viewer_otp', None)
    session.pop('viewer_pending_email', None)
    session.pop('viewer_pending_col', None)
    session.pop('viewer_otp_issued_at', None)
    session.pop('viewer_otp_attempts', None)
    
    return jsonify({'status': 'success', 'email': saved_email})


@app.route('/api/pages/public/<username>/<slug>', methods=['PUT'])
def collaborative_edit_page(username, slug):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('SELECT * FROM markdown_pages WHERE tenant_username = ? AND slug = ?', (username, slug))
    page = c.fetchone()
    if not page:
        conn.close()
        return jsonify({'error': 'Page not found.'}), 404
        
    page = dict(page)
    col_id = page.get('collection_id')
    
    if not col_id:
        conn.close()
        return jsonify({'error': 'Unauthorized: Page is not part of a collection.'}), 403
        
    c.execute('SELECT * FROM markdown_collections WHERE id = ?', (col_id,))
    col = c.fetchone()
    if not col:
        conn.close()
        return jsonify({'error': 'Collection not found.'}), 404
        
    col = dict(col)
    
    has_edit_permission = False
    
    if session.get('role') == 'tenant' and session.get('tenant_username') == username:
        has_edit_permission = True
    elif col.get('visibility') == 'private':
        viewer_email = session.get('viewer_email')
        if viewer_email:
            c.execute('SELECT role FROM collection_access WHERE collection_id = ? AND email = ?', (col_id, viewer_email))
            access = c.fetchone()
            if access and access[0] == 'edit':
                has_edit_permission = True
                
    if not has_edit_permission:
        conn.close()
        return jsonify({'error': 'Unauthorized: You do not have edit permission.'}), 403
        
    data = request.json or {}
    content = data.get('content', '')
    title = data.get('title', page['title']).strip()[:200]
    
    now = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('UPDATE markdown_pages SET title = ?, content = ?, updated_at = ? WHERE id = ?', 
              (title, content, now, page['id']))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'slug': page['slug']})


# ─── Feature Allotment Admin API ──────────────────────────────────────────────

@app.route('/api/admin/feature-allotment', methods=['GET'])
def admin_feature_allotment():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get available features
    c.execute('SELECT id, name, flag_key, description FROM feature_flags')
    features = [dict(r) for r in c.fetchall()]

    # Get tenants and their allotted features
    c.execute('SELECT id, username, custom_domain_email AS email FROM tenants WHERE is_deleted = 0')
    tenants = [dict(r) for r in c.fetchall()]
    
    for t in tenants:
        c.execute('''
            SELECT ff.id, ff.name, ff.flag_key 
            FROM tenant_features tf
            JOIN feature_flags ff ON tf.feature_id = ff.id
            WHERE tf.tenant_id = ?
        ''', (t['id'],))
        t['alloted_features'] = [dict(row) for row in c.fetchall()]

    conn.close()
    return jsonify({'tenants': tenants, 'features': features})


@app.route('/api/admin/feature-allotment/add', methods=['POST'])
def admin_feature_allotment_add():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    tenant_id = data.get('tenant_id')
    feature_id = data.get('feature_id')
    if not tenant_id or not feature_id:
        return jsonify({'error': 'tenant_id and feature_id are required'}), 400
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT OR IGNORE INTO tenant_features (tenant_id, feature_id) VALUES (?, ?)', (tenant_id, feature_id))
        conn.commit()
        status = 'success'
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    return jsonify({'status': status})


@app.route('/api/admin/feature-allotment/remove', methods=['POST'])
def admin_feature_allotment_remove():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    tenant_id = data.get('tenant_id')
    feature_id = data.get('feature_id')
    if not tenant_id or not feature_id:
        return jsonify({'error': 'tenant_id and feature_id are required'}), 400
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM tenant_features WHERE tenant_id = ? AND feature_id = ?', (tenant_id, feature_id))
        conn.commit()
        status = 'success'
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    return jsonify({'status': status})


@app.route('/api/admin/feature-flags', methods=['POST'])
def admin_create_feature_flag():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    name = data.get('name', '').strip()
    flag_key = data.get('flag_key', '').strip()
    description = data.get('description', '').strip()
    if not name or not flag_key:
        return jsonify({'error': 'name and flag_key are required'}), 400
    if not flag_key.startswith('allot_'):
        flag_key = 'allot_' + flag_key
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO feature_flags (name, flag_key, description) VALUES (?, ?, ?)', (name, flag_key, description))
        conn.commit()
        status = 'success'
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Feature name or flag key already exists'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/admin/feature-flags/<int:flag_id>', methods=['DELETE'])
def admin_delete_feature_flag(flag_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('DELETE FROM feature_flags WHERE id = ?', (flag_id,))
        c.execute('DELETE FROM tenant_features WHERE feature_id = ?', (flag_id,))
        conn.commit()
        status = 'success'
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/papers', methods=['GET'])
def admin_list_papers():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT p.id, p.title, p.slug, p.is_published, p.created_at, p.updated_at, t.username as tenant_username
        FROM markdown_pages p
        JOIN tenants t ON p.tenant_username = t.username
        ORDER BY p.updated_at DESC
    ''')
    papers = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'papers': papers})

@app.route('/api/admin/papers/delete', methods=['POST'])
def admin_delete_paper():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    paper_id = request.json.get('paper_id')
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('DELETE FROM markdown_pages WHERE id = ?', (paper_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/admin/allot-pages', methods=['POST'])
def admin_allot_pages():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
        
    target_type = request.json.get('target_type') # 'username', 'tier', 'global'
    target_value = request.json.get('target_value')
    limit_value = request.json.get('limit')
    
    try:
        limit_val = int(limit_value) if limit_value else None
    except:
        limit_val = None
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        if target_type == 'username':
            c.execute('UPDATE tenants SET granted_pages_limit = ? WHERE username = ?', (limit_val, target_value))
        elif target_type == 'tier':
            c.execute('UPDATE tenants SET granted_pages_limit = ? WHERE tier = ?', (limit_val, target_value))
        elif target_type == 'global':
            c.execute('UPDATE tenants SET granted_pages_limit = ?', (limit_val,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'error': str(e)}), 500
        
    conn.close()
    return jsonify({'status': 'success'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

import os
from flask import request, jsonify, session
import urllib.request
import tempfile
from werkzeug.utils import secure_filename
import boto3

# Add this at the end of app.py

@app.route('/api/photos/upload', methods=['POST'])
def photos_upload():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    if not tenant:
        return jsonify({'error': 'Tenant not found'}), 404
        
    username = tenant['username']
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
        
    if not file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.mp4', '.webm', '.ogg', '.mov')):
        return jsonify({'error': 'Only image and video files are allowed'}), 400

    filename = secure_filename(file.filename)
    tenant_dir = get_tenant_storage_dir()
    dest_path = os.path.join(tenant_dir, filename)
    
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    try:
        file.save(dest_path)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Record in db
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO photos (tenant_username, filename, size_bytes) VALUES (?, ?, ?)',
              (username, filename, file_size))
    conn.commit()
    conn.close()
    
    # Recalculate usage
    recalculate_tenant_usage(tenant_id, username)
    return jsonify({'status': 'success'})

@app.route('/api/photos/list', methods=['GET'])
def photos_list():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    username = session.get('tenant_username')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id, filename, album_id, size_bytes, created_at FROM photos WHERE tenant_username = ? ORDER BY id DESC', (username,))
    rows = c.fetchall()
    conn.close()
    
    return jsonify({'photos': [dict(r) for r in rows]})

import uuid

@app.route('/api/photos/albums/create', methods=['POST'])
def create_album():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    
    name = request.json.get('name')
    if not name:
        return jsonify({'error': 'Name is required'}), 400
        
    username = session.get('tenant_username')
    slug = str(uuid.uuid4())[:8] + '-' + "".join([c if c.isalnum() else '-' for c in name]).lower()
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO photo_albums (tenant_username, name, slug) VALUES (?, ?, ?) RETURNING id, name, slug', (username, name, slug))
    album = c.fetchone()
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'album': dict(album) if album else {}})

@app.route('/api/photos/albums/list', methods=['GET'])
def list_albums():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    username = session.get('tenant_username')
    tenant_id = session.get('tenant_id')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Get viewer's custom_domain_email
    c.execute('SELECT custom_domain_email FROM tenants WHERE id = ?', (tenant_id,))
    row = c.fetchone()
    viewer_email = row[0] if row else None

    # Get albums with photo count owned by the tenant
    c.execute('''
        SELECT a.id, a.name, a.slug, a.visibility, a.created_at,
               COUNT(ap.id) as photo_count
        FROM photo_albums a
        LEFT JOIN album_photos ap ON a.id = ap.album_id
        WHERE a.tenant_username = ?
        GROUP BY a.id, a.name, a.slug, a.visibility, a.created_at
        ORDER BY a.created_at DESC
    ''', (username,))

    albums = [dict(row) for row in c.fetchall()]

    for album in albums:
        c.execute('''
            SELECT filename FROM album_photos 
            WHERE album_id = ? 
            ORDER BY id ASC LIMIT 4
        ''', (album['id'],))
        album['photos'] = [r['filename'] for r in c.fetchall()]
        album['thumbnail'] = album['photos'][0] if album['photos'] else None
        album['is_shared_album'] = False

    shared_albums = []
    if viewer_email:
        # Get shared albums where this user has access (via shared_album_access table)
        c.execute('''
            SELECT a.id, a.name, a.slug, a.visibility, a.created_at,
                   COUNT(ap.id) as photo_count,
                   s.share_token,
                   a.tenant_username as owner_username
            FROM shared_albums s
            JOIN shared_album_access saa ON s.share_token = saa.share_token
            JOIN photo_albums a ON s.album_id = a.id
            LEFT JOIN album_photos ap ON a.id = ap.album_id
            WHERE LOWER(saa.email) = LOWER(?)
            GROUP BY a.id, a.name, a.slug, a.visibility, a.created_at, s.share_token, a.tenant_username
            ORDER BY a.created_at DESC
        ''', (viewer_email,))
        shared_albums = [dict(row) for row in c.fetchall()]
        for sa in shared_albums:
            c.execute('''
                SELECT filename FROM album_photos 
                WHERE album_id = ? 
                ORDER BY id ASC LIMIT 4
            ''', (sa['id'],))
            sa['photos'] = [r['filename'] for r in c.fetchall()]
            sa['thumbnail'] = sa['photos'][0] if sa['photos'] else None
            sa['is_shared_album'] = True

    conn.close()

    combined_albums = albums + shared_albums
    combined_albums.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)

    return jsonify({'albums': combined_albums})

@app.route('/api/photos/albums/add', methods=['POST'])
def add_to_album():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    album_id = request.json.get('album_id')
    filename = request.json.get('filename')
    username = session.get('tenant_username')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Check if album belongs to user
    c.execute('SELECT id FROM photo_albums WHERE id = ? AND tenant_username = ?', (album_id, username))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Album not found or unauthorized'}), 404
        
    # Check if photo already in album
    c.execute('SELECT id FROM album_photos WHERE album_id = ? AND filename = ?', (album_id, filename))
    if c.fetchone():
        conn.close()
        return jsonify({'status': 'success'}) # Already added
        
    c.execute('INSERT INTO album_photos (album_id, filename) VALUES (?, ?)', (album_id, filename))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/photos/albums/view/<slug>', methods=['GET'])
def view_album(slug):
    # This route can be public or private
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('SELECT id, name, visibility, tenant_username FROM photo_albums WHERE slug = ?', (slug,))
    album = c.fetchone()
    
    if not album:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
        
    if album['visibility'] == 'private' and session.get('tenant_username') != album['tenant_username']:
        conn.close()
        return jsonify({'error': 'Unauthorized. This album is private.'}), 401
        
    c.execute('SELECT ap.filename, p.created_at, p.size_bytes FROM album_photos ap INNER JOIN photos p ON ap.filename = p.filename AND p.tenant_username = ? WHERE ap.album_id = ? ORDER BY ap.id DESC', (album['tenant_username'], album['id']))
    photos = [dict(row) for row in c.fetchall()]

    c.execute('SELECT share_token FROM shared_albums WHERE album_id = ?', (album['id'],))
    share_row = c.fetchone()
    share_token = share_row['share_token'] if share_row else None

    allowed_users = []
    if share_token:
        c.execute('''
            SELECT saa.email, t.username
            FROM shared_album_access saa
            LEFT JOIN tenants t ON LOWER(saa.email) = LOWER(t.custom_domain_email)
            WHERE saa.share_token = ?
            ORDER BY saa.granted_at ASC
        ''', (share_token,))
        allowed_users = [{'email': r['email'], 'username': r['username'] or r['email'].split('@')[0]} for r in c.fetchall()]

    conn.close()
    
    return jsonify({
        'album': dict(album),
        'photos': photos,
        'share_token': share_token,
        'allowed_users': allowed_users
    })

@app.route('/api/photos/fetch-url', methods=['POST'])
def photos_fetch_url():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    tenant_id = session.get('tenant_id')
    tenant = get_tenant(tenant_id)
    username = tenant['username']
    
    data = request.json or {}
    url = data.get('url')
    if not url:
        return jsonify({'error': 'Missing url'}), 400
        
    if not is_safe_url(url):
        return jsonify({'error': 'Invalid or forbidden URL'}), 400
        
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read()
            content_type = response.headers.get('Content-Type', 'image/jpeg')
            
        import re
        if 'text/html' in content_type:
            html = content.decode('utf-8', errors='ignore')
            match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
            if match:
                img_url = match.group(1)
                if not is_safe_url(img_url):
                    return jsonify({'error': 'Invalid or forbidden image URL'}), 400
                req2 = urllib.request.Request(img_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req2, timeout=10) as response2:
                    content = response2.read()
                    content_type = response2.headers.get('Content-Type', 'image/jpeg')
                    cd = response2.headers.get('Content-Disposition', '')
                    filename_match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
                    if filename_match:
                        filename = secure_filename(filename_match.group(1))
                    else:
                        filename = secure_filename(img_url.split('/')[-1])
            else:
                return jsonify({'error': 'No image found on that page'}), 400
        else:
            cd = response.headers.get('Content-Disposition', '')
            filename_match = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
            if filename_match:
                filename = secure_filename(filename_match.group(1))
            else:
                filename = secure_filename(url.split('?')[0].split('/')[-1])

        if not filename or '.' not in filename:
            ext = content_type.split('/')[-1] if '/' in content_type else 'jpg'
            if ext == 'jpeg': ext = 'jpg'
            filename = f"downloaded_image.{ext}"
            
        # Ensure filename is not too long for the filesystem (ext4 limit is 255 bytes)
        if len(filename) > 200:
            name_part, ext_part = os.path.splitext(filename)
            filename = name_part[:190] + ext_part
            
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
            
        tenant_dir = get_tenant_storage_dir()
        dest_path = os.path.join(tenant_dir, filename)
        
        with open(tmp_path, 'rb') as src, open(dest_path, 'wb') as dst:
            dst.write(src.read())
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO photos (tenant_username, filename, size_bytes) VALUES (?, ?, ?)',
                  (username, filename, len(content)))
        conn.commit()
        conn.close()
        
        os.unlink(tmp_path)
        recalculate_tenant_usage(tenant_id, username)
        return jsonify({'status': 'success', 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/photos/albums/share', methods=['POST'])
def share_album():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    album_id = request.json.get('album_id')
    sharing_type_param = request.json.get('sharing_type')  # None if not explicitly sent
    if not album_id:
        return jsonify({'error': 'Album ID is required'}), 400
        
    username = session.get('tenant_username')
    
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT id FROM photo_albums WHERE id = ? AND tenant_username = ?', (album_id, username))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Album not found or unauthorized'}), 404
        
    c.execute('SELECT share_token, sharing_type FROM shared_albums WHERE album_id = ?', (album_id,))
    row = c.fetchone()
    if row:
        token = row['share_token']
        # Only update sharing_type if explicitly provided in the request
        if sharing_type_param is not None:
            sharing_type = sharing_type_param
            c.execute('UPDATE shared_albums SET sharing_type = ? WHERE album_id = ?', (sharing_type, album_id))
        else:
            # Read and preserve the existing sharing_type
            sharing_type = row['sharing_type'] or 'public'
    else:
        import secrets
        token = secrets.token_hex(12)
        sharing_type = sharing_type_param or 'public'
        c.execute('INSERT INTO shared_albums (album_id, share_token, sharing_type) VALUES (?, ?, ?)', (album_id, token, sharing_type))
    conn.commit()
    conn.close()
    
    return jsonify({
        'status': 'success',
        'share_link': f'https://photos.echo.oqens.me/shared/{token}',
        'share_token': token,
        'sharing_type': sharing_type
    })

@app.route('/api/photos/albums/shared', methods=['GET'])
def list_shared_albums():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    username = session.get('tenant_username')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT a.id, a.name, a.slug, s.share_token, s.sharing_type, a.created_at,
               COUNT(ap.id) as photo_count
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        LEFT JOIN album_photos ap ON a.id = ap.album_id
        WHERE a.tenant_username = ?
        GROUP BY a.id, a.name, a.slug, s.share_token, s.sharing_type, a.created_at
        ORDER BY a.created_at DESC
    ''', (username,))
    
    albums = [dict(row) for row in c.fetchall()]
    
    for album in albums:
        c.execute('''
            SELECT filename FROM album_photos 
            WHERE album_id = ? 
            ORDER BY id ASC LIMIT 4
        ''', (album['id'],))
        album['photos'] = [r['filename'] for r in c.fetchall()]
        album['thumbnail'] = album['photos'][0] if album['photos'] else None
        
    conn.close()
    return jsonify({'shared_albums': albums})

@app.route('/api/photos/albums/unshare', methods=['POST'])
def unshare_album():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    album_id = request.json.get('album_id')
    username = session.get('tenant_username')
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT id FROM photo_albums WHERE id = ? AND tenant_username = ?', (album_id, username))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Album not found or unauthorized'}), 404
        
    c.execute('DELETE FROM shared_albums WHERE album_id = ?', (album_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/photos/albums/shared/view/<token>', methods=['GET'])
def view_shared_album_public(token):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT a.id, a.name, a.tenant_username, s.sharing_type
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        WHERE s.share_token = ?
    ''', (token,))
    album = c.fetchone()
    
    if not album:
        conn.close()
        return jsonify({'error': 'Shared album not found'}), 404
        
    # Check private sharing authorization
    if album['sharing_type'] == 'private':
        is_owner = (session.get('role') == 'tenant' and session.get('tenant_username') == album['tenant_username'])
        
        authorized = False
        viewer_email = None
        if not is_owner and session.get('role') == 'tenant' and session.get('tenant_id'):
            c.execute('SELECT custom_domain_email FROM tenants WHERE id = ?', (session['tenant_id'],))
            t_row = c.fetchone()
            if t_row:
                viewer_email = t_row['custom_domain_email']
                c.execute('SELECT id FROM shared_album_access WHERE share_token = ? AND LOWER(email) = LOWER(?)', (token, viewer_email))
                if c.fetchone():
                    authorized = True
                    
        if not is_owner and not authorized:
            has_pending = False
            if viewer_email:
                c.execute('SELECT id FROM album_access_requests WHERE share_token = ? AND LOWER(requester_email) = LOWER(?) AND status = \'pending\'', (token, viewer_email))
                if c.fetchone():
                    has_pending = True
            conn.close()
            return jsonify({
                'error': 'private_access_required',
                'album_name': album['name'],
                'owner': album['tenant_username'],
                'has_pending': has_pending
            }), 403
            
    c.execute('''
        SELECT ap.filename, p.created_at, p.size_bytes 
        FROM album_photos ap 
        INNER JOIN photos p ON ap.filename = p.filename AND p.tenant_username = ? 
        WHERE ap.album_id = ? 
        ORDER BY ap.id DESC
    ''', (album['tenant_username'], album['id']))
    photos = [dict(row) for row in c.fetchall()]

    allowed_users = []
    c.execute('''
        SELECT saa.email, t.username
        FROM shared_album_access saa
        LEFT JOIN tenants t ON LOWER(saa.email) = LOWER(t.custom_domain_email)
        WHERE saa.share_token = ?
        ORDER BY saa.granted_at ASC
    ''', (token,))
    allowed_users = [{'email': r['email'], 'username': r['username'] or r['email'].split('@')[0]} for r in c.fetchall()]

    conn.close()
    
    return jsonify({
        'album': {
            'name': album['name'],
            'owner': album['tenant_username']
        },
        'photos': photos,
        'allowed_users': allowed_users
    })

@app.route('/api/photos/albums/request-access', methods=['POST'])
def request_access():
    data = request.json or {}
    share_token = data.get('share_token')
    requester_username = data.get('requester_username', '').strip()
    requester_email = data.get('requester_email', '').strip().lower()
    details = data.get('details', '').strip()
    
    if not share_token or not requester_username or not requester_email:
        return jsonify({'error': 'Missing required fields'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        SELECT a.id, a.name, a.tenant_username, t.custom_domain_email 
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        JOIN tenants t ON a.tenant_username = t.username
        WHERE s.share_token = ?
    ''', (share_token,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Shared album not found'}), 404
        
    album_id, album_name, owner_username, owner_email = row
    
    c.execute('SELECT id FROM album_access_requests WHERE share_token = ? AND LOWER(requester_email) = LOWER(?) AND status = \'pending\'', (share_token, requester_email))
    if c.fetchone():
        conn.close()
        return jsonify({'status': 'pending_exists', 'message': 'You have already requested access. Please wait for the owner to approve.'})
        
    import secrets
    approval_token = secrets.token_hex(24)
    
    c.execute('''
        INSERT INTO album_access_requests (share_token, requester_username, requester_email, details, approval_token)
        VALUES (?, ?, ?, ?, ?)
    ''', (share_token, requester_username, requester_email, details, approval_token))
    conn.commit()
    conn.close()
    
    subject = f"OQENS: Private Album Access Request for '{album_name}'"
    approval_link = f"https://photos.echo.oqens.me/album-request?token={approval_token}"
    html_body = f"""
    <h3>Access Request for Private Album</h3>
    <p>Hello {owner_username},</p>
    <p>A user has requested access to view your private photo album <strong>'{album_name}'</strong>.</p>
    <div style="background: #f8fafc; padding: 16px; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: 24px;">
        <p><strong>Requester Name:</strong> {requester_username}</p>
        <p><strong>Requester Email:</strong> {requester_email}</p>
        <p><strong>Message / Details:</strong> {details or "No details provided."}</p>
    </div>
    <p>Please click the button below to approve or reject this request:</p>
    <p><a href="{approval_link}" style="display: inline-block; padding: 12px 24px; background: #111; color: #fff; text-decoration: none; border-radius: 6px; font-weight: bold;">Review Request</a></p>
    <p>Or copy this link: {approval_link}</p>
    <br>
    <p>Best regards,<br>OQENS Security</p>
    """
    
    send_system_email(owner_email, subject, html_body)
    
    return jsonify({'status': 'success'})

@app.route('/api/photos/albums/request-details', methods=['GET'])
def get_request_details():
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'Token is required'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT r.requester_username, r.requester_email, r.details, r.status, a.name as album_name
        FROM album_access_requests r
        JOIN shared_albums s ON r.share_token = s.share_token
        JOIN photo_albums a ON s.album_id = a.id
        WHERE r.approval_token = ?
    ''', (token,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'Invalid token or request not found'}), 404
        
    return jsonify(dict(row))

@app.route('/api/photos/albums/approve-request', methods=['POST'])
def approve_request():
    data = request.json or {}
    token = data.get('token')
    action = data.get('action')
    
    if not token or action not in ['approve', 'reject']:
        return jsonify({'error': 'Missing or invalid parameters'}), 400
        
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('SELECT share_token, requester_username, requester_email, status FROM album_access_requests WHERE approval_token = ?', (token,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Access request not found'}), 404
        
    share_token, requester_username, requester_email, current_status = row
    
    if current_status != 'pending':
        conn.close()
        return jsonify({'error': f'Request has already been {current_status}'}), 400
        
    new_status = 'approved' if action == 'approve' else 'rejected'
    c.execute('UPDATE album_access_requests SET status = ? WHERE approval_token = ?', (new_status, token))
    
    if action == 'approve':
        c.execute('INSERT INTO shared_album_access (share_token, email) VALUES (?, ?)', (share_token, requester_email))
        
    conn.commit()
    
    c.execute('''
        SELECT a.name, a.tenant_username 
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        WHERE s.share_token = ?
    ''', (share_token,))
    album_row = c.fetchone()
    conn.close()
    
    if album_row:
        album_name, owner_username = album_row
        subject = f"OQENS: Access Request {new_status.capitalize()} for '{album_name}'"
        
        if action == 'approve':
            album_link = f"https://photos.echo.oqens.me/shared/{share_token}"
            body_text = f"""
            <p>Hello {requester_username},</p>
            <p>Your request to view the private album <strong>'{album_name}'</strong> has been <strong>approved</strong> by {owner_username}.</p>
            <p>You can now view the album using the link below:</p>
            <p><a href="{album_link}" style="display: inline-block; padding: 12px 24px; background: #10b981; color: #fff; text-decoration: none; border-radius: 6px; font-weight: bold;">View Shared Album</a></p>
            """
        else:
            body_text = f"""
            <p>Hello {requester_username},</p>
            <p>Your request to view the private album <strong>'{album_name}'</strong> has been declined by {owner_username}.</p>
            """
            
        html_body = f"""
        <h3>Album Access Update</h3>
        {body_text}
        <br>
        <p>Best regards,<br>OQENS Team</p>
        """
        try:
            send_system_email(requester_email, subject, html_body)
        except Exception as e:
            print("Failed to send access request response email:", e)
            
    return jsonify({'status': 'success', 'new_status': new_status})

@app.route('/api/photos/albums/permissions', methods=['GET'])
def get_album_permissions():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
    
    share_token = request.args.get('share_token')
    if not share_token:
        return jsonify({'error': 'Share token is required'}), 400
        
    username = session.get('tenant_username')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Verify the caller is the owner of the album associated with this token
    c.execute('''
        SELECT a.tenant_username, a.name 
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        WHERE s.share_token = ?
    ''', (share_token,))
    row = c.fetchone()
    if not row or row['tenant_username'] != username:
        conn.close()
        return jsonify({'error': 'Unauthorized or album not found'}), 403
        
    # Get all granted users
    c.execute('''
        SELECT saa.email, t.username, saa.granted_at
        FROM shared_album_access saa
        LEFT JOIN tenants t ON LOWER(saa.email) = LOWER(t.custom_domain_email)
        WHERE saa.share_token = ?
        ORDER BY saa.granted_at DESC
    ''', (share_token,))
    
    users = [{'email': r['email'], 'username': r['username'] or r['email'].split('@')[0], 'granted_at': str(r['granted_at'])} for r in c.fetchall()]
    conn.close()
    
    return jsonify({'users': users})

@app.route('/api/photos/albums/permissions/grant', methods=['POST'])
def grant_album_permission():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    share_token = data.get('share_token')
    email = data.get('email')
    
    if not share_token or not email:
        return jsonify({'error': 'Missing share_token or email'}), 400
        
    username = session.get('tenant_username')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Verify ownership
    c.execute('''
        SELECT a.tenant_username 
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        WHERE s.share_token = ?
    ''', (share_token,))
    row = c.fetchone()
    if not row or row['tenant_username'] != username:
        conn.close()
        return jsonify({'error': 'Unauthorized or album not found'}), 403
        
    # Check if already granted
    c.execute('SELECT id FROM shared_album_access WHERE share_token = ? AND LOWER(email) = LOWER(?)', (share_token, email))
    if c.fetchone():
        conn.close()
        return jsonify({'error': 'User already has access'}), 400
        
    # Grant access
    c.execute('INSERT INTO shared_album_access (share_token, email) VALUES (?, ?)', (share_token, email))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Access granted successfully'})

@app.route('/api/photos/albums/permissions/revoke', methods=['POST'])
def revoke_album_permission():
    if session.get('role') != 'tenant':
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json or {}
    share_token = data.get('share_token')
    email = data.get('email')
    
    if not share_token or not email:
        return jsonify({'error': 'Missing share_token or email'}), 400
        
    username = session.get('tenant_username')
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    # Verify ownership
    c.execute('''
        SELECT a.tenant_username 
        FROM shared_albums s
        JOIN photo_albums a ON s.album_id = a.id
        WHERE s.share_token = ?
    ''', (share_token,))
    row = c.fetchone()
    if not row or row['tenant_username'] != username:
        conn.close()
        return jsonify({'error': 'Unauthorized or album not found'}), 403
        
    # Revoke access
    c.execute('DELETE FROM shared_album_access WHERE share_token = ? AND LOWER(email) = LOWER(?)', (share_token, email))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'success', 'message': 'Access revoked successfully'})

# --- Data Collection & Analytics Endpoints ---

def get_ip_location(ip):
    if ip in ('127.0.0.1', 'localhost', '::1') or ip.startswith('192.168.') or ip.startswith('10.'):
        return {'country': 'Local', 'countryCode': 'LOC', 'city': 'Localhost', 'isp': 'Local Network'}
    try:
        import urllib.request
        import json
        url = f"http://ip-api.com/json/{ip}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get('status') == 'success':
                return {
                    'country': data.get('country', 'Unknown'),
                    'countryCode': data.get('countryCode', 'UN'),
                    'city': data.get('city', 'Unknown'),
                    'isp': data.get('isp', 'Unknown')
                }
    except Exception as e:
        print(f"Error doing IP lookup for {ip}: {e}")
    return {'country': 'Unknown', 'countryCode': 'UN', 'city': 'Unknown', 'isp': 'Unknown'}

@app.route('/disclosures')
@app.route('/disclousers')
def serve_disclosures():
    return app.send_static_file('disclosures.html')

@app.route('/api/analytics/collect', methods=['POST'])
def collect_analytics():
    try:
        data = request.json or {}
        share_token = data.get('share_token')
        device_type = data.get('device_type', 'Desktop')
        device_name = data.get('device_name', 'Unknown Device')
        battery_level = data.get('battery_level')
        connection_type = data.get('connection_type')
        source_url = data.get('source_url')
        referrer_url = data.get('referrer_url')
        os_name = data.get('os_name', 'Unknown OS')
        browser_name = data.get('browser_name', 'Unknown Browser')

        if not share_token:
            return jsonify({'error': 'Missing share_token'}), 400

        # Get visitor IP
        visitor_ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For') or request.remote_addr
        if visitor_ip and ',' in visitor_ip:
            visitor_ip = visitor_ip.split(',')[0].strip()

        # Geolocation lookup
        loc = get_ip_location(visitor_ip)

        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # Try to find an existing dataset for this shared album
        c.execute("SELECT id FROM analytics_datasets WHERE target_type = 'shared_album' AND target_value = ?", (share_token,))
        row = c.fetchone()
        
        dataset_id = None
        if row:
            dataset_id = row[0]
        else:
            # Look up album details to see if it exists
            c.execute('''
                SELECT a.name, a.tenant_username
                FROM shared_albums s
                JOIN photo_albums a ON s.album_id = a.id
                WHERE s.share_token = ?
            ''', (share_token,))
            album_row = c.fetchone()
            if album_row:
                album_name, owner = album_row[0], album_row[1]
                dataset_name = f"Album: {album_name} ({owner})"
                
                # Check if dataset name exists (name is UNIQUE)
                c.execute("SELECT id FROM analytics_datasets WHERE name = ?", (dataset_name,))
                name_row = c.fetchone()
                if name_row:
                    dataset_id = name_row[0]
                    c.execute("UPDATE analytics_datasets SET target_type = 'shared_album', target_value = ? WHERE id = ?", (share_token, dataset_id))
                else:
                    c.execute(
                        "INSERT INTO analytics_datasets (name, target_type, target_value) VALUES (?, 'shared_album', ?)",
                        (dataset_name, share_token)
                    )
                    dataset_id = c.lastrowid
            else:
                # Custom URL or untracked/unknown album
                dataset_name = f"Custom: {share_token}"
                c.execute("SELECT id FROM analytics_datasets WHERE name = ?", (dataset_name,))
                name_row = c.fetchone()
                if name_row:
                    dataset_id = name_row[0]
                else:
                    c.execute(
                        "INSERT INTO analytics_datasets (name, target_type, target_value) VALUES (?, 'custom_url', ?)",
                        (dataset_name, share_token)
                    )
                    dataset_id = c.lastrowid

        # Insert log
        c.execute('''
            INSERT INTO analytics_logs (
                dataset_id, ip_address, country, city, isp, 
                device_type, device_name, os_name, browser_name, 
                battery_level, connection_type, source_url, referrer_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            dataset_id, visitor_ip, loc.get('country'), loc.get('city'), loc.get('isp'),
            device_type, device_name, os_name, browser_name,
            battery_level, connection_type, source_url, referrer_url
        ))

        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        print("Error collecting analytics:", e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/collections/datasets', methods=['GET'])
def admin_list_datasets():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT d.id, d.name, d.target_type, d.target_value, d.created_at,
                   (SELECT COUNT(*) FROM analytics_logs l WHERE l.dataset_id = d.id) as logs_count
            FROM analytics_datasets d
            ORDER BY d.created_at DESC
        ''')
        rows = c.fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/collections/shared-albums', methods=['GET'])
def admin_list_shared_albums():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT sa.share_token, pa.name as album_name, pa.tenant_username
            FROM shared_albums sa
            JOIN photo_albums pa ON sa.album_id = pa.id
            ORDER BY pa.created_at DESC
        ''')
        rows = c.fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/collections/datasets/create', methods=['POST'])
def admin_create_dataset():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json or {}
        name = data.get('name')
        target_type = data.get('target_type')
        target_value = data.get('target_value')
        
        if not name or not target_type or not target_value:
            return jsonify({'error': 'Missing fields'}), 400
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO analytics_datasets (name, target_type, target_value) VALUES (?, ?, ?)",
                (name, target_type, target_value)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({'error': 'Dataset name already exists'}), 400
            
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/collections/datasets/delete', methods=['POST'])
def admin_delete_dataset():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        data = request.json or {}
        dataset_id = data.get('id')
        if not dataset_id:
            return jsonify({'error': 'Missing id'}), 400
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM analytics_datasets WHERE id = ?", (dataset_id,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/collections/datasets/analytics', methods=['GET'])
def admin_dataset_analytics():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        dataset_id = request.args.get('id')
        
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        use_filter = dataset_id and dataset_id != 'global'
        
        # 1. Device Type
        q_device_type = '''
            SELECT COALESCE(device_type, 'Unknown'), COUNT(*) 
            FROM analytics_logs 
            {where}
            GROUP BY device_type
        '''
        if use_filter:
            c.execute(q_device_type.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_device_type.format(where=""))
        device_types = {row[0]: row[1] for row in c.fetchall()}
        
        # 2. Device Name
        q_device_name = '''
            SELECT COALESCE(device_name, 'Unknown'), COUNT(*) 
            FROM analytics_logs 
            {where}
            GROUP BY device_name 
            ORDER BY COUNT(*) DESC LIMIT 10
        '''
        if use_filter:
            c.execute(q_device_name.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_device_name.format(where=""))
        device_names = {row[0]: row[1] for row in c.fetchall()}
        
        # 3. Countries
        q_countries = '''
            SELECT COALESCE(country, 'Unknown'), COUNT(*) 
            FROM analytics_logs 
            {where}
            GROUP BY country 
            ORDER BY COUNT(*) DESC LIMIT 10
        '''
        if use_filter:
            c.execute(q_countries.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_countries.format(where=""))
        countries = {row[0]: row[1] for row in c.fetchall()}
        
        # 4. Source URLs
        q_source_urls = '''
            SELECT COALESCE(source_url, 'Unknown'), COUNT(*) 
            FROM analytics_logs 
            {where}
            GROUP BY source_url 
            ORDER BY COUNT(*) DESC LIMIT 10
        '''
        if use_filter:
            c.execute(q_source_urls.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_source_urls.format(where=""))
        source_urls = {row[0]: row[1] for row in c.fetchall()}
        
        # 5. ISPs
        q_isps = '''
            SELECT COALESCE(isp, 'Unknown'), COUNT(*) 
            FROM analytics_logs 
            {where}
            GROUP BY isp 
            ORDER BY COUNT(*) DESC LIMIT 10
        '''
        if use_filter:
            c.execute(q_isps.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_isps.format(where=""))
        isps = {row[0]: row[1] for row in c.fetchall()}

        # 6. Battery Level ranges
        q_battery = '''
            SELECT battery_level 
            FROM analytics_logs 
            {where}
        '''
        if use_filter:
            c.execute(q_battery.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_battery.format(where=""))
        battery_rows = c.fetchall()
        
        battery_ranges = {
            '0-20%': 0,
            '21-50%': 0,
            '51-80%': 0,
            '81-100%': 0,
            'Not Available': 0
        }
        for r in battery_rows:
            level = r[0]
            if level is None:
                battery_ranges['Not Available'] += 1
            else:
                pct = level * 100
                if pct <= 20:
                    battery_ranges['0-20%'] += 1
                elif pct <= 50:
                    battery_ranges['21-50%'] += 1
                elif pct <= 80:
                    battery_ranges['51-80%'] += 1
                else:
                    battery_ranges['81-100%'] += 1

        # 7. Recent logs preview (last 20 logs)
        q_recent = '''
            SELECT ip_address, country, city, device_name, os_name, created_at 
            FROM analytics_logs 
            {where}
            ORDER BY created_at DESC 
            LIMIT 20
        '''
        if use_filter:
            c.execute(q_recent.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_recent.format(where=""))
        
        recent_logs = []
        for r in c.fetchall():
            recent_logs.append({
                'ip_address': r[0],
                'country': r[1],
                'city': r[2],
                'device_name': r[3],
                'os_name': r[4],
                'created_at': r[5]
            })

        # 8. Daily Timeline trend (downloads/hits per day for last 14 days)
        # We group by the date string format YYYY-MM-DD
        q_timeline = '''
            SELECT DATE(created_at) as log_date, COUNT(*) 
            FROM analytics_logs 
            {where}
            GROUP BY log_date
            ORDER BY log_date ASC
        '''
        if use_filter:
            c.execute(q_timeline.format(where="WHERE dataset_id = ?"), (dataset_id,))
        else:
            c.execute(q_timeline.format(where=""))
        
        daily_trends = {str(row[0]): row[1] for row in c.fetchall() if row[0] is not None}

        conn.close()
        
        return jsonify({
            'device_types': device_types,
            'device_names': device_names,
            'countries': countries,
            'source_urls': source_urls,
            'isps': isps,
            'battery_ranges': battery_ranges,
            'recent_logs': recent_logs,
            'daily_trends': daily_trends
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/collections/datasets/download', methods=['GET'])
def admin_download_dataset_csv():
    if session.get('role') != 'admin':
        return "Unauthorized", 401
    try:
        dataset_id = request.args.get('id')
        if not dataset_id:
            return "Missing dataset id", 400
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        use_filter = dataset_id and dataset_id not in ['global', 'all']
        
        if use_filter:
            c.execute("SELECT name FROM analytics_datasets WHERE id = ?", (dataset_id,))
            dataset_row = c.fetchone()
            dataset_name = dataset_row['name'] if dataset_row else f"dataset_{dataset_id}"
            
            c.execute('''
                SELECT id, ip_address, country, city, isp, device_type, device_name, 
                       os_name, browser_name, battery_level, connection_type, 
                       source_url, referrer_url, created_at
                FROM analytics_logs
                WHERE dataset_id = ?
                ORDER BY created_at DESC
            ''', (dataset_id,))
        else:
            dataset_name = "all_visitor_analytics"
            c.execute('''
                SELECT id, ip_address, country, city, isp, device_type, device_name, 
                       os_name, browser_name, battery_level, connection_type, 
                       source_url, referrer_url, created_at
                FROM analytics_logs
                ORDER BY created_at DESC
            ''')
            
        rows = c.fetchall()
        conn.close()
        
        import csv
        import io
        from flask import Response
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        writer.writerow([
            'Log ID', 'IP Address', 'Country', 'City', 'ISP', 'Device Type', 
            'Device Name', 'OS Name', 'Browser Name', 'Battery Level', 
            'Connection Type', 'Source URL', 'Referrer URL', 'Timestamp'
        ])
        
        for r in rows:
            battery = f"{int(r['battery_level'] * 100)}%" if r['battery_level'] is not None else "N/A"
            writer.writerow([
                r['id'],
                r['ip_address'],
                r['country'],
                r['city'],
                r['isp'],
                r['device_type'],
                r['device_name'],
                r['os_name'],
                r['browser_name'],
                battery,
                r['connection_type'],
                r['source_url'],
                r['referrer_url'],
                r['created_at']
            ])
            
        response = Response(output.getvalue(), mimetype='text/csv')
        safe_name = "".join(x for x in dataset_name if x.isalnum() or x in (' ', '_', '-')).strip().replace(' ', '_')
        response.headers["Content-Disposition"] = f"attachment; filename={safe_name}_export.csv"
        return response
    except Exception as e:
        return str(e), 500
