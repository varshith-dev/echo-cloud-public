import sqlite3
conn = sqlite3.connect('/opt/dashboard/oqens.db')
c = conn.cursor()
c.execute('SELECT id, username, secret_code, cloud_id, api_key, custom_domain_email FROM tenants')
for row in c.fetchall():
    print(row)
conn.close()
