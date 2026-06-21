import sqlite3
import datetime

conn = sqlite3.connect('/opt/dashboard/oqens.db')
c = conn.cursor()
now = datetime.datetime.utcnow().isoformat() + 'Z'

c.execute('INSERT INTO status_incidents (title, description, status, created_at) VALUES (?, ?, ?, ?)', 
          ('Scheduled Database Migration', 'We are currently upgrading our core infrastructure to PostgreSQL to enable Multi-Node High Availability Load Balancing. The main dashboard and APIs will be temporarily paused for roughly 10 minutes to safely migrate data.', 'Investigating', now))

c.execute('INSERT INTO status_metrics (checked_at, api_status, cos_status, nginx_status, db_status) VALUES (?, ?, ?, ?, ?)', 
          (now, 'maintenance', 'maintenance', 'operational', 'maintenance'))

conn.commit()
conn.close()
print("Maintenance logged")
