
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


# DATABASE_URL is validated in app.config.validate_env(); ensure it's present
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Set DATABASE_URL in environment before starting the application.")

# Create engine (expects PostgreSQL URL as validated in config)
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
