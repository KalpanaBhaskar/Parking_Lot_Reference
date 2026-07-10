from sqlmodel import SQLModel,Session,create_engine
from typing import Annotated
from fastapi import Depends

# Format: postgresql://<username>:<password>@<host>:<port>/<database_name>
DATABASE_URL = "postgresql://postgres:H@num@n999@localhost:5432/parking_lot_db"

engine = create_engine(DATABASE_URL, echo=True)

# Table Creation Function
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]  #[The_Type, The_Extra_Instructions]