import os
from flask import Flask, jsonify, request, session, redirect, url_for, send_from_directory
import time
import subprocess
import pg_wrapper as sqlite3
import string
import datetime
import random
import secrets
import hashlib
from functools import wraps

app = Flask(__name__, static_folder='static')
app.secret_key = secrets.token_hex(32)
app.permanent_session_lifetime = datetime.timedelta(hours=48)

DB_FILE = os.environ.get('DATABASE_URL', 'postgresql://oqens_user:oqens_pass@localhost/oqens')
ADMIN_SECRET = '6069'
