import sys
sys.path.insert(0, '/opt/dashboard')
import pg_wrapper as sqlite3
import requests

DB = "dbname=oqens user=oqens_user password=oqens_pass host=localhost"

# Step 1: Simulate admin soft-delete of testretry99 (id=35)
conn = sqlite3.connect(DB)
c = conn.cursor()
c.execute("UPDATE tenants SET username='testretry99_deleted_35', custom_domain_email='testretry99@example.com_deleted_35', is_deleted=1, billing_status='Deleted' WHERE id=35")
conn.commit()
conn.close()
print("Step 1: Simulated delete done")

# Step 2: Re-register with same credentials
r = requests.post('http://localhost:5000/api/auth/signup', json={
    'username': 'testretry99',
    'email': 'testretry99@example.com',
    'password': 'test1234',
    'plan': 'free'
})
print("Step 2 Re-signup:", r.status_code, r.text[:300])

# Step 3: Cleanup
if r.status_code == 200:
    conn2 = sqlite3.connect(DB)
    c2 = conn2.cursor()
    c2.execute("DELETE FROM tenants WHERE username='testretry99'")
    conn2.commit()
    conn2.close()
    print("Step 3: Cleaned up new test account")
