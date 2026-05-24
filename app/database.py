import os
import pathlib
from dotenv import load_dotenv

# Load .env from the project root (two levels up from this file)
BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gameclub_v2.db")

# Ensure DATABASE_URL points to PostgreSQL
if not DATABASE_URL.startswith("postgresql"):
    raise RuntimeError("DATABASE_URL must be a PostgreSQL URL, e.g., postgresql://user:pass@host:port/dbname")
# Create PostgreSQL engine (no SQLite-specific args)
engine = create_engine(DATABASE_URL, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
