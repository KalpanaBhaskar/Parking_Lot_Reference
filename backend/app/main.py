"""
main.py — Application Entry Point & Server Startup
----------------------------------------------------
This file is the "brain" of the FastAPI application. It:
  1. Creates all database tables on startup
  2. Seeds dummy data (if the DB is empty) for testing
  3. Builds the in-memory heaps from the current DB state
  4. Registers all API routers (parking, waitlist, reservations)
  5. Provides a root health-check endpoint

HOW TO RUN:
    uvicorn app.main:app --reload
    Then visit http://localhost:8000/docs for interactive API docs.

WHAT CHANGED FROM ORIGINAL:
    - FIXED: Import path "from app.routers import parking" → "services as parking"
      (the file is named services.py, not parking.py)
    - ADDED: Import and registration of waitlist and reservations routers
    - ADDED: Reservation model import (so its table gets created)
    - CHANGED: heap_manager.push_slot() now takes distance_from_entrance
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlmodel import Session, select

# Import your database tools
from app.database.session import engine, create_db_and_tables
# CHANGED: Added Reservation to the import so its table is created by create_all()
# WHY? SQLModel.metadata.create_all() only creates tables for models that
# have been imported. If we don't import Reservation here, the Reservations
# table won't be created in PostgreSQL on startup.
from app.database.models import ParkingSlot, VehicleSlotType, Reservation

# Import your algorithm and routes
from app.services.heap_manager import heap_manager
# FIXED: "from app.routers import parking" → "from app.routers import services as parking"
# WHY? The file is called services.py, not parking.py. Python imports
# must match the actual filename. The "as parking" alias keeps the rest
# of the code working (parking.router still works).
from app.routers import services as parking
# ADDED: Import the new feature routers
from app.routers import waitlist       # Waitlist feature endpoints
from app.routers import reservations   # Reservation feature endpoints

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Create all tables in PostgreSQL if they don't exist
    print("Initializing Database Tables...")
    create_db_and_tables()

    # 2. Open a temporary startup session
    with Session(engine) as session:
        
        # --- OPTIONAL SEEDER FOR TESTING ---
        # Check if the database has any slots at all
        existing_slots = session.exec(select(ParkingSlot)).first()
        
        if not existing_slots:
            print("Database is empty. Injecting dummy parking slots for testing...")
            dummy_slots = [
                # 3 Regular Slots
                ParkingSlot(slot_type=VehicleSlotType.Regular, distance_from_entrance=15),
                ParkingSlot(slot_type=VehicleSlotType.Regular, distance_from_entrance=20),
                ParkingSlot(slot_type=VehicleSlotType.Regular, distance_from_entrance=25),
                # 2 Electric Slots
                ParkingSlot(slot_type=VehicleSlotType.Electric, distance_from_entrance=10),
                ParkingSlot(slot_type=VehicleSlotType.Electric, distance_from_entrance=12),
                # 1 Handicapped Slot
                ParkingSlot(slot_type=VehicleSlotType.Handicapped, distance_from_entrance=2),
            ]
            session.add_all(dummy_slots)
            session.commit()
            print("Dummy slots added successfully!")
        # -----------------------------------

        # 3. Query all EMPTY slots to populate your in-memory heaps
        print("Building priority heaps from database state...")
        statement = select(ParkingSlot).where(ParkingSlot.is_empty == True)
        empty_slots = session.exec(statement).all()
        
        count = 0
        for slot in empty_slots:
            # CHANGED: Now passing distance_from_entrance as the third argument
            # WHY? The heap sorts by distance (closer slots first).
            # Previously only passed (slot_type, slotID), so the heap
            # sorted by ID (arbitrary). Now it sorts by proximity to
            # entrance — which is the correct behavior for the problem.
            heap_manager.push_slot(slot.slot_type, slot.slotID, slot.distance_from_entrance)
            count += 1
            
        print(f"Startup Complete: {count} available slots loaded into heaps in O(N log N) time.")
    
    # 4. Yield control back to FastAPI so it can start accepting API requests
    yield
    
    # (Optional) Teardown logic goes here, executing when the server shuts down
    print("Shutting down Parking Lot System...")


# Initialize the core FastAPI Application
app = FastAPI(
    title="Motorq Parking Lot System",
    description="Dynamic API for managing parking slot availability and billing.",
    version="1.0.0",
    lifespan=lifespan
)

# Register your routing endpoints
app.include_router(parking.router)
# ADDED: Register the new feature routers
# WHY separate include_router calls?
#   Each router handles one feature. FastAPI merges them into the app.
#   The endpoints are prefixed by each router's own prefix:
#     parking.router   → /api/parking/...
#     waitlist.router   → /api/waitlist/...
#     reservations.router → /api/reservations/...
app.include_router(waitlist.router)
app.include_router(reservations.router)

# A simple root endpoint to verify the server is alive
@app.get("/")
def root():
    return {
        "status": "Online",
        "message": "Welcome to the Parking Lot System API. Navigate to /docs to test the endpoints."
    }