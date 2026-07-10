"""
waitlist.py — Waitlist API Endpoints
---------------------------------------
WHY THIS FILE EXISTS:
    The problem statement lists "Waitlist for vehicles" as a bonus feature.
    When the parking lot is full for a given vehicle type, instead of
    being turned away, vehicles can join a FIFO queue (waitlist).
    When a slot frees up (in the exit endpoint in services.py),
    the first vehicle in the waitlist is auto-assigned the freed slot.

ENDPOINTS IN THIS FILE:
    POST   /api/waitlist/join                  → Join the waitlist
    GET    /api/waitlist/status/{license_plate} → Check your queue position
    DELETE /api/waitlist/leave/{license_plate}  → Leave the waitlist voluntarily

WHY A SEPARATE ROUTER FILE?
    Code splitting / modularity. Each feature (parking, waitlist,
    reservations) gets its own file. This keeps each file short,
    focused, and easy to navigate. It also follows the Single
    Responsibility Principle — each module handles ONE concern.

HOW THE AUTO-ASSIGN WORKS (important!):
    The auto-assign does NOT happen in this file. It happens in
    services.py → exit_vehicle(). When a vehicle exits and frees a slot:
      1. The exit route checks if anyone is waiting (waitlist_manager.pop_next)
      2. If YES → the freed slot goes directly to the waitlisted vehicle
      3. If NO → the freed slot goes back to the heap for general use
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# WHY import VehicleSlotType?
#   The waitlist request needs to know WHICH type of slot the vehicle needs.
#   We reuse the same enum from models.py for consistency.
from app.database.models import VehicleSlotType

# WHY import waitlist_manager (the global instance)?
#   All waitlist data lives in this single in-memory object.
#   Every endpoint reads from / writes to the same instance.
from app.services.waitlist_manager import waitlist_manager


# ──────────────────────────────────────────────────────────────
# ROUTER SETUP
# ──────────────────────────────────────────────────────────────
# WHY prefix="/api/waitlist"?
#   Keeps waitlist URLs separate from parking URLs.
#   Endpoints become: /api/waitlist/join, /api/waitlist/status/..., etc.
# WHY tags=["Waitlist"]?
#   Groups these endpoints under a "Waitlist" section in the Swagger
#   docs page (/docs), making the API easier to explore.
router = APIRouter(prefix="/api/waitlist", tags=["Waitlist"])


# ──────────────────────────────────────────────────────────────
# REQUEST SCHEMA
# ──────────────────────────────────────────────────────────────
# WHY a Pydantic model for the request body?
#   FastAPI automatically validates the JSON body against this schema.
#   If license_plate is missing or vehicle_type isn't a valid enum value,
#   FastAPI returns a 422 error with details — we don't have to write
#   that validation code ourselves.
class WaitlistRequest(BaseModel):
    license_plate: str             # The vehicle's license plate
    vehicle_type: VehicleSlotType  # Which type of slot they need


# ──────────────────────────────────────────────────────────────
# HELPER: License Plate Normalization
# ──────────────────────────────────────────────────────────────
# WHY normalize here too?
#   The plate must be in the same format everywhere (DB, heap, waitlist).
#   The model's @field_validator cleans it for DB writes, but the waitlist
#   is in-memory — we need to clean it ourselves before adding.
def normalize_plate(plate: str) -> str:
    """Remove spaces and convert to uppercase to match DB storage format."""
    return plate.replace(" ", "").upper()


# ──────────────────────────────────────────────────────────────
# ENDPOINT: POST /api/waitlist/join
# ──────────────────────────────────────────────────────────────
# WHY POST?
#   POST = "create a new resource." Joining a waitlist creates a new
#   waitlist entry. GET would be wrong because this changes state.
@router.post("/join")
def join_waitlist(request: WaitlistRequest):
    """
    Add a vehicle to the waitlist queue for its slot type.

    WHY no database session parameter?
        The waitlist is 100% in-memory (waitlist_manager). No database
        reads or writes happen here. This makes the endpoint very fast
        — O(1) time complexity for the append operation.

    EDGE CASES HANDLED:
        - Vehicle already on a waitlist → 409 Conflict
        - Empty plate after normalization → caught by validate step
    """
    # Step 1: Normalize the plate to match the format used everywhere else
    plate = normalize_plate(request.license_plate)

    # Step 2: Validate that the normalized plate is not empty
    # WHY? If someone sends license_plate: "   " (all spaces), normalize
    # turns it into "" (empty string). We can't waitlist an empty plate.
    if not plate:
        raise HTTPException(
            status_code=400,  # 400 Bad Request = client sent invalid data
            detail="License plate cannot be empty."
        )

    # Step 3: Check if this vehicle is already in ANY waitlist queue
    # WHY? A vehicle shouldn't be in multiple waitlists simultaneously.
    # If "KA01AB1234" is already waiting for Regular, they shouldn't
    # also be able to join the Electric waitlist — that's unfair.
    if waitlist_manager.is_already_waiting(plate):
        raise HTTPException(
            status_code=409,  # 409 Conflict = state conflict with existing data
            detail="This vehicle is already on a waitlist."
        )

    # Step 4: Add to the waitlist and get the queue position back
    position = waitlist_manager.add_to_waitlist(request.vehicle_type, plate)

    # Return a helpful response with the queue position
    return {
        "message": f"Vehicle added to {request.vehicle_type.value} waitlist.",
        "license_plate": plate,
        "position_in_queue": position,
        "note": "You will be auto-assigned a slot when one becomes available."
    }


# ──────────────────────────────────────────────────────────────
# ENDPOINT: GET /api/waitlist/status/{license_plate}
# ──────────────────────────────────────────────────────────────
# WHY a path parameter ({license_plate}) instead of a request body?
#   GET requests typically should NOT have a body. Many HTTP clients
#   and proxies strip the body from GET requests. The standard REST
#   convention is to identify the resource in the URL path.
#   Example: GET /api/waitlist/status/KA01AB1234
@router.get("/status/{license_plate}")
def check_waitlist_status(license_plate: str):
    """
    Check the current position of a vehicle in the waitlist.

    Returns the vehicle's position, the queue it's in, and the
    total number of vehicles waiting in that queue for context.

    WHY iterate over all VehicleSlotTypes?
        We don't know which queue the vehicle joined (the URL only
        has the plate, not the type). So we search all three queues.
        Since there are only 3 types, this is fast — O(3 × N) worst case.
    """
    plate = normalize_plate(license_plate)

    # Search each vehicle type's queue to find where this plate is
    for vehicle_type in VehicleSlotType:
        position = waitlist_manager.get_position(vehicle_type, plate)
        if position is not None:
            # Found! Return the position and queue details
            return {
                "license_plate": plate,
                "vehicle_type": vehicle_type.value,
                "position_in_queue": position,
                "total_waiting": waitlist_manager.get_waitlist_count(vehicle_type),
            }

    # If we checked all queues and found nothing, the vehicle isn't waiting
    raise HTTPException(
        status_code=404,  # 404 Not Found = the requested resource doesn't exist
        detail="This vehicle is not on any waitlist.",
    )


# ──────────────────────────────────────────────────────────────
# ENDPOINT: DELETE /api/waitlist/leave/{license_plate}
# ──────────────────────────────────────────────────────────────
# WHY DELETE method?
#   REST convention: DELETE = remove a resource. The vehicle is
#   "deleting" their waitlist entry. This maps naturally:
#   POST /join = create entry, DELETE /leave = remove entry.
@router.delete("/leave/{license_plate}")
def leave_waitlist(license_plate: str):
    """
    Remove a vehicle from the waitlist (they decided not to wait anymore).

    WHY iterate over all types?
        Same reason as status — we don't know which queue they joined.
        We try removing from each queue; .remove_from_waitlist() returns
        True if it found and removed the plate, False if not found.
    """
    plate = normalize_plate(license_plate)

    # Try to remove from each vehicle type's queue
    for vehicle_type in VehicleSlotType:
        if waitlist_manager.remove_from_waitlist(vehicle_type, plate):
            # Successfully removed from this queue
            return {
                "message": f"Vehicle removed from {vehicle_type.value} waitlist.",
                "license_plate": plate,
            }

    # If we get here, the vehicle wasn't in any queue
    raise HTTPException(
        status_code=404,
        detail="This vehicle is not on any waitlist.",
    )
