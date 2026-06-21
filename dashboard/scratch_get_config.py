import pg_wrapper as sqlite3
import os

db_url = os.environ.get('DATABASE_URL', 'postgresql://oqens_user:oqens_pass@localhost/oqens')
conn = sqlite3.connect(db_url)
c = conn.cursor()
c.execute("SELECT key, value FROM system_config WHERE key LIKE 'mailman%'")
for r in c.fetchall():
    print(r)
conn.close()
