"""
Configuration Management for MTA Subway Server
================================================

This module loads and validates configuration from two sources:
  1. Environment variables (.env file) — server settings, API keys
  2. JSON configuration (config/config.json) — subway stop, lines, direction

The Config class validates everything at startup and exits with a clear
error message if anything is wrong. This "fail fast" approach prevents
the server from running with bad configuration.

Usage:
    from server.config import Config
    config = Config()
    print(config.stop_id)   # e.g. '631N'
    print(config.lines)     # e.g. ['4', '5', '6']

Run standalone to validate your configuration:
    python server/config.py
"""

import os
import json
import sys
import re
from pathlib import Path
from dotenv import load_dotenv


# =============================================================================
# Feed group definitions
# =============================================================================
# MTA publishes GTFS-Realtime data in separate feeds. Each feed covers
# a group of lines. We can only fetch one feed at a time, so all the
# lines a user wants to monitor must belong to the same feed.
#
# Feed ID → list of lines in that feed
FEED_GROUPS = {
    "1":    ["1", "2", "3", "4", "5", "6"],   # Numbered lines
    "ACE":  ["A", "C", "E"],
    "BDFM": ["B", "D", "F", "M"],
    "NQRW": ["N", "Q", "R", "W"],
    "L":    ["L"],
    "G":    ["G"],
    "JZ":   ["J", "Z"],
    "7":    ["7"],
    "SI":   ["S"],  # Staten Island Railway
}

# Reverse lookup: line → feed ID  (built from FEED_GROUPS above)
# e.g. "4" → "1", "A" → "ACE", "L" → "L"
LINE_TO_FEED = {}
for feed_id, lines_in_feed in FEED_GROUPS.items():
    for line in lines_in_feed:
        LINE_TO_FEED[line] = feed_id


class Config:
    """
    Configuration manager for the MTA Subway Server.

    Loads settings from .env and config.json, validates them, and exposes
    them as simple attributes. If anything is invalid, the constructor
    prints a helpful error and exits the process.

    Attributes (from .env):
        mta_api_key  (str): Optional MTA API key (empty string if unused)
        flask_env    (str): 'production' or 'development'
        flask_host   (str): IP to bind to — '0.0.0.0' for all interfaces
        flask_port   (int): Port number — default 5000
        cache_ttl    (int): Cache lifetime in seconds — default 60
        log_level    (str): Logging level — default 'INFO'
        log_file     (str): Path to log file — empty string for console only

    Attributes (from config.json):
        stop_id      (str): GTFS stop ID with direction, e.g. '631N'
        lines       (list): Train lines to monitor, e.g. ['4', '5', '6']
        direction    (str): 'N' (northbound/uptown) or 'S' (southbound/downtown)
        max_trains   (int): Max arrivals per line — default 3
    """

    def __init__(self, config_path="config/config.json"):
        """
        Load and validate all configuration.

        Args:
            config_path: Path to the JSON config file (relative to project root
                         or absolute). Defaults to 'config/config.json'.

        Exits:
            Calls sys.exit(1) with a helpful message if config is invalid.
        """
        try:
            # --- Step 1: Load environment variables from .env ---
            self._load_env()

            # --- Step 2: Load user settings from config.json ---
            self._load_json(config_path)

            # --- Step 3: Validate everything ---
            self._validate()

        except FileNotFoundError:
            # The config.json file doesn't exist yet
            print("❌ Configuration Error: Missing config file")
            print(f"\n   Cannot find: {config_path}")
            print("\n   Solution:")
            print(f"     1. cp config/config.example.json {config_path}")
            print(f"     2. Edit {config_path} with your stop_id, lines, direction")
            sys.exit(1)

        except json.JSONDecodeError as e:
            # The config.json file has invalid JSON syntax
            print("❌ Configuration Error: Invalid JSON")
            print(f"\n   File: {config_path}")
            print(f"   Error: {e}")
            print("\n   Tip: Use a JSON validator to check your syntax")
            sys.exit(1)

        except ValueError as e:
            # A config value failed validation
            print("❌ Configuration Error: Invalid settings")
            print(f"\n   {e}")
            print(f"\n   Fix the issue in: {config_path}")
            sys.exit(1)

    # =========================================================================
    # Loading methods
    # =========================================================================

    def _load_env(self):
        """
        Load server configuration from environment variables.

        Reads from .env file (via python-dotenv) with sensible defaults.
        Environment variables are used for server infrastructure settings
        that shouldn't live in the user-facing config.json.
        """
        # load_dotenv() reads .env into os.environ. Safe to call even if
        # .env doesn't exist — it just does nothing in that case.
        load_dotenv()

        # MTA API key — optional since nyct-gtfs v2.0+ works without one
        self.mta_api_key = os.getenv("MTA_API_KEY", "")

        # Flask settings
        self.flask_env = os.getenv("FLASK_ENV", "production")
        self.flask_host = os.getenv("FLASK_HOST", "0.0.0.0")
        self.flask_port = int(os.getenv("FLASK_PORT", "5000"))

        # Cache time-to-live in seconds. 60s is a good balance between
        # freshness and not hammering the MTA API.
        self.cache_ttl = int(os.getenv("CACHE_TTL", "60"))

        # Logging
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.log_file = os.getenv("LOG_FILE", "")

    def _load_json(self, config_path):
        """
        Load user-facing subway configuration from a JSON file.

        Args:
            config_path: Path to the JSON config file.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            json.JSONDecodeError: If the JSON is malformed.
            KeyError: If a required field is missing.
        """
        if not Path(config_path).exists():
            raise FileNotFoundError(config_path)

        with open(config_path, "r") as f:
            data = json.load(f)

        # Required fields
        self.stop_id = data["stop_id"]
        self.lines = data["lines"]
        self.direction = data["direction"]

        # Optional fields with defaults
        self.max_trains = data.get("max_trains", 3)

    # =========================================================================
    # Validation methods
    # =========================================================================

    def _validate(self):
        """
        Validate all configuration values.

        Checks performed:
          - stop_id matches the expected format (letters/digits + N or S)
          - direction is 'N' or 'S'
          - direction suffix matches the stop_id suffix
          - lines list is non-empty
          - all lines belong to the same GTFS feed
          - max_trains is between 1 and 5
          - port is a valid port number
          - cache_ttl is at least 10 seconds

        Raises:
            ValueError: With a detailed, human-friendly error message.
        """
        # --- stop_id format ---
        # Valid examples: '631N', 'A42S', 'R16N'
        # Must be uppercase alphanumeric followed by N or S
        if not re.match(r"^[A-Z0-9]+[NS]$", self.stop_id):
            raise ValueError(
                f"Invalid stop_id: '{self.stop_id}'\n"
                f"   Expected format: <base_id><direction>  (e.g. '631N', 'A42S')\n"
                f"   The last character must be 'N' (north) or 'S' (south)."
            )

        # --- direction ---
        if self.direction not in ("N", "S"):
            raise ValueError(
                f"Invalid direction: '{self.direction}'\n"
                f"   Must be 'N' (northbound/uptown) or 'S' (southbound/downtown)."
            )

        # --- direction must match stop_id suffix ---
        # A common mistake is stop_id='631S' with direction='N'
        if not self.stop_id.endswith(self.direction):
            raise ValueError(
                f"Direction mismatch: stop_id='{self.stop_id}' but direction='{self.direction}'\n"
                f"   The stop_id ends with '{self.stop_id[-1]}', "
                f"so direction must also be '{self.stop_id[-1]}'."
            )

        # --- lines not empty ---
        if not self.lines:
            raise ValueError(
                "Lines list cannot be empty.\n"
                "   Specify at least one train line, e.g. [\"4\"] or [\"A\", \"C\"]."
            )

        # --- all lines in the same feed ---
        # Find the feed for the first line
        first_feed_id = LINE_TO_FEED.get(self.lines[0])
        if first_feed_id is None:
            raise ValueError(
                f"Unknown train line: '{self.lines[0]}'\n"
                f"   Valid lines: {sorted(LINE_TO_FEED.keys())}"
            )

        # Check every other line is in the same feed
        for line in self.lines[1:]:
            if LINE_TO_FEED.get(line) != first_feed_id:
                raise ValueError(
                    f"Cannot mix lines from different feeds: {self.lines}\n"
                    f"   '{self.lines[0]}' is in feed '{first_feed_id}', "
                    f"but '{line}' is in feed '{LINE_TO_FEED.get(line, '???')}'\n"
                    f"   Valid groups: {_format_feed_groups()}"
                )

        # --- max_trains range ---
        if not 1 <= self.max_trains <= 5:
            raise ValueError(
                f"Invalid max_trains: {self.max_trains}\n"
                f"   Must be between 1 and 5. Recommended: 3."
            )

        # --- port range ---
        if not 1 <= self.flask_port <= 65535:
            raise ValueError(
                f"Invalid port: {self.flask_port}\n"
                f"   Must be between 1 and 65535."
            )

        # --- cache TTL ---
        if self.cache_ttl < 10:
            raise ValueError(
                f"Invalid cache_ttl: {self.cache_ttl}\n"
                f"   Must be at least 10 seconds. Recommended: 60."
            )

    # =========================================================================
    # Public helper methods
    # =========================================================================

    def get_feed_id(self):
        """
        Get the GTFS feed ID for the configured lines.

        Returns:
            str: Feed ID string, e.g. '1' for lines 1-6, 'ACE' for A/C/E.

        This works because _validate() already confirmed all lines are in
        the same feed, so we just look up the first line.
        """
        return LINE_TO_FEED[self.lines[0]]

    def __repr__(self):
        """Readable string representation (does NOT expose API key)."""
        return (
            f"Config(stop_id='{self.stop_id}', lines={self.lines}, "
            f"direction='{self.direction}', max_trains={self.max_trains})"
        )


# =============================================================================
# Module-level helpers
# =============================================================================

def _format_feed_groups():
    """Format FEED_GROUPS as a readable string for error messages."""
    parts = []
    for feed_id, lines in FEED_GROUPS.items():
        parts.append(f"{feed_id}: [{', '.join(lines)}]")
    return " | ".join(parts)


# =============================================================================
# Standalone validation
# =============================================================================
# Run `python server/config.py` to validate your configuration files
# without starting the server.

if __name__ == "__main__":
    print("Validating configuration...\n")
    config = Config()
    print("✅ Configuration is valid!\n")
    print(f"  Stop ID:    {config.stop_id}")
    print(f"  Lines:      {', '.join(config.lines)}")
    print(f"  Direction:  {config.direction} "
          f"({'Northbound' if config.direction == 'N' else 'Southbound'})")
    print(f"  Max trains: {config.max_trains}")
    print(f"  Feed ID:    {config.get_feed_id()}")
    print(f"  Server:     {config.flask_host}:{config.flask_port}")
    print(f"  Cache TTL:  {config.cache_ttl}s")
