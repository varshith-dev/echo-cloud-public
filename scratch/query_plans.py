import sys
sys.path.append('/opt/dashboard')
import pg_wrapper as sqlite3
conn = sqlite3.connect('postgresql://oqens_user:oqens_pass@localhost/oqens')
c = conn.cursor()
c.execute('SELECT id, name, subject FROM mail_templates')
for row in c.fetchall():
    print(row)
conn.close()
