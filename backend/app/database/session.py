import os
from pathlib import Path
from typing import Annotated
from dotenv import load_dotenv
from fastapi import Depends
from sqlmodel import SQLModel, Session, create_engine
load_dotenv(Path(__file__).resolve().parents[3] / ".env")

# Format: postgresql://<username>:<password>@<host>:<port>/<database_name>
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set in the .env file")

engine = create_engine(DATABASE_URL, echo=True)

# Table Creation Function
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]