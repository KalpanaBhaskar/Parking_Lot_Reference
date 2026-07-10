"""
waitlist_manager.py — In-Memory Waitlist Queue
------------------------------------------------
WHY THIS FILE EXISTS:
    The problem statement says: "Waitlist for vehicles."
    When the parking lot is full for a given vehicle type, instead of
    flat-out rejecting the vehicle, we add it to a FIFO queue (waitlist).
    When a slot frees up (on vehicle exit), the first vehicle in the
    waitlist for that type gets auto-assigned the freed slot.

WHY IN-MEMORY (not stored in the database)?
    Same reason as the heap_manager — speed. Queue operations (enqueue,
    dequeue, peek) are O(1) with a deque. For a coding assignment,
    in-memory is perfectly acceptable. In production, you'd use Redis
    or a database-backed queue for persistence across server restarts.

DATA STRUCTURE CHOICE — collections.deque:
    We use Python's built-in `deque` (double-ended queue) because:
    ┌───────────────────────────────────────────────────────────┐
    │  Operation         │  deque    │  list (regular)          │
    │  append (enqueue)  │  O(1)    │  O(1)                     │
    │  popleft (dequeue) │  O(1)    │  O(N) ← shifts elements   │
    │  remove (by value) │  O(N)    │  O(N)                     │
    └───────────────────────────────────────────────────────────┘
    The key advantage: popleft() is O(1) vs list.pop(0) which is O(N).
    For a waitlist where we always serve the first person, this matters.
"""

from collections import deque  # Python built-in, no pip install needed
from app.database.models import VehicleSlotType


class WaitlistManager:
    """
    Manages a separate FIFO waitlist queue for each vehicle type.

    Internal structure (dict of deques):
        {
            "regular":     deque(["KA01AB1234", "MH02CD5678", ...]),
            "electric":    deque(["DL03EF9012", ...]),
            "handicapped": deque([])
        }

    Each entry stores the license plate (string) of the waiting vehicle.
    The first vehicle added is the first to be served (FIFO).
    """

    def __init__(self):
        """
        Initialize one empty deque per vehicle type.

        WHY a dict of deques?
            Each vehicle type has its own independent waitlist.
            A Regular vehicle waiting has nothing to do with Electric slots
            becoming available. Keeping them separate prevents cross-type
            confusion and makes lookups faster.
        """
        self._queues: dict[VehicleSlotType, deque] = {
            VehicleSlotType.Regular: deque(),
            VehicleSlotType.Electric: deque(),
            VehicleSlotType.Handicapped: deque(),
        }

    def add_to_waitlist(
        self, vehicle_type: VehicleSlotType, license_plate: str
    ) -> int:
        """
        Add a vehicle to the END of the waitlist for its type.

        WHY append to the end?
            FIFO = First In, First Out. The vehicle that arrives first
            should be served first. append() adds to the right (end)
            of the deque, so the earliest arrival is always at the left.

        Parameters
        ----------
        vehicle_type : VehicleSlotType
            The type of slot the vehicle needs (regular/electric/handicapped).
        license_plate : str
            The vehicle's cleaned, normalized license plate.

        Returns
        -------
        int
            The vehicle's position in the queue (1-indexed).
            Why 1-indexed? "You are #1 in line" is more natural than
            "You are #0 in line" for user-facing responses.
        """
        self._queues[vehicle_type].append(license_plate)
        return len(self._queues[vehicle_type])  # 1-indexed position

    def pop_next(self, vehicle_type: VehicleSlotType) -> str | None:
        """
        Remove and return the FIRST vehicle waiting for this slot type.

        WHY popleft() instead of pop()?
            popleft() removes from the LEFT (front) of the deque = FIFO.
            pop() removes from the RIGHT (back) = LIFO (stack behavior).
            We want the vehicle that waited the LONGEST to go first.

        Returns
        -------
        str or None
            The license plate of the next vehicle in line,
            or None if no one is waiting for this slot type.
        """
        # Guard clause: if the queue is empty, return None
        if not self._queues[vehicle_type]:
            return None
        # Remove and return the first (leftmost) element
        return self._queues[vehicle_type].popleft()

    def remove_from_waitlist(
        self, vehicle_type: VehicleSlotType, license_plate: str
    ) -> bool:
        """
        Remove a specific vehicle from the waitlist (they decided to leave).

        WHY not just use popleft()?
            A vehicle might want to cancel waiting. They could be ANYWHERE
            in the queue, not necessarily at the front. deque.remove()
            finds and removes the first occurrence of the value.

        Parameters
        ----------
        vehicle_type : VehicleSlotType
            The type of waitlist to search in.
        license_plate : str
            The plate of the vehicle that wants to leave the waitlist.

        Returns
        -------
        bool
            True if the vehicle was found and removed.
            False if the vehicle wasn't in this waitlist.
        """
        try:
            self._queues[vehicle_type].remove(license_plate)
            return True
        except ValueError:
            # deque.remove() raises ValueError if the item isn't found
            # We catch it and return False instead of crashing
            return False

    def get_position(
        self, vehicle_type: VehicleSlotType, license_plate: str
    ) -> int | None:
        """
        Check a vehicle's current position in the waitlist.

        Returns
        -------
        int or None
            1-indexed position (e.g., 1 = you're next),
            or None if the vehicle isn't in this queue.
        """
        try:
            # Convert deque to list to use .index()
            # WHY convert? deque.index() exists in Python 3.5+, but
            # converting to list is universally safe and readable.
            index = list(self._queues[vehicle_type]).index(license_plate)
            return index + 1  # Convert 0-indexed → 1-indexed for humans
        except ValueError:
            return None

    def get_waitlist_count(self, vehicle_type: VehicleSlotType) -> int:
        """
        Return how many vehicles are currently waiting for this slot type.

        WHY is this useful?
            The waitlist status endpoint shows "you are #3 of 7 waiting"
            — the total count gives context to the position.
        """
        return len(self._queues[vehicle_type])

    def is_already_waiting(self, license_plate: str) -> bool:
        """
        Check if a license plate is already in ANY waitlist.

        WHY check ALL queues?
            A vehicle should only be in one waitlist at a time.
            If someone tries to join the Regular waitlist but is already
            in the Electric waitlist, we should reject the duplicate.
            This prevents a single vehicle from "reserving" spots in
            multiple queues simultaneously.

        Parameters
        ----------
        license_plate : str
            The normalized license plate to search for.

        Returns
        -------
        bool
            True if found in any queue, False otherwise.
        """
        for queue in self._queues.values():
            if license_plate in queue:  # O(N) scan per queue
                return True
        return False


# ──────────────────────────────────────────────────────────────
# GLOBAL SINGLETON INSTANCE
# ──────────────────────────────────────────────────────────────
# WHY a global instance?
#   Same pattern as heap_manager.py. One WaitlistManager instance lives
#   in server memory for the entire lifetime of the app. All API routes
#   share the same instance — they import this variable directly.
#
# WHY not create it inside each request?
#   Because the waitlist data would be lost after every request.
#   A global instance persists between requests (while the server runs).
waitlist_manager = WaitlistManager()
