"""
reservations.py — Reservation API Endpoints
----------------------------------------------
WHY THIS FILE EXISTS:
    The problem statement lists "Reservations" as a bonus feature.
    A vehicle can reserve a parking slot in advance. The slot is held
    for a limited time (default: 30 minutes). If the vehicle arrives
    and parks within that window, they get their reserved slot. If they
    don't arrive in time, the reservation expires and the slot is
    released back to the available pool.

ENDPOINTS IN THIS FILE:
    POST   /api/reservations/create                → Reserve a slot
    GET    /api/reservations/status/{license_plate} → Check reservation details
    DELETE /api/reservations/cancel/{license_plate} → Cancel and release slot

EXPIRY STRATEGY — "Lazy Expiration":
    Instead of running a background timer/thread to check for expired
    reservations every N seconds, we check for expiry WHENEVER a relevant
    action happens:
      - When creating a new reservation (so freed slots become available)
      - When checking reservation status (so expired ones don't show up)

    WHY lazy instead of a background job?
      1. Simpler code — no threading, no race conditions, no cron
      2. Perfectly adequate for a coding assignment
      3. In production, you'd use Celery/APScheduler/cron for this

HOW RESERVATIONS INTERACT WITH PARKING (services.py):
    When a vehicle calls POST /api/parking/park, the park_vehicle()
    function checks: "Does this vehicle have an active, non-expired
    reservation?" If YES → use the reserved slot. If NO → pop from heap.
    This check lives in services.py, NOT here — each router stays focused
    on its own feature.
"""

from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app.database.models import Reservation, ParkingSlot, VehicleSlotType
from app.database.session import SessionDep
from app.services.heap_manager import heap_manager


# ──────────────────────────────────────────────────────────────
# CONFIGURABLE CONSTANTS
# ──────────────────────────────────────────────────────────────
# WHY a named constant instead of a magic number?
#   If you see `timedelta(minutes=30)` buried in code, you'd have to
#   search for it to change it. A named constant at the top of the file
#   makes the business rule visible and easy to update.
RESERVATION_HOLD_MINUTES = 30  # How long a reservation holds a slot


# ──────────────────────────────────────────────────────────────
# ROUTER SETUP
# ──────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/reservations", tags=["Reservations"])


# ──────────────────────────────────────────────────────────────
# REQUEST SCHEMA
# ──────────────────────────────────────────────────────────────
class ReservationRequest(BaseModel):
    license_plate: str             # Vehicle's license plate
    vehicle_type: VehicleSlotType  # Type of slot to reserve


# ──────────────────────────────────────────────────────────────
# HELPER: License Plate Normalization
# ──────────────────────────────────────────────────────────────
def normalize_plate(plate: str) -> str:
    """Remove spaces and convert to uppercase to match DB storage format."""
    return plate.replace(" ", "").upper()


# ──────────────────────────────────────────────────────────────
# HELPER: Lazy Expiration Cleanup
# ──────────────────────────────────────────────────────────────
def cleanup_expired_reservations(session) -> int:
    """
    Find all reservations that have passed their expiry time and
    release their held slots back to the available pool.

    HOW IT WORKS:
        1. Query: SELECT * FROM Reservations WHERE is_active=true AND expires_at < now
        2. For each expired reservation:
           a. Mark reservation as inactive (is_active = False)
           b. Mark the slot as empty (is_empty = True)
           c. Push the slot back to the heap (so it can be assigned again)
        3. Commit all changes in one batch

    WHY "lazy" cleanup?
        We don't run a background timer. Instead, we call this function
        BEFORE any operation that cares about slot availability. This
        ensures expired reservations are cleaned up "just in time."

    WHEN is this called?
        - Before creating a new reservation (frees up expired slots)
        - Before checking reservation status (removes stale data)

    Parameters
    ----------
    session : SQLModel Session
        The database session to use for queries and commits.

    Returns
    -------
    int
        Number of expired reservations that were cleaned up.
        Useful for logging / debugging.
    """
    now = datetime.now(timezone.utc)

    # Find all active reservations whose expiry time is in the past
    statement = select(Reservation).where(
        Reservation.is_active == True,    # Only look at active ones
        Reservation.expires_at < now      # Expiry time has passed
    )
    expired_reservations = session.exec(statement).all()

    # Process each expired reservation
    for res in expired_reservations:
        # Step A: Deactivate the reservation record
        res.is_active = False
        session.add(res)

        # Step B: Release the slot that was being held
        slot = session.get(ParkingSlot, res.slotID)
        if slot:
            slot.is_empty = True  # Slot is now available again
            session.add(slot)
            # Step C: Push slot back to the heap for general assignment
            # WHY pass distance_from_entrance?
            #   The heap sorts by distance — closer slots are assigned first.
            #   We need the distance value to maintain correct heap ordering.
            heap_manager.push_slot(
                slot.slot_type, slot.slotID, slot.distance_from_entrance
            )

    # Commit all cleanup changes in one batch (more efficient than per-item)
    if expired_reservations:
        session.commit()

    return len(expired_reservations)


# ──────────────────────────────────────────────────────────────
# ENDPOINT: POST /api/reservations/create
# ──────────────────────────────────────────────────────────────
@router.post("/create")
def create_reservation(request: ReservationRequest, session: SessionDep):
    """
    Reserve a parking slot for a vehicle. The slot is held for
    RESERVATION_HOLD_MINUTES (default: 30 minutes).

    The vehicle must arrive and call POST /api/parking/park within
    that time window. If they don't, the reservation expires and
    the slot is released back to the general pool.

    Flow:
    1. Clean up any expired reservations (frees their slots)
    2. Validate: is the plate non-empty? does the vehicle already have one?
    3. Pop the nearest available slot from the heap
    4. Create a Reservation record with an expiry timestamp
    5. Save to database

    WHY pop from the heap?
        When we reserve a slot, we REMOVE it from the available pool.
        Other vehicles shouldn't be able to get this slot while it's
        reserved. If the reservation expires, cleanup pushes it back.
    """
    plate = normalize_plate(request.license_plate)

    # Validate plate is not empty after normalization
    if not plate:
        raise HTTPException(status_code=400, detail="License plate cannot be empty.")

    # Step 1: Clean up expired reservations to free their slots
    # WHY do this first? Some slots might have been "held" by reservations
    # that expired. By cleaning up first, those slots become available
    # for THIS reservation.
    cleanup_expired_reservations(session)

    # Step 2: Check if vehicle already has an active reservation
    # WHY? One vehicle = one reservation at a time. Prevents slot hoarding.
    existing = session.exec(
        select(Reservation).where(
            Reservation.license_plate == plate,
            Reservation.is_active == True,
        )
    ).first()

    if existing:
        raise HTTPException(
            status_code=409,  # 409 Conflict
            detail="This vehicle already has an active reservation.",
        )

    # Step 3: Pop the nearest available slot from the heap
    # WHY pop (not just peek)? We need to REMOVE it from the available
    # pool so no one else can grab it while it's reserved.
    slot_id = heap_manager.pop_slot(request.vehicle_type)

    if not slot_id:
        raise HTTPException(
            status_code=409,
            detail=f"No available slots to reserve for {request.vehicle_type.value} vehicles.",
        )

    # Fetch the slot from DB and mark as "held" (not available)
    slot = session.get(ParkingSlot, slot_id)
    if not slot:
        raise HTTPException(status_code=500, detail="Heap-database sync error.")

    # Mark slot as not empty (it's now "held" by the reservation)
    slot.is_empty = False

    # Step 4: Create the Reservation record
    now = datetime.now(timezone.utc)
    reservation = Reservation(
        license_plate=plate,
        vehicle_type=request.vehicle_type,
        slotID=slot_id,
        created_at=now,
        # expires_at = current time + hold duration
        # WHY timedelta? It creates a datetime that's N minutes in the future.
        expires_at=now + timedelta(minutes=RESERVATION_HOLD_MINUTES),
    )

    # Step 5: Save everything to the database
    try:
        session.add(slot)
        session.add(reservation)
        session.commit()
        session.refresh(reservation)  # Get the auto-generated reservationID
    except Exception as e:
        session.rollback()
        # Push slot back to heap since the reservation failed
        heap_manager.push_slot(
            slot.slot_type, slot.slotID, slot.distance_from_entrance
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create reservation: {str(e)}",
        )

    return {
        "message": "Reservation created successfully.",
        "reservation": {
            "reservationID": reservation.reservationID,
            "license_plate": reservation.license_plate,
            "vehicle_type": reservation.vehicle_type.value,
            "slotID": reservation.slotID,
            "created_at": str(reservation.created_at),
            "expires_at": str(reservation.expires_at),
        },
        "expires_in_minutes": RESERVATION_HOLD_MINUTES,
        "note": f"Arrive and park within {RESERVATION_HOLD_MINUTES} minutes "
                f"or the reservation will expire.",
    }


# ──────────────────────────────────────────────────────────────
# ENDPOINT: GET /api/reservations/status/{license_plate}
# ──────────────────────────────────────────────────────────────
@router.get("/status/{license_plate}")
def get_reservation_status(license_plate: str, session: SessionDep):
    """
    Check if a vehicle has an active reservation and view its details.

    Also triggers lazy expiration cleanup, so if the reservation just
    expired, the response will correctly say "not found" instead of
    showing stale data.
    """
    plate = normalize_plate(license_plate)

    # Clean up expired reservations before checking
    # WHY? If this vehicle's reservation just expired 1 second ago,
    # cleanup marks it as inactive. Without this, we'd return a
    # reservation that's technically expired — misleading for the user.
    cleanup_expired_reservations(session)

    # Query for an active reservation for this plate
    reservation = session.exec(
        select(Reservation).where(
            Reservation.license_plate == plate,
            Reservation.is_active == True,
        )
    ).first()

    if not reservation:
        raise HTTPException(
            status_code=404,
            detail="No active reservation found for this vehicle.",
        )

    # Calculate remaining time before expiry
    now = datetime.now(timezone.utc)
    remaining = reservation.expires_at - now
    # WHY max(0, ...)? If remaining is negative (shouldn't happen after
    # cleanup, but just in case), we clamp to 0 instead of showing
    # negative minutes — that would confuse the user.
    remaining_minutes = max(0, remaining.total_seconds() / 60)

    return {
        "reservation": {
            "reservationID": reservation.reservationID,
            "license_plate": reservation.license_plate,
            "vehicle_type": reservation.vehicle_type.value,
            "slotID": reservation.slotID,
            "created_at": str(reservation.created_at),
            "expires_at": str(reservation.expires_at),
        },
        "remaining_minutes": round(remaining_minutes, 1),
    }


# ──────────────────────────────────────────────────────────────
# ENDPOINT: DELETE /api/reservations/cancel/{license_plate}
# ──────────────────────────────────────────────────────────────
@router.delete("/cancel/{license_plate}")
def cancel_reservation(license_plate: str, session: SessionDep):
    """
    Cancel an active reservation and release the held slot.

    Flow:
    1. Find the active reservation for this license plate
    2. Mark the reservation as inactive
    3. Release the slot (mark as empty, push back to the heap)
    4. Commit to the database

    WHY release the slot?
        When the reservation was created, we popped a slot from the
        heap and marked it as not-empty. Cancelling undoes both of
        those actions — the slot becomes available again.
    """
    plate = normalize_plate(license_plate)

    # Find the active reservation
    reservation = session.exec(
        select(Reservation).where(
            Reservation.license_plate == plate,
            Reservation.is_active == True,
        )
    ).first()

    if not reservation:
        raise HTTPException(
            status_code=404,
            detail="No active reservation found for this vehicle.",
        )

    # Step 1: Deactivate the reservation
    reservation.is_active = False
    session.add(reservation)

    # Step 2: Release the slot back to the available pool
    slot = session.get(ParkingSlot, reservation.slotID)
    if slot:
        slot.is_empty = True
        session.add(slot)
        # Push slot back to heap so other vehicles can use it
        heap_manager.push_slot(
            slot.slot_type, slot.slotID, slot.distance_from_entrance
        )

    # Step 3: Commit all changes
    try:
        session.commit()
    except Exception as e:
        session.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cancel reservation: {str(e)}",
        )

    return {
        "message": "Reservation cancelled successfully.",
        "license_plate": plate,
        "freed_slot": reservation.slotID,
    }
