import pg_wrapper as sqlite3
import os

conn = sqlite3.connect("dbname=oqens user=postgres")
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS photo_albums (
    id SERIAL PRIMARY KEY,
    tenant_username TEXT,
    name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')

c.execute('''CREATE TABLE IF NOT EXISTS photos (
    id SERIAL PRIMARY KEY,
    tenant_username TEXT,
    filename TEXT,
    album_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    size_bytes BIGINT
)''')

c.execute('''CREATE TABLE IF NOT EXISTS shared_albums (
    id SERIAL PRIMARY KEY,
    album_id INTEGER,
    share_token TEXT UNIQUE
)''')

conn.commit()
conn.close()
print("Photos tables created.")
