"""
Utility Functions for MTA Subway Server
========================================

Small helper functions used across the server. Kept separate from
business logic so they're easy to test and reuse.

Functions:
    format_wall_clock    — Convert a datetime to "H:MM" wall clock string
    format_compact_times — Compact a list of "H:MM" strings for display
    get_local_ip         — Get this machine's local network IP address
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
    if arrival_datetime.tzinfo is None:
        raise ValueError(
            f"Cannot format naive datetime (no timezone): {arrival_datetime}"
        )

    local_time = arrival_datetime.astimezone(NY_TZ)
    return local_time.strftime("%-I:%M")


def format_compact_times(times):
    """
    Compact a list of "H:MM" time strings for the e-ink display.

    Groups consecutive times by hour. First time in each hour group is
    shown as H:MM, subsequent times show only ,MM. Hour groups are
    separated by semicolons.

    Args:
        times (list[str]): List of "H:MM" strings, e.g. ["5:34", "5:45", "6:01"].

    Returns:
        str: Compact string, e.g. "5:34,45;6:01".

    Example:
        >>> format_compact_times(["5:34", "5:45", "5:56", "6:01", "6:04"])
        "5:34,45,56;6:01,04"
    """
    if not times:
        return ""

    groups = []
    current_hour = None
    current_parts = []

    for time_str in times:
        hour, minute = time_str.split(":")
        if hour != current_hour:
            if current_parts:
                groups.append(current_parts)
            current_hour = hour
            current_parts = [f"{hour}:{minute}"]
        else:
            current_parts.append(minute)

    if current_parts:
        groups.append(current_parts)

    return ";".join(",".join(parts) for parts in groups)


def get_local_ip():
    """
    Get this machine's IP address on the local network.

    Returns:
        str: Local IP address like "192.168.1.50", or "127.0.0.1" if
             the local IP can't be determined.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"
