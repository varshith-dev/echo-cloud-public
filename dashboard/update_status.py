import sqlite3
import datetime

conn = sqlite3.connect('/opt/dashboard/oqens.db')
c = conn.cursor()
now = datetime.datetime.utcnow().isoformat() + 'Z'

c.execute('INSERT INTO status_incidents (title, description, status, created_at) VALUES (?, ?, ?, ?)', 
          ('Dashboard API Degraded', 'A brief deployment issue caused partial API downtime resulting in some Connection Failed errors for dashboards. The issue has been identified and completely resolved.', 'Resolved', now))

c.execute('INSERT INTO status_metrics (checked_at, api_status, cos_status, nginx_status, db_status) VALUES (?, ?, ?, ?, ?)', 
          (now, 'degraded', 'operational', 'operational', 'operational'))

conn.commit()
conn.close()
print("Status updated successfully")
