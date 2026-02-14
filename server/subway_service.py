"""
MTA Subway Data Service
========================

Fetches real-time subway arrival data from the MTA GTFS-Realtime API
and converts it into simple wall clock times for the e-ink display.

This is the core business logic of the server. It:
  1. Connects to MTA via the nyct-gtfs library
  2. Filters trains by stop, line, and direction
  3. Converts arrival datetimes to "H:MM" wall clock strings
  4. Handles errors gracefully (returns empty data, never crashes)

Usage:
    from server.config import Config
    from server.subway_service import SubwayService

    config = Config()
    service = SubwayService(config)
    data = service.get_arrivals()
    # data['lines'] → {'4': ['3:45', '3:52', '4:01'], ...}
"""

import logging
import traceback
from datetime import datetime, timedelta

import pytz
from nyct_gtfs import NYCTFeed

from server.utils import format_wall_clock


# Module-level logger — name will be "server.subway_service" in log output
logger = logging.getLogger(__name__)

# New York timezone, used to check if arrival times are in the future
NY_TZ = pytz.timezone("America/New_York")


class SubwayService:
    """
    Service for fetching and processing MTA subway arrival data.

    Call get_arrivals() to get the latest subway times. The method handles
    all errors internally and always returns a valid dict (possibly empty).

    Attributes:
        config: The Config object with stop_id, lines, direction, etc.
    """

    def __init__(self, config):
        """
        Initialize the subway service.

        Args:
            config (Config): Validated configuration object.
        """
        self.config = config

        logger.info(
            f"SubwayService initialized: stop={config.stop_id}, "
            f"lines={config.lines}, direction={config.direction}"
        )

    def get_arrivals(self):
        """
        Fetch subway arrivals from MTA and return formatted wall clock times.

        This is the main public method. It fetches live data from the MTA,
        filters and formats it, and returns a structured dict.

        Returns:
            dict: Always returns a dict with this shape:
                {
                    'lines': {
                        '<line>': ['<time1>', '<time2>', ...],
                        ...
                    },
                    'metadata': {
                        'stop_id': str,
                        'last_update': datetime,
                        'fetch_duration_ms': int,
                        'train_count': int
                    }
                }

                On error, 'lines' will be empty and metadata will contain
                an 'error' key describing what went wrong.
        """
        start_time = datetime.now()

        try:
            logger.info(f"Fetching GTFS data for stop {self.config.stop_id}")

            # -----------------------------------------------------------------
            # Step 1: Fetch the GTFS-Realtime feed from MTA
            # -----------------------------------------------------------------
            # We pass the first line as the feed specifier. nyct-gtfs knows
            # which feed to fetch based on the line (e.g., "4" → feed "1",
            # "A" → feed "ACE"). All our configured lines are in the same
            # feed (validated at startup), so any line works here.
            feed = NYCTFeed(self.config.lines[0])

            # -----------------------------------------------------------------
            # Step 2: Process each configured line
            # -----------------------------------------------------------------
            arrivals_by_line = {}

            for line in self.config.lines:
                times = self._get_times_for_line(feed, line)
                # Only include lines that have at least one upcoming train
                if times:
                    arrivals_by_line[line] = times

            # -----------------------------------------------------------------
            # Step 3: Build the response
            # -----------------------------------------------------------------
            duration_ms = int(
                (datetime.now() - start_time).total_seconds() * 1000
            )
            train_count = sum(len(t) for t in arrivals_by_line.values())

            logger.info(
                f"Found {train_count} trains across "
                f"{len(arrivals_by_line)} lines in {duration_ms}ms"
            )

            return {
                "lines": arrivals_by_line,
                "metadata": {
                    "stop_id": self.config.stop_id,
                    "last_update": datetime.now(NY_TZ),
                    "fetch_duration_ms": duration_ms,
                    "train_count": train_count,
                },
            }

        except Exception as e:
            # Catch ALL exceptions so the server never crashes from bad data
            # or network issues. Log the full traceback for debugging.
            duration_ms = int(
                (datetime.now() - start_time).total_seconds() * 1000
            )
            logger.error(f"Error fetching subway data: {e}")
            logger.error(traceback.format_exc())

            return {
                "lines": {},
                "metadata": {
                    "stop_id": self.config.stop_id,
                    "last_update": datetime.now(NY_TZ),
                    "fetch_duration_ms": duration_ms,
                    "train_count": 0,
                    "error": str(e),
                },
            }

    def _get_times_for_line(self, feed, line):
        """
        Extract wall clock arrival times for one train line at our stop.

        Args:
            feed (NYCTFeed): The fetched GTFS feed object.
            line (str): The train line to filter for, e.g. "4" or "A".

        Returns:
            list[str]: Up to max_trains wall clock times, sorted
                       chronologically. Example: ['3:45', '3:52', '4:01'].
                       Returns empty list if no trains found.
        """
        try:
            # Use nyct-gtfs's built-in filtering:
            #   line_id        → only trains on this line (e.g. "4")
            #   headed_for_stop_id → only trains that will stop at our stop
            #   travel_direction   → only trains going our direction
            trips = feed.filter_trips(
                line_id=line,
                headed_for_stop_id=self.config.stop_id,
                travel_direction=self.config.direction,
            )

            logger.debug(
                f"Line {line}: found {len(trips)} trips "
                f"heading for {self.config.stop_id}"
            )

            # For each trip, extract the arrival time at our stop
            times = []
            for trip in trips:
                arrival_time = self._extract_arrival_time(trip)
                if arrival_time is not None:
                    times.append(arrival_time)

            # Sort by arrival time (earliest first)
            times.sort()

            # Convert datetimes to wall clock strings and limit to max_trains
            wall_clock_times = []
            for dt in times[: self.config.max_trains]:
                wall_clock_times.append(format_wall_clock(dt))

            return wall_clock_times

        except Exception as e:
            # If something goes wrong processing one line, log it and
            # return empty — don't let one bad line break the whole response
            logger.warning(f"Error processing line {line}: {e}")
            return []

    def _extract_arrival_time(self, trip):
        """
        Get the arrival datetime for our stop from a single trip.

        Walks through the trip's stop_time_updates to find the entry
        matching our configured stop_id, then validates the arrival time.

        Args:
            trip: A nyct-gtfs Trip object.

        Returns:
            datetime or None: The arrival time if valid, None if the train
                should be skipped (departed, no data, etc.).
        """
        try:
            # Each trip has a list of stops it will make. Walk through them
            # to find the one matching our stop_id.
            for stop_update in trip.stop_time_updates:
                if stop_update.stop_id == self.config.stop_id:
                    arrival = stop_update.arrival

                    # Skip if no arrival time data
                    if arrival is None:
                        logger.debug(
                            f"Trip {trip.trip_id}: no arrival time "
                            f"for stop {self.config.stop_id}"
                        )
                        return None

                    # nyct-gtfs returns naive datetimes that are
                    # implicitly in New York time. We need to make them
                    # timezone-aware so we can compare and format them.
                    if arrival.tzinfo is None:
                        arrival = NY_TZ.localize(arrival)

                    # Skip if the train has already departed (arrival in past)
                    now = datetime.now(NY_TZ)
                    if arrival <= now:
                        return None

                    # Valid future arrival — return it
                    return arrival

            # Our stop wasn't found in this trip's stop list
            # (shouldn't happen since we filtered by headed_for_stop_id,
            #  but handle it gracefully just in case)
            return None

        except Exception as e:
            logger.debug(f"Error extracting arrival from trip: {e}")
            return None
