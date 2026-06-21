import psycopg2
import sys

def fix_sequences():
    conn = psycopg2.connect('postgresql://oqens_user:oqens_pass@localhost/oqens')
    conn.autocommit = True
    c = conn.cursor()

    tables = [
        "referral_links",
        "shared_albums",
        "analytics_datasets",
        "analytics_logs",
        "photo_albums",
        "photos",
        "album_access_requests"
    ]

    for t in tables:
        try:
            seq_name = f"{t}_id_seq"
            c.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq_name} OWNED BY {t}.id")
            c.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {t}")
            max_id = c.fetchone()[0]
            c.execute(f"SELECT setval('{seq_name}', {max_id}, false)")
            c.execute(f"ALTER TABLE {t} ALTER COLUMN id SET DEFAULT nextval('{seq_name}')")
            print(f"Fixed {t}")
        except Exception as e:
            print(f"Error on {t}: {e}")

if __name__ == "__main__":
    fix_sequences()
