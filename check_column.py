from sqlalchemy import create_engine, text

engine = create_engine('postgresql+psycopg://postgres:127264@127.0.0.1:5432/hr_system')
conn = engine.connect()
result = conn.execute(text("SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_name = 'interviews' AND column_name = 'result'"))
for row in result:
    print(f"column: {row[0]}, type: {row[1]}, udt: {row[2]}")
conn.close()
