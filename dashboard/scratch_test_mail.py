import urllib.request
import urllib.error
import json
import os
import pg_wrapper as sqlite3

db_url = os.environ.get('DATABASE_URL', 'postgresql://oqens_user:oqens_pass@localhost/oqens')
conn = sqlite3.connect(db_url)
c = conn.cursor()
c.execute("SELECT key, value FROM system_config WHERE key IN ('mailman_access_code', 'mailman_secret_key', 'mailman_base_url')")
config = {r[0]: r[1] for r in c.fetchall()}
conn.close()

access_code = config.get('mailman_access_code')
secret_key = config.get('mailman_secret_key')
base_url = config.get('mailman_base_url', 'https://mailman.oqens.me')

print("Using URL:", base_url)
print("Access Code:", access_code)
print("Secret Key:", secret_key)

payload = {
    'to': 'varshithpaladugu07@gmail.com',
    'recipientName': 'Varshith',
    'fromName': 'OQENS Security',
    'subject': 'Test Mail',
    'html': '<p>This is a test mail</p>'
}

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

try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("Success! Response:", resp.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print("HTTP Error:", e.code, e.reason)
    try:
        body = e.read().decode('utf-8')
        print("Body:", body)
    except Exception as ex:
        print("Failed to read body:", ex)
except Exception as e:
    print("Other Exception:", e)
