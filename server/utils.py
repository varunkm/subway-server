"""
Utility Functions for MTA Subway Server
========================================

Small helper functions used across the server. Kept separate from
business logic so they're easy to test and reuse.

Functions:
    format_wall_clock  — Convert a datetime to "H:MM" wall clock string
    get_local_ip       — Get this machine's local network IP address
"""

import socket
import pytz
from datetime import datetime


# New York timezone — used for all time formatting.
# MTA data is in NYC time, and our display shows NYC time.
NY_TZ = pytz.timezone("America/New_York")


def format_wall_clock(arrival_datetime):
    """
    Convert a datetime to a wall clock time string like "3:45".

    This is the core formatting function for the entire project. We display
    wall clock times (not "5 min") because the e-ink display refreshes
    infrequently. Wall clock times stay accurate between refreshes.

    Args:
        arrival_datetime (datetime): A timezone-aware datetime representing
            when the train arrives. Can be in any timezone — it will be
            converted to America/New_York.

    Returns:
        str: Time in "H:MM" format. Examples:
            - 3:45 PM  → "3:45"
            - 9:05 AM  → "9:05"
            - 12:00 PM → "12:00"  (noon)
            - 12:30 AM → "12:30"  (just after midnight)

    Raises:
        ValueError: If arrival_datetime has no timezone info.
    """
    # Safety check: we need timezone info to convert correctly
    if arrival_datetime.tzinfo is None:
        raise ValueError(
            f"Cannot format naive datetime (no timezone): {arrival_datetime}"
        )

    # Convert to New York time (handles EST/EDT automatically via pytz)
    local_time = arrival_datetime.astimezone(NY_TZ)

    # Format as 12-hour time without AM/PM
    # %-I gives the hour without a leading zero (Unix/macOS)
    # %M gives minutes with a leading zero (always two digits)
    # Example: 3:45 PM → "3:45", 12:05 AM → "12:05"
    return local_time.strftime("%-I:%M")


def get_local_ip():
    """
    Get this machine's IP address on the local network.

    This is useful for telling the user what IP to point the ESP32 at.
    Uses a socket trick: connect to an external IP (doesn't actually send
    data) to determine which local interface would be used.

    Returns:
        str: Local IP address like "192.168.1.50", or "127.0.0.1" if
             the local IP can't be determined.
    """
    try:
        # Create a UDP socket (doesn't actually send anything)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to a public IP to figure out our local interface
        # 8.8.8.8 is Google's DNS — we don't actually send any data
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        # If anything goes wrong, fall back to localhost
        return "127.0.0.1"
