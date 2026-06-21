import requests

# Step 1: Delete test account (simulate admin delete via API)
r = requests.delete('http://localhost:5000/api/admin/tenants/35',
    cookies={'session': 'skip'})
print("Delete:", r.status_code, r.text[:100])

# Step 2: Re-register with same email/username
r2 = requests.post('http://localhost:5000/api/auth/signup', json={
    'username': 'testretry99',
    'email': 'testretry99@example.com',
    'password': 'test1234',
    'plan': 'free'
})
print("Re-signup:", r2.status_code, r2.text[:200])
