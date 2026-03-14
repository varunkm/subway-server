"""
Configuration Management for MTA Subway Server
================================================

This module loads and validates configuration from two sources:
  1. Environment variables (.env file) — server settings, API keys
  2. JSON configuration (config/config.json) — stations, lines, direction

The Config class validates everything at startup and exits with a clear
error message if anything is wrong. This "fail fast" approach prevents
the server from running with bad configuration.

Usage:
    from server.config import Config
    config = Config()
    print(config.stations)   # list of station dicts
    print(config.max_trains) # e.g. 3

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
# a group of lines. Lines within a single station must belong to the same
# feed. Lines across different stations can be in different feeds.
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
        stations    (list): List of station dicts, each with label, stop_id, lines, direction
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
            print("❌ Configuration Error: Missing config file")
            print(f"\n   Cannot find: {config_path}")
            print("\n   Solution:")
            print(f"     1. cp config/config.example.json {config_path}")
            print(f"     2. Edit {config_path} with your stations")
            sys.exit(1)

        except json.JSONDecodeError as e:
            print("❌ Configuration Error: Invalid JSON")
            print(f"\n   File: {config_path}")
            print(f"   Error: {e}")
            print("\n   Tip: Use a JSON validator to check your syntax")
            sys.exit(1)

        except ValueError as e:
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
        """
        load_dotenv()

        self.mta_api_key = os.getenv("MTA_API_KEY", "")
        self.flask_env = os.getenv("FLASK_ENV", "production")
        self.flask_host = os.getenv("FLASK_HOST", "0.0.0.0")
        self.flask_port = int(os.getenv("FLASK_PORT", "5000"))
        self.cache_ttl = int(os.getenv("CACHE_TTL", "60"))
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
        """
        if not Path(config_path).exists():
            raise FileNotFoundError(config_path)

        with open(config_path, "r") as f:
            data = json.load(f)

        self.stations = data["stations"]
        self.max_trains = data.get("max_trains", 3)

    # =========================================================================
    # Validation methods
    # =========================================================================

    def _validate(self):
        """
        Validate all configuration values.

        Raises:
            ValueError: With a detailed, human-friendly error message.
        """
        # --- stations must be a non-empty array ---
        if not isinstance(self.stations, list) or not self.stations:
            raise ValueError(
                "\"stations\" must be a non-empty array.\n"
                "   Each station needs: label, stop_id, lines, direction."
            )

        # --- validate each station ---
        for i, station in enumerate(self.stations):
            self._validate_station(station, i)

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

    def _validate_station(self, station, index):
        """
        Validate a single station entry.

        Args:
            station: Dict with label, stop_id, lines, direction.
            index: Station index for error messages.

        Raises:
            ValueError: With a detailed error message.
        """
        prefix = f"Station [{index}]"

        # --- required fields ---
        for field in ("label", "stop_id", "lines", "direction"):
            if field not in station:
                raise ValueError(
                    f"{prefix}: missing required field \"{field}\"."
                )

        label = station["label"]
        stop_id = station["stop_id"]
        lines = station["lines"]
        direction = station["direction"]

        # --- label must be non-empty string ---
        if not isinstance(label, str) or not label.strip():
            raise ValueError(
                f"{prefix}: \"label\" must be a non-empty string."
            )

        # --- stop_id format ---
        if not re.match(r"^[A-Z0-9]+[NS]$", stop_id):
            raise ValueError(
                f"{prefix} ({label}): Invalid stop_id: '{stop_id}'\n"
                f"   Expected format: <base_id><direction>  (e.g. '631N', 'Q05S')\n"
                f"   The last character must be 'N' (north) or 'S' (south)."
            )

        # --- direction ---
        if direction not in ("N", "S"):
            raise ValueError(
                f"{prefix} ({label}): Invalid direction: '{direction}'\n"
                f"   Must be 'N' (northbound/uptown) or 'S' (southbound/downtown)."
            )

        # --- direction must match stop_id suffix ---
        if not stop_id.endswith(direction):
            raise ValueError(
                f"{prefix} ({label}): Direction mismatch: stop_id='{stop_id}' "
                f"but direction='{direction}'\n"
                f"   The stop_id ends with '{stop_id[-1]}', "
                f"so direction must also be '{stop_id[-1]}'."
            )

        # --- lines not empty ---
        if not lines:
            raise ValueError(
                f"{prefix} ({label}): Lines list cannot be empty.\n"
                f"   Specify at least one train line, e.g. [\"4\"] or [\"A\", \"C\"]."
            )

        # --- all lines within this station must be in the same feed ---
        first_feed_id = LINE_TO_FEED.get(lines[0])
        if first_feed_id is None:
            raise ValueError(
                f"{prefix} ({label}): Unknown train line: '{lines[0]}'\n"
                f"   Valid lines: {sorted(LINE_TO_FEED.keys())}"
            )

        for line in lines[1:]:
            if LINE_TO_FEED.get(line) != first_feed_id:
                raise ValueError(
                    f"{prefix} ({label}): Cannot mix lines from different feeds: {lines}\n"
                    f"   '{lines[0]}' is in feed '{first_feed_id}', "
                    f"but '{line}' is in feed '{LINE_TO_FEED.get(line, '???')}'\n"
                    f"   Valid groups: {_format_feed_groups()}"
                )

    # =========================================================================
    # Public helper methods
    # =========================================================================

    def get_feed_ids(self):
        """
        Get the set of distinct GTFS feed IDs needed across all stations.

        Returns:
            set: Feed ID strings, e.g. {'1', 'NQRW'}.
        """
        feed_ids = set()
        for station in self.stations:
            feed_ids.add(LINE_TO_FEED[station["lines"][0]])
        return feed_ids

    def __repr__(self):
        """Readable string representation (does NOT expose API key)."""
        labels = [s["label"] for s in self.stations]
        return (
            f"Config(stations={labels}, max_trains={self.max_trains})"
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
    print(f"  Stations:   {len(config.stations)}")
    for station in config.stations:
        print(f"    - {station['label']}: stop={station['stop_id']}, "
              f"lines={', '.join(station['lines'])}, dir={station['direction']}")
    print(f"  Max trains: {config.max_trains}")
    print(f"  Feed IDs:   {', '.join(sorted(config.get_feed_ids()))}")
    print(f"  Server:     {config.flask_host}:{config.flask_port}")
    print(f"  Cache TTL:  {config.cache_ttl}s")
