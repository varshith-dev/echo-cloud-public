import requests

# Test 1: Fresh signup
r = requests.post('http://localhost:5000/api/auth/signup', json={
    'username': 'testretry99',
    'email': 'testretry99@example.com',
    'password': 'test1234',
    'plan': 'free'
})
print("Signup test:", r.status_code, r.text[:200])

# Test 2: Pricing API
r2 = requests.get('http://localhost:5000/api/system/pricing')
print("Pricing test:", r2.status_code, r2.text[:200])
