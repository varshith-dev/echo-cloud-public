import pg_wrapper as sqlite3
import os

conn = sqlite3.connect("dbname=oqens user=postgres")
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS feature_flags (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE,
    flag_key TEXT UNIQUE,
    description TEXT
)''')

c.execute('''CREATE TABLE IF NOT EXISTS tenant_features (
    tenant_id INTEGER,
    feature_id INTEGER,
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, feature_id)
)''')

# Insert defaults
c.execute('''INSERT INTO feature_flags (id, name, flag_key, description) 
             VALUES (1, 'Collections Feature', 'allot_collections', 'Organize papers into collections') 
             ON CONFLICT DO NOTHING''')

c.execute('''INSERT INTO feature_flags (id, name, flag_key, description) 
             VALUES (2, 'Custom Backgrounds', 'allot_backgrounds', 'Set custom CSS backgrounds for papers') 
             ON CONFLICT DO NOTHING''')

conn.commit()
conn.close()
print("Tables created.")
