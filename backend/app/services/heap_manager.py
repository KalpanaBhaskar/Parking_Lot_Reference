"""
heap_manager.py — In-Memory Min-Heap for Slot Assignment
---------------------------------------------------------
WHY THIS FILE EXISTS:
    We need to quickly find the BEST (closest to entrance) available
    parking slot for a given vehicle type. A min-heap gives us O(log N)
    push and O(log N) pop — much faster than scanning the database
    every time a vehicle arrives.

DATA STRUCTURE: Min-Heap (Priority Queue)
    Python's heapq module implements a MIN-heap using a regular list.
    The smallest element is always at index 0 (the "top").

    We store tuples: (distance_from_entrance, slot_id)
    ┌───────────────────────────────────────────────────────────────┐
    │  WHY tuples?                                                  │
    │  Python compares tuples element-by-element:                   │
    │    (5, 101) < (10, 102)  → True  (5 < 10, so first wins)     │
    │    (5, 101) < (5, 200)   → True  (5 == 5, so compare IDs)    │
    │                                                               │
    │  This means the heap automatically prioritizes:               │
    │    1. Slots closer to the entrance (smaller distance)         │
    │    2. If tied, the slot with the smaller ID (arbitrary tiebreak)│
    └───────────────────────────────────────────────────────────────┘

    CHANGED from original: Previously pushed raw slot_id integers.
    Now pushes (distance, slot_id) tuples so the heap sorts by
    proximity to entrance — especially important for Handicapped slots
    which should be "near entrances" per the problem statement.

WHY THREE SEPARATE HEAPS?
    Each vehicle type has its own pool of compatible slots:
    - Regular vehicles → Regular slots only
    - Electric vehicles → Electric slots only (with charging stations)
    - Handicapped vehicles → Handicapped slots only (near entrances)
    Separate heaps prevent cross-type assignment.

LIFECYCLE:
    1. Server starts → main.py reads empty slots from DB → pushes each
       into the correct heap using push_slot()
    2. Vehicle parks → pop_slot() removes the best slot from the heap
    3. Vehicle exits → push_slot() returns the slot to the heap
    4. Reservation created → pop_slot() removes slot (held for reservation)
    5. Reservation expires/cancels → push_slot() returns slot to heap
"""

import heapq
# heap based on types
from app.database.models import VehicleSlotType


class ParkingHeapManager:
    def __init__(self):
        #initialise 3 separate min-heaps
        #dict with 3 key-value pairs
        # CHANGED: Each heap now stores (distance, slot_id) tuples
        # instead of raw slot_ids, so the heap sorts by distance
        self.heaps: dict[VehicleSlotType, list[tuple[int, int]]] = {
            VehicleSlotType.Regular: [],
            VehicleSlotType.Electric: [],
            VehicleSlotType.Handicapped: []
        }

    # CHANGED: Added distance_from_entrance parameter
    # WHY? The heap needs the distance to sort correctly.
    # Previously sorted by slot_id (arbitrary). Now sorts by distance
    # (meaningful — closer slots are assigned first).
    #
    # Time Complexity: O(log N) — heapq.heappush maintains heap property
    def push_slot(
        self,
        slot_type: VehicleSlotType,
        slot_id: int,
        distance_from_entrance: int
    ):
        """
        Add a slot back to its type's min-heap.

        Called when:
        - Server starts (loading empty slots from DB)
        - A vehicle exits (slot becomes available again)
        - A reservation expires or is cancelled

        Parameters
        ----------
        slot_type : VehicleSlotType
            Which heap to push into (Regular/Electric/Handicapped).
        slot_id : int
            The unique ID of the parking slot.
        distance_from_entrance : int
            How far the slot is from the entrance (in meters).
            The heap sorts by this value — smaller = higher priority.
        """
        # Push a tuple: (distance, slot_id)
        # heapq sorts by the FIRST element of the tuple (distance)
        # If distances are equal, it falls back to the SECOND element (slot_id)
        heapq.heappush(self.heaps[slot_type], (distance_from_entrance, slot_id))

    # Time Complexity: O(log N) — heapq.heappop re-heapifies after removal
    def pop_slot(self, slot_type: VehicleSlotType) -> int | None:
        """
        Remove and return the BEST available slot for this vehicle type.

        "Best" = closest to entrance (smallest distance value).

        Returns
        -------
        int or None
            The slot_id of the best slot, or None if no slots available.
            We return ONLY the slot_id (not the distance tuple) because
            the caller (the router) only needs the ID to look up the
            slot in the database.
        """
        if not self.heaps[slot_type]:
            return None
        # heappop returns the SMALLEST tuple: (smallest_distance, slot_id)
        # We unpack it and return only the slot_id
        _distance, slot_id = heapq.heappop(self.heaps[slot_type])
        return slot_id

    # ADDED: Method to check how many slots are available per type
    # WHY? The GET /api/parking/availability endpoint needs this data.
    # By reading directly from the heap, we avoid a database query.
    # Time Complexity: O(1) — len() on a list is constant time.
    def get_availability(self) -> dict[str, int]:
        """
        Return the count of available (empty) slots for each vehicle type.

        Returns a dict like: {"regular": 3, "electric": 2, "handicapped": 1}

        WHY .value?
            VehicleSlotType.Regular is an Enum object, not a string.
            .value gives us "regular" (the string), which is JSON-serializable
            and more readable in API responses.
        """
        return {
            slot_type.value: len(heap)
            for slot_type, heap in self.heaps.items()
        }


# Create a global instance that will live in memory while the server runs   
heap_manager = ParkingHeapManager()