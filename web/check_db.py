from booking import init_schema, db

init_schema()
with db() as conn:
    for table in ("booking_slots", "booking_reservations"):
        count = conn.execute("SELECT count(*) AS n FROM " + table).fetchone()["n"]
        print(table + ": " + str(count) + " filas")
print("PostgreSQL conectado; esquema disponible.")
