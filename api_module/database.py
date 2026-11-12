# api_module/database.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

# Fetch variables
USER = os.getenv("user")
PASSWORD = os.getenv("password")
HOST = os.getenv("host")
PORT = os.getenv("port")
DBNAME = os.getenv("dbname")

if not (USER and PASSWORD and HOST and PORT and DBNAME):
    raise RuntimeError(f"DB env missing. Got user={USER!r} host={HOST!r} port={PORT!r} db={DBNAME!r}")


# Construct the SQLAlchemy connection string
DATABASE_URL = f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{DBNAME}?sslmode=require"

# Create the SQLAlchemy engine
engine = create_engine(DATABASE_URL)

# Test the connection
"""
try:
    with engine.connect() as connection:
        print("Connection successful!")
except Exception as e:
    print(f"Failed to connect: {e}")
"""

# Keep benchmark cache transactions independent of the main request pool.
# Four connections maximum: callers cannot exhaust their own pool while waiting
# for a second connection to finish a cache refresh/ranking transaction.
benchmark_engine = create_engine(
    DATABASE_URL, pool_pre_ping=True, pool_size=2, max_overflow=2, pool_timeout=20,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()