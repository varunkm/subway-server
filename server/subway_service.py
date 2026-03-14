"""
MTA Subway Data Service
========================

Fetches real-time subway arrival data from the MTA GTFS-Realtime API
and converts it into simple wall clock times for the e-ink display.

This is the core business logic of the server. It:
  1. Connects to MTA via the nyct-gtfs library
  2. Fetches multiple GTFS feeds when stations span different feed groups
  3. Filters trains by stop, line, and direction
  4. Converts arrival datetimes to "H:MM" wall clock strings
  5. Handles errors gracefully (returns empty data, never crashes)

Usage:
    from server.config import Config
    from server.subway_service import SubwayService

    config = Config()
    service = SubwayService(config)
    data = service.get_arrivals()
    # data['stations'] → [{'label': '86/Lex', 'lines': {'4': ['3:45', ...]}}, ...]
"""

import logging
import traceback
from datetime import datetime, timedelta

import pytz
from nyct_gtfs import NYCTFeed

from config import LINE_TO_FEED
from utils import format_wall_clock


# Module-level logger — name will be "server.subway_service" in log output
logger = logging.getLogger(__name__)

# New York timezone, used to check if arrival times are in the future
NY_TZ = pytz.timezone("America/New_York")


class SubwayService:
    """
    Service for fetching and processing MTA subway arrival data.

    Call get_arrivals() to get the latest subway times. The method handles
    all errors internally and always returns a valid dict (possibly empty).
    """

    def __init__(self, config):
        """
        Initialize the subway service.

        Args:
            config (Config): Validated configuration object.
        """
        self.config = config
        self.feed_ids = config.get_feed_ids()

        labels = [s["label"] for s in config.stations]
        logger.info(
            f"SubwayService initialized: stations={labels}, "
            f"feeds={sorted(self.feed_ids)}"
        )

    def get_arrivals(self):
        """
        Fetch subway arrivals from MTA and return formatted wall clock times.

        Returns:
            dict: Always returns a dict with this shape:
                {
                    'stations': [
                        {
                            'label': str,
                            'lines': {'<line>': ['<time1>', ...], ...}
                        },
                        ...
                    ],
                    'metadata': {
                        'last_update': datetime,
                        'fetch_duration_ms': int,
                        'train_count': int
                    }
                }

                On error, 'stations' will have empty lines and metadata
                will contain an 'error' key.
        """
        start_time = datetime.now()

        try:
            # -----------------------------------------------------------------
            # Step 1: Fetch all required GTFS feeds (one per feed group)
            # -----------------------------------------------------------------
            feeds = {}
            for feed_id in self.feed_ids:
                # NYCTFeed accepts a line name; pick the first line in that feed
                # to identify which feed to fetch.
                representative_line = self._line_for_feed(feed_id)
                logger.info(f"Fetching GTFS feed '{feed_id}' via line '{representative_line}'")
                feeds[feed_id] = NYCTFeed(representative_line)

            # -----------------------------------------------------------------
            # Step 2: Process each station
            # -----------------------------------------------------------------
            stations_result = []

            for station in self.config.stations:
                arrivals_by_line = {}

                for line in station["lines"]:
                    feed_id = LINE_TO_FEED[line]
                    feed = feeds[feed_id]
                    times = self._get_times_for_line(feed, line, station)
                    if times:
                        arrivals_by_line[line] = times

                stations_result.append({
                    "label": station["label"],
                    "lines": arrivals_by_line,
                })

            # -----------------------------------------------------------------
            # Step 3: Build the response
            # -----------------------------------------------------------------
            duration_ms = int(
                (datetime.now() - start_time).total_seconds() * 1000
            )
            train_count = sum(
                len(t)
                for s in stations_result
                for t in s["lines"].values()
            )

            logger.info(
                f"Found {train_count} trains across "
                f"{len(stations_result)} stations in {duration_ms}ms"
            )

            return {
                "stations": stations_result,
                "metadata": {
                    "last_update": datetime.now(NY_TZ),
                    "fetch_duration_ms": duration_ms,
                    "train_count": train_count,
                },
            }

        except Exception as e:
            duration_ms = int(
                (datetime.now() - start_time).total_seconds() * 1000
            )
            logger.error(f"Error fetching subway data: {e}")
            logger.error(traceback.format_exc())

            return {
                "stations": [],
                "metadata": {
                    "last_update": datetime.now(NY_TZ),
                    "fetch_duration_ms": duration_ms,
                    "train_count": 0,
                    "error": str(e),
                },
            }

    def _line_for_feed(self, feed_id):
        """Get a representative line name for a feed ID to pass to NYCTFeed."""
        for station in self.config.stations:
            for line in station["lines"]:
                if LINE_TO_FEED[line] == feed_id:
                    return line
        return None

    def _get_times_for_line(self, feed, line, station):
        """
        Extract wall clock arrival times for one train line at a station.

        Args:
            feed (NYCTFeed): The fetched GTFS feed object.
            line (str): The train line to filter for, e.g. "4" or "Q".
            station (dict): Station config with stop_id, direction, etc.

        Returns:
            list[str]: Up to max_trains wall clock times, sorted
                       chronologically. Example: ['3:45', '3:52', '4:01'].
        """
        try:
            trips = feed.filter_trips(
                line_id=line,
                headed_for_stop_id=station["stop_id"],
                travel_direction=station["direction"],
            )

            logger.debug(
                f"Line {line}: found {len(trips)} trips "
                f"heading for {station['stop_id']}"
            )

            times = []
            for trip in trips:
                arrival_time = self._extract_arrival_time(trip, station["stop_id"])
                if arrival_time is not None:
                    times.append(arrival_time)

            times.sort()

            wall_clock_times = []
            for dt in times[: self.config.max_trains]:
                wall_clock_times.append(format_wall_clock(dt))

            return wall_clock_times

        except Exception as e:
            logger.warning(f"Error processing line {line}: {e}")
            return []

    def _extract_arrival_time(self, trip, stop_id):
        """
        Get the arrival datetime for a stop from a single trip.

        Args:
            trip: A nyct-gtfs Trip object.
            stop_id: The GTFS stop ID to look for.

        Returns:
            datetime or None: The arrival time if valid, None if the train
                should be skipped.
        """
        try:
            for stop_update in trip.stop_time_updates:
                if stop_update.stop_id == stop_id:
                    arrival = stop_update.arrival

                    if arrival is None:
                        return None

                    if arrival.tzinfo is None:
                        arrival = NY_TZ.localize(arrival)

                    now = datetime.now(NY_TZ)
                    if arrival <= now:
                        return None

                    return arrival

            return None

        except Exception as e:
            logger.debug(f"Error extracting arrival from trip: {e}")
            return None
