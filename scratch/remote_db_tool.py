import psycopg2
import sys

def main():
    conn = psycopg2.connect('postgresql://oqens_user:oqens_pass@localhost/oqens')
    c = conn.cursor()
    c.execute("SELECT id, name, subject, body FROM mail_templates")
    print("Mail templates:")
    for r in c.fetchall():
        print(f"ID: {r[0]}")
        print(f"Name: {r[1]}")
        print(f"Subject: {r[2]}")
        print(f"Body:\n{r[3]}\n" + "-"*50)
    conn.close()

if __name__ == '__main__':
    main()
