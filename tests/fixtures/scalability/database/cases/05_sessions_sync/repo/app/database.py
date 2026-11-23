from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = "postgresql://app:s3cret@db.internal:5432/shop"

engine = create_engine(DATABASE_URL, pool_size=5, max_overflow=10, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session():
    with Session(engine) as session:
        yield session


def get_tx():
    with SessionLocal.begin() as session:
        yield session
