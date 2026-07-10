"""
fine_calculator.py — Overstay Fine Calculation Logic
-----------------------------------------------------
WHY THIS FILE EXISTS:
    The problem statement says: "Fine for Exceeding Maximum Parking Time."
    We keep the fine logic in its own module so the router stays clean.
    This is a "pure function" — it takes inputs and returns a result,
    with NO database or HTTP dependencies. Easy to test, easy to reuse.

HOW IT WORKS:
    - A maximum parking duration is defined (default: 24 hours).
    - If a vehicle parks longer than the max, an overstay fine is charged
      ON TOP of the normal parking fee.
    - Fine formula: (hours_over_limit) × (penalty_rate_per_hour)

EXAMPLE:
    Vehicle parked for 26 hours:
    - Base fee: 26 × ₹50 = ₹1300 (handled in the router, not here)
    - Overstay: 26 - 24 = 2 extra hours
    - Fine: 2 × ₹100 = ₹200 (THIS is what this module calculates)
    - Total: ₹1300 + ₹200 = ₹1500
"""

# ──────────────────────────────────────────────────────────────
# CONFIGURABLE CONSTANTS
# ──────────────────────────────────────────────────────────────
# WHY constants at the top of the file?
#   Makes business rules easy to find and change without hunting
#   through code. In a production app, these would come from a
#   config file or environment variables.

MAX_PARKING_HOURS = 24              # Maximum allowed parking duration (hours)
OVERSTAY_PENALTY_PER_HOUR = 100.0   # Fine charged per extra hour (in ₹)


def calculate_overstay_fine(hours_parked: int) -> float:
    """
    Calculate the overstay fine for a vehicle.

    This is a PURE FUNCTION — no side effects, no database calls,
    no HTTP requests. Given the same input, it always returns the
    same output. This makes it very easy to unit test.

    Parameters
    ----------
    hours_parked : int
        Total hours the vehicle was parked (already ceiling-rounded
        by the router before calling this function).

    Returns
    -------
    float
        The fine amount in ₹. Returns 0.0 if the vehicle did NOT
        exceed the maximum parking time.

    Examples
    --------
    >>> calculate_overstay_fine(10)
    0.0     # 10 hours ≤ 24 limit → no fine

    >>> calculate_overstay_fine(24)
    0.0     # Exactly at limit → no fine (not "over" the limit)

    >>> calculate_overstay_fine(26)
    200.0   # 26 - 24 = 2 extra hours × ₹100/hr = ₹200

    >>> calculate_overstay_fine(48)
    2400.0  # 48 - 24 = 24 extra hours × ₹100/hr = ₹2400
    """

    # GUARD CLAUSE: If within the allowed time, no fine applies
    # WHY check this first? It's the most common case (early return pattern).
    # Most vehicles park for a few hours, not 24+. By returning early,
    # we skip the math for the majority of calls.
    if hours_parked <= MAX_PARKING_HOURS:
        return 0.0

    # Calculate how many hours BEYOND the limit the vehicle stayed
    overstay_hours = hours_parked - MAX_PARKING_HOURS

    # Fine = extra hours × penalty rate per hour
    return overstay_hours * OVERSTAY_PENALTY_PER_HOUR
