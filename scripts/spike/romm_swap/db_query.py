"""Run one parameterized query against RomM's database. Runs INSIDE the RomM
container (spike.py pipes it to the image's Python), with the connector and
the DB_* settings RomM itself uses.

Usage: python - '{"query": "SELECT ... WHERE id = ?", "params": [5]}'

Prints the rows as a JSON list of objects, every value a string and NULL as
"NULL", the way the mariadb client prints them.
"""
import json
import os
import sys

import mariadb


def main():
    request = json.loads(sys.argv[1])
    conn = mariadb.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWD"],
        database=os.environ["DB_NAME"],
    )
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(request["query"], tuple(request["params"]))
        rows = [
            {key: "NULL" if value is None else str(value) for key, value in row.items()}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
