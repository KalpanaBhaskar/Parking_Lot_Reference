"""
services.py — Core Parking API Endpoints
------------------------------------------
WHY THIS FILE EXISTS:
    This is the MAIN router for the parking lot system. It contains the
    two most critical operations:
      1. Parking a vehicle  (POST /api/parking/park)
      2. Exiting a vehicle  (POST /api/parking/exit)
      3. Checking availability (GET /api/parking/availability)  ← ADDED

HOW FASTAPI ROUTING WORKS:
    - An APIRouter groups related endpoints under a common URL prefix.
    - Each endpoint is a Python function decorated with @router.get/post/etc.
    - FastAPI automatically:
      a. Validates the request body using Pydantic models
      b. Injects dependencies (like the database session) via Depends()
      c. Converts the return dict to a JSON response
    - The router is registered in main.py: app.include_router(parking.router)

WHAT CHANGED FROM THE ORIGINAL:
    - FIXED: "cost_per_hour" → "cost_per_hr" (must match model field name)
    - ADDED: normalize_plate() helper for consistent DB lookups
    - ADDED: try/except with session.rollback() around DB commits
    - ADDED: GET /api/parking/availability endpoint
    - ADDED: Reservation check in park_vehicle (honors pre-reserved slots)
    - ADDED: Waitlist auto-assign in exit_vehicle (serves waiting vehicles)
    - ADDED: Overstay fine calculation in exit_vehicle
    - ADDED: Null check for slot in exit_vehicle (defensive programming)
"""

import math
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app.database.models import VehicleSlotType, Ticket , ParkingSlot, Reservation  # ADDED: Reservation import
from app.database.session import SessionDep
from app.services.heap_manager import heap_manager
# ADDED: Import the waitlist manager for auto-assigning freed slots to waiting vehicles
from app.services.waitlist_manager import waitlist_manager
# ADDED: Import the fine calculator for overstay penalty computation
from app.services.fine_calculator import calculate_overstay_fine


# Create a router instance
# WHY prefix="/api/parking"?
#   All endpoints in this file will start with /api/parking/...
#   This keeps URLs organized and follows REST conventions.
# WHY tags=["Parking Operations"]?
#   Tags group endpoints in the Swagger UI (/docs page) under a
#   collapsible section, making the API documentation easier to navigate.
router = APIRouter(prefix="/api/parking",tags=["Parking Operations"])


# ──────────────────────────────────────────────────────────────
# REQUEST SCHEMAS (Pydantic Models)
# ──────────────────────────────────────────────────────────────
# WHY separate request models (not just raw parameters)?
#   FastAPI uses Pydantic models to AUTOMATICALLY validate the incoming
#   JSON request body. If a required field is missing or has the wrong
#   type, FastAPI returns a 422 Unprocessable Entity error with detailed
#   error messages — BEFORE your endpoint code even runs. This saves
#   you from writing manual validation code.

class EntryRequest(BaseModel):
    license_plate : str             # The vehicle's license plate (e.g., "KA01AB1234")
    vehicle_type: VehicleSlotType   # Must be one of: "regular", "electric", "handicapped"

class ExitRequest(BaseModel):
    license_plate: str              # The plate of the vehicle that wants to exit


# ──────────────────────────────────────────────────────────────
# HELPER: License Plate Normalization
# ──────────────────────────────────────────────────────────────
# ADDED: This helper ensures the plate format matches what the DB stores.
#
# WHY is this needed?
#   The Ticket model has a @field_validator that cleans the plate on
#   CREATION (removes spaces, converts to uppercase). But when we
#   SEARCH for a ticket on exit, we need the search term to match
#   the cleaned format. Otherwise:
#
#   Entry: user sends "ka 01 ab 1234"
#          → @field_validator cleans it → stored as "KA01AB1234"
#   Exit:  user sends "ka 01 ab 1234"
#          → we search WHERE license_plate = "ka 01 ab 1234"
#          → NO MATCH! (DB has "KA01AB1234")
#
#   By normalizing BEFORE the search, both sides match correctly.
def normalize_plate(plate: str) -> str:
    """Remove spaces and convert to uppercase to match DB storage format."""
    return plate.replace(" ", "").upper()


# ──────────────────────────────────────────────────────────────
# ENDPOINT: GET /api/parking/availability
# ──────────────────────────────────────────────────────────────
# ADDED: The problem statement requires "real-time slot availability."
# This endpoint fulfills that requirement.
#
# WHY a GET request (not POST)?
#   GET = reading data without modifying anything (idempotent).
#   POST = creating/modifying data. Checking availability is read-only,
#   so GET is the correct HTTP method.
#
# WHY read from the heap (not the database)?
#   Speed. The heap is in-memory, so len() is O(1) per type.
#   A database query would be O(N) and require a DB connection.
#   The heap is always in sync with the DB (we maintain it carefully).
@router.get("/availability")
def get_availability():
    """
    Returns the current count of available (empty) slots for each
    vehicle type. Reads directly from the in-memory heap.

    Example response:
    {
        "message": "Current slot availability",
        "available_slots": {
            "regular": 3,
            "electric": 2,
            "handicapped": 1
        }
    }
    """
    availability = heap_manager.get_availability()
    return {
        "message": "Current slot availability",
        "available_slots": availability
    }


# ──────────────────────────────────────────────────────────────
# ENDPOINT: POST /api/parking/park
# ──────────────────────────────────────────────────────────────
@router.post("/park")
def park_vehicle(request: EntryRequest, session: SessionDep):
    """
    Park a vehicle in the nearest available slot of its type.

    Flow:
    1. Normalize the license plate for consistent DB lookups
    2. Check if the vehicle has a reservation → use that reserved slot
    3. If no reservation → pop the nearest slot from the min-heap
    4. If no slots available at all → reject with 409 (suggest waitlist)
    5. Create a Ticket record in the database
    6. Return the ticket details to the client

    WHY this order?
        Reservations are checked FIRST because a reserved slot is
        already "claimed" — it shouldn't be given to anyone else.
        If there's no reservation, we fall back to the normal heap flow.
    """

    # ADDED: Normalize plate so it matches the format stored in the database
    plate = normalize_plate(request.license_plate)

    # ADDED: Check if this vehicle has an active, non-expired reservation
    # WHY check reservations before the heap?
    #   If someone reserved a slot, we MUST honor it. The slot was already
    #   popped from the heap when the reservation was created, so it's not
    #   in the heap anymore. We use the reserved slotID directly.
    reservation = session.exec(
        select(Reservation).where(
            Reservation.license_plate == plate,          # Match this vehicle
            Reservation.is_active == True,               # Reservation still active
            Reservation.vehicle_type == request.vehicle_type,  # Correct type
            Reservation.expires_at > datetime.now(timezone.utc)  # Not expired
        )
    ).first()

    if reservation:
        # Use the slot that was reserved for this vehicle
        best_slot_id = reservation.slotID
        # Mark reservation as consumed (no longer active)
        reservation.is_active = False
        session.add(reservation)
    else:
        # No reservation → normal flow: pop the nearest available slot
        best_slot_id = heap_manager.pop_slot(request.vehicle_type)

    #check slot availability using heap
    if not best_slot_id:
        raise HTTPException(
            # CHANGED: 400 → 409 Conflict
            # WHY 409? HTTP 409 means "the request conflicts with the current
            # state of the server" — i.e., the lot is full. 400 means "bad
            # request" which implies the CLIENT made an error. But the client's
            # request is valid — it's the lot's state that prevents it.
            status_code=409,
            detail = f"Parking lot is full for {request.vehicle_type.value} vehicles. "
                     f"Use POST /api/waitlist/join to join the waitlist."
        )
    #fetch slot from DB
    slot = session.get(ParkingSlot, best_slot_id)
    if not slot:
        raise HTTPException(
            status_code=500, 
            detail = "Heap-database sync error. Slot exists in heap but not in DB.")
    slot.is_empty = False

    new_ticket = Ticket(
        license_plate= plate,          # CHANGED: use normalized plate instead of raw input
        vehicle_type= request.vehicle_type,
        slotID= best_slot_id,
        cost_per_hr = 50.0
    )

    # ADDED: try/except around database commit
    # WHY?
    #   If the DB commit fails (most commonly because of the partial unique
    #   index — a vehicle with this plate is already parked), we need to:
    #   1. ROLLBACK the failed transaction (undo any partial DB changes)
    #   2. PUSH the slot BACK into the heap (we already popped it out)
    #   3. Return a CLEAR error message to the client
    #
    # Without this, a failed commit would leave the slot "lost" — popped
    # from the heap but not actually used. The slot would be unavailable
    # until the server restarts.
    try:
        #save to db
        session.add(slot)
        session.add(new_ticket)
        session.commit()
        session.refresh(new_ticket)  # WHY refresh? Gets the auto-generated ticketID from DB
    except Exception as e:
        session.rollback()  # Undo any partial writes to the database
        # Push the slot back — we took it from the heap but couldn't use it
        heap_manager.push_slot(slot.slot_type, slot.slotID, slot.distance_from_entrance)
        raise HTTPException(
            status_code=409,
            detail=f"Could not park vehicle. This license plate may already be parked. "
                   f"Error: {str(e)}"
        )

    #return json ticket object
    # CHANGED: Return a manually constructed dict instead of the raw model object.
    # WHY? The raw model includes the SQLAlchemy relationship field (slot),
    # which can cause serialization issues. A manual dict gives us full
    # control over what the API response looks like.
    return{
        "message": "Vehicle parked successfully",
        "ticket": {
            "ticketID": new_ticket.ticketID,
            "license_plate": new_ticket.license_plate,
            "vehicle_type": new_ticket.vehicle_type.value,
            "slotID": new_ticket.slotID,
            "check_in": str(new_ticket.check_in),
            "cost_per_hr": new_ticket.cost_per_hr,
        }
    }


# ──────────────────────────────────────────────────────────────
# ENDPOINT: POST /api/parking/exit
# ──────────────────────────────────────────────────────────────
@router.post("/exit")
def exit_vehicle(request: ExitRequest, session: SessionDep):
    """
    Process a vehicle's exit from the parking lot.

    Flow:
    1. Normalize the plate and find the active (open) ticket
    2. Calculate parking duration in hours (rounded up)
    3. Calculate base fee = hours × cost_per_hr
    4. Calculate overstay fine (if exceeded max allowed hours)
    5. Total fee = base fee + overstay fine
    6. Close the ticket and free the slot
    7. Check waitlist:
       - If someone is waiting → auto-assign this slot to them
       - If no one waiting → push slot back to the heap
    8. Commit everything to the database
    9. Return the fee breakdown

    WHAT CHANGED FROM ORIGINAL:
    - FIXED: "cost_per_hour" → "cost_per_hr" (matching model field name)
    - ADDED: plate normalization for consistent lookups
    - ADDED: overstay fine calculation
    - ADDED: waitlist auto-assign logic
    - ADDED: null check for slot (defensive programming)
    - ADDED: try/except for safe commit
    - ADDED: detailed fee breakdown in response
    """

    # ADDED: Normalize plate to match the cleaned format in the database
    plate = normalize_plate(request.license_plate)

    # 1. Find the active, open ticket for this license plate using the index
    statement = select(Ticket).where(
        Ticket.license_plate == plate,     # CHANGED: use normalized plate
        Ticket.is_closed == False
    )
    ticket = session.exec(statement).first()

    if not ticket:
        raise HTTPException(
            status_code=404, 
            detail="No active parking ticket found for this vehicle."
        )

    # 2. Calculate time and fee
    ticket.check_out = datetime.now(timezone.utc)
    
    # Calculate duration in seconds, convert to hours, and round up (ceiling)
    duration = ticket.check_out - ticket.check_in
    hours_parked = math.ceil(duration.total_seconds() / 3600.0)
    
    # Minimum 1 hour charge logic
    if hours_parked == 0:
        hours_parked = 1

    # FIXED: "cost_per_hour" → "cost_per_hr"
    # WHY did this break? The Ticket model defines the field as "cost_per_hr"
    # but the original code here used "cost_per_hour" — a typo that would
    # cause an AttributeError at runtime. The field name must EXACTLY match
    # what's defined in models.py.
    base_fee = hours_parked * ticket.cost_per_hr

    # ADDED: Calculate overstay fine using the fine_calculator service
    # WHY a separate function in a separate file?
    #   Keeps fee logic isolated and testable. If the business changes
    #   the fine rules (e.g., different rates for different vehicle types,
    #   or a grace period), we only edit fine_calculator.py — this router
    #   stays untouched. This is the Single Responsibility Principle.
    overstay_fine = calculate_overstay_fine(hours_parked)

    # Total fee = normal parking charge + any overstay penalty
    ticket.total_fee = base_fee + overstay_fine
    ticket.is_closed = True

    # 3. Update the slot status in the database
    slot = session.get(ParkingSlot, ticket.slotID)

    # ADDED: Null check for the slot — defensive programming
    # WHY? If the slot was somehow deleted from the DB while a ticket
    # was still active (data integrity issue), accessing slot.is_empty
    # would raise an AttributeError. Better to catch it with a clear error.
    if not slot:
        raise HTTPException(
            status_code=500,
            detail="Slot record not found in database. Data integrity issue."
        )
    slot.is_empty = True

    # ADDED: Check waitlist BEFORE pushing slot back to the heap
    # ─────────────────────────────────────────────────────────
    # WHY check waitlist here (not elsewhere)?
    #   This is the exact moment a slot becomes available. The fairest
    #   approach is: serve waiting vehicles FIRST, then return the slot
    #   to the general pool only if no one is waiting.
    #
    # FLOW:
    #   pop_next() returns the license plate of the first person waiting
    #   for this slot type. If someone is waiting:
    #     → Create a new ticket for them (auto-park)
    #     → Keep the slot occupied (don't push to heap)
    #   If no one is waiting:
    #     → Push slot back to heap for future use
    next_vehicle_plate = waitlist_manager.pop_next(slot.slot_type)
    waitlist_auto_assigned = None  # Track if we auto-assigned (for the response)

    if next_vehicle_plate:
        # Someone IS waiting! Auto-assign this slot to them immediately.
        slot.is_empty = False  # Slot stays occupied (new vehicle moving in)

        # Create a new ticket for the waitlisted vehicle
        # WHY a new Ticket? They're now "parked" — same as if they called /park
        waitlist_ticket = Ticket(
            license_plate=next_vehicle_plate,
            vehicle_type=slot.slot_type,
            slotID=slot.slotID,
            cost_per_hr=50.0,
        )
        session.add(waitlist_ticket)

        # Store info for the response so the exiting driver knows what happened
        waitlist_auto_assigned = {
            "license_plate": next_vehicle_plate,
            "assigned_slot": slot.slotID,
            "message": "Waitlisted vehicle was auto-assigned this freed slot.",
        }
    else:
        # 4. No one waiting → Push the slot back into the heap so it can be reused O(log N)
        # CHANGED: Now passing distance_from_entrance for proper min-heap ordering
        # (previously only passed slot_type and slot_id)
        heap_manager.push_slot(slot.slot_type, slot.slotID, slot.distance_from_entrance)

    # 5. Commit all changes
    # ADDED: try/except for safe database commit
    # WHY? If the commit fails (e.g., connection drop, constraint violation),
    # we rollback to prevent partial writes. Without rollback, the session
    # would be in a broken state for subsequent requests.
    try:
        session.add(ticket)
        session.add(slot)
        session.commit()
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Error processing vehicle exit: {str(e)}"
        )

    # CHANGED: Response now includes a detailed fee breakdown
    # WHY? The original only returned total_fee. Now we show base_fee
    # and overstay_fine separately so the user understands the charges.
    response = {
        "message": "Vehicle exited successfully",
        "duration_hours": hours_parked,
        "base_fee": base_fee,
        "overstay_fine": overstay_fine,
        "total_fee": ticket.total_fee,
        "freed_slot": slot.slotID,
    }

    # ADDED: Include waitlist auto-assignment info in response if it happened
    if waitlist_auto_assigned:
        response["waitlist_auto_assigned"] = waitlist_auto_assigned

    return response
