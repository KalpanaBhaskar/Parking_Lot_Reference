from datetime import datetime , timezone
from enum import Enum
from sqlmodel import Field, SQLModel, Relationship
#from typing import Literal 

# for custom validation
from pydantic import field_validator

#this is for conditional index
from sqlalchemy import Index 


#Enum forces the input to be 1 of the 3 options
class VehicleSlotType(str,Enum):
    Regular = "regular"
    Electric ="electric"
    Handicapped = "handicapped"

class ParkingSlot(SQLModel,table = True):
    __tablename__ = "Parking_Slots"
    slotID: int| None = Field(default=None,primary_key=True)
    #alternate : slot_type: Literal["regular", "electric", "handicapped"]
    slot_type: VehicleSlotType
    #indexing this column to check empty slots faster -like fetch from a hashmap is O(1)
    is_empty: bool = Field(default=True ,index=True)
    # ADDED: Distance from the parking lot entrance (in meters)
    # WHY? The problem says Handicapped slots should be "near entrances."
    # The min-heap uses this value as the SORT KEY — slots closer to the
    # entrance (smaller distance) get assigned first. This is especially
    # important for Handicapped vehicles but benefits all types.
    # WHY store in DB? So we can rebuild the heap on server restart.
    distance_from_entrance: int = Field(default=0)
    # tickets is a multi valued attribute and has Ticket objects
    #ticket in string coz its defined later after the class - string is placeholder
    tickets: list["Ticket"] = Relationship(back_populates="slot")

class Ticket(SQLModel,table = True):
    __tablename__ = "Tickets"
    ticketID: int|None = Field(primary_key=True, default=None)
    slotID: int|None = Field(foreign_key="Parking_Slots.slotID" , default = None)
    # validity check
    # CHANGED: min_length from 16 → 1, max_length from 16 → 20
    # WHY? Most real license plates are 6-10 characters.
    # Example: "KA01AB1234" is only 10 chars. Enforcing exactly 16
    # would reject almost every valid plate. A range of 1-20 is safe
    # enough to cover all formats without being overly restrictive.
    license_plate: str|None = Field(default=None, max_length=20, min_length=1)
    vehicle_type : VehicleSlotType
    # so that the start time is not the time when server started, when the ticket was created
    '''
    The main difference is how and when the default value is evaluated: 
    default is evaluated once when the code is first loaded, while 
    default_factory is executed as a function every single time a new 
    object is created.'''
    check_in : datetime|None = Field(default_factory= lambda: datetime.now(timezone.utc))
    check_out: datetime|None = Field(default = None)
    is_closed: bool|None = Field(default=False,index=True)
    
    cost_per_hr: int =Field(default = 50.0)
    # ADDED: Stores the final calculated fee when the vehicle exits
    # WHY nullable (None by default)?
    #   When the ticket is first created (vehicle enters), we don't know
    #   the fee yet. It's calculated only when the vehicle exits, based on
    #   how long they parked. So it starts as None and gets filled on exit.
    # WHY float?
    #   The fee can include fractions (e.g., overstay penalties).
    #   Using float avoids integer truncation in calculations.
    total_fee: float | None = Field(default=None)
    #tickets in parking slot and slot in tickets are related
    slot: ParkingSlot = Relationship(back_populates="tickets")

    #checking if open ticket licenses are unique
    __table_args__ = (
        Index("unique_open_ticket_license_plate", #index name
              "license_plate",  #target column
              unique = True,   # rule
              postgresql_where="is_closed = false" # condition filter
            ),  # ADDED: trailing comma makes this a proper Python tuple
                # WHY? In Python, (x) is just parentheses (grouping).
                # (x,) is a one-element tuple. SQLAlchemy's __table_args__
                # expects a TUPLE, not a bare value. Without this comma,
                # Python sees: __table_args__ = Index(...)  (just the Index object)
                # With the comma: __table_args__ = (Index(...),)  (a tuple containing the Index)
    )

    @field_validator("license_plate")  # pydantic decorator
    @classmethod  # pydantic needs this because it does class level validation
    def validate_and_clean_plate(cls, value:str)-> str:
        # clean_value = value.strip().upper()
        clean_value = value.replace(" ","").upper()
        if not clean_value.isalnum():
            raise ValueError("License plate must contain only letters and numbers")
        return clean_value  #the new value is returned to the column

"""
# While you don't have to write JOIN statements yourself, SQLModel is
# "lazy" by default. This means it won't actually look at the Tickets
# table until the exact moment you type slot.tickets in your code. 
# This triggers a second, separate database query.If you are loading a
# list of 100 slots and want to see their tickets without hitting the 
# database 100 extra times, you can tell SQLModel to do an explicit 
# join right away during your initial query using joinedload


from datetime import datetime, timezone
from sqlalchemy.orm import joinedload
from sqlmodel import Session, select


def checkout_vehicle(session: Session, slot_id: int):
    # 1. WHERE IT GOES: Inside the select options before executing the statement
    statement = (
        select(ParkingSlot)
        .where(ParkingSlot.slotID == slot_id)
        .options(joinedload(ParkingSlot.tickets))  # Force a SQL JOIN immediately
    )

    # Execute the query
    slot = session.exec(statement).first()

    # 2. Safety Check: Does the slot exist?
    if not slot:
        print(f"Error: Parking slot {slot_id} not found.")
        return

    # 3. Find the active open ticket using our relationship list
    active_ticket = None
    for ticket in slot.tickets:
        if not ticket.is_closed:
            active_ticket = ticket
            break

    # 4. Process the checkout if an active ticket is found
    if active_ticket:
        # Update ticket details
        active_ticket.check_out = datetime.now(timezone.utc)
        active_ticket.is_closed = True

        # Update slot availability status
        slot.is_empty = True

        # Save both changes to the database at once
        session.add(active_ticket)
        session.add(slot)
        session.commit()

        print(
            f"Successfully checked out vehicle {active_ticket.license_plate} from Slot {slot_id}."
        )
    else:
        print(f"No active vehicle found parked in Slot {slot_id}.")

"""


# ──────────────────────────────────────────────────────────────
# ADDED: Reservation Model (Bonus Feature)
# ──────────────────────────────────────────────────────────────
# WHY a separate table (not just a flag on ParkingSlot)?
#   Because a reservation has its own lifecycle and data:
#   - WHO reserved it (license_plate)
#   - WHEN it was created and WHEN it expires
#   - WHETHER it's still active
#   Mixing all of this into ParkingSlot would violate the Single
#   Responsibility Principle — ParkingSlot should only know about
#   slot-related data, not reservation workflows.
#
# HOW RESERVATIONS WORK:
#   1. Vehicle calls POST /api/reservations/create
#   2. A slot is popped from the heap and "held" (is_empty = False)
#   3. A Reservation record is created with an expires_at timestamp
#   4. When the vehicle arrives, POST /api/parking/park checks for
#      an active reservation and uses the reserved slot
#   5. If expires_at passes without the vehicle arriving, lazy cleanup
#      marks the reservation inactive and releases the slot
class Reservation(SQLModel, table=True):
    __tablename__ = "Reservations"

    # Auto-incrementing primary key — each reservation gets a unique ID
    reservationID: int | None = Field(primary_key=True, default=None)

    # WHO reserved? The vehicle's license plate (cleaned/normalized)
    license_plate: str = Field(max_length=20, min_length=1)

    # WHAT type of slot did they request?
    vehicle_type: VehicleSlotType

    # WHICH specific slot is being held for them?
    # WHY a foreign key? Links this reservation to a real slot in the
    # Parking_Slots table. The database enforces that the slotID exists.
    slotID: int = Field(foreign_key="Parking_Slots.slotID")

    # WHEN was the reservation created?
    # WHY default_factory with lambda?
    #   Same pattern as Ticket.check_in — ensures each reservation
    #   records the EXACT time it was created, not when the server started.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # WHEN does this reservation expire?
    # This is set at creation time: created_at + RESERVATION_HOLD_MINUTES
    # WHY no default? Because the expiry depends on business logic
    # (the hold duration constant), which is set in the reservations router.
    # The router calculates: expires_at = now + timedelta(minutes=30)
    expires_at: datetime

    # Is this reservation still active (not expired, not used, not cancelled)?
    # WHY indexed? We frequently query "WHERE is_active = true" to find
    # active reservations. An index makes this query fast — O(log N).
    is_active: bool = Field(default=True, index=True)