"""
Flask Application — MTA Subway Server
=======================================

This is the main entry point for the server. It creates a Flask web app
with three endpoints:

    GET /subway      → Plaintext wall clock times (for ESP32)
    GET /subway/json → JSON with times + metadata (for debugging)
    GET /health      → Server health and configuration (for monitoring)

The server caches subway data for 60 seconds (configurable) and keeps
a "last known good" fallback cache that never expires. This means the
ESP32 always gets data, even when the MTA API is down.

Run directly:
    python server/app.py

Or with Flask:
    FLASK_APP=server.app flask run
"""

import logging
import time
import traceback
from datetime import datetime

import pytz
from flask import Flask, Response, jsonify
from flask_caching import Cache

from config import Config
from subway_service import SubwayService
from utils import format_compact_times, get_local_ip


# =============================================================================
# Application setup
# =============================================================================

# Load and validate configuration (exits with error if invalid)
config = Config()

# Create Flask app
app = Flask(__name__)

# Configure caching
app.config["CACHE_TYPE"] = "SimpleCache"
app.config["CACHE_DEFAULT_TIMEOUT"] = config.cache_ttl
cache = Cache(app)

# Create the subway data service
subway_service = SubwayService(config)

# New York timezone for timestamps in responses
NY_TZ = pytz.timezone("America/New_York")

# Track when the server started (for uptime reporting in /health)
SERVER_START_TIME = time.time()

# Track the last successful MTA fetch (for health reporting)
_last_fetch = {"time": None}


# =============================================================================
# Logging setup
# =============================================================================

def _setup_logging():
    """Configure logging based on the loaded config."""
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if config.log_file:
        file_handler = logging.FileHandler(config.log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


_setup_logging()
logger = logging.getLogger(__name__)


# =============================================================================
# Cache helpers
# =============================================================================

def _fetch_and_cache():
    """
    Fetch fresh subway data, update both caches, and return the data.

    Returns:
        tuple: (data_dict, is_from_cache, is_fallback)
    """
    cached = cache.get("subway_data")
    if cached is not None:
        logger.debug("Cache hit — returning cached subway data")
        return cached, True, False

    logger.debug("Cache miss — fetching fresh data from MTA")
    data = subway_service.get_arrivals()

    if "error" not in data["metadata"]:
        cache.set("subway_data", data, timeout=config.cache_ttl)
        cache.set("subway_data_last_good", data, timeout=0)
        _last_fetch["time"] = datetime.now(NY_TZ)
        return data, False, False

    logger.warning("MTA fetch failed, checking fallback cache")
    fallback = cache.get("subway_data_last_good")
    if fallback is not None:
        logger.info("Returning fallback (last known good) data")
        return fallback, False, True

    logger.warning("No fallback cache available")
    return data, False, False


# =============================================================================
# Plaintext formatting
# =============================================================================

def _format_plaintext(stations):
    """
    Format subway arrival data as plaintext for the ESP32.

    Each train line is one row: <label> <line>)<compact times>
    The last line is always "Updated H:MM".
    If a station has zero trains, shows "<label>) No trains".

    Args:
        stations (list): List of station dicts from get_arrivals().

    Returns:
        str: Formatted plaintext for the e-ink display.
    """
    output_lines = []

    for station in stations:
        label = station["label"]
        lines = station["lines"]

        if not lines:
            output_lines.append(f"{label}) No trains")
        else:
            for line_id, times in lines.items():
                compact = format_compact_times(times)
                output_lines.append(f"{label} {line_id}){compact}")

    # Append the update timestamp
    now = datetime.now(NY_TZ)
    updated_time = now.strftime("%-I:%M")
    output_lines.append(f"Updated {updated_time}")

    return "\n".join(output_lines)


# =============================================================================
# API Endpoints
# =============================================================================

@app.route("/subway", methods=["GET"])
def get_subway_plaintext():
    """
    Primary endpoint — returns plaintext wall clock times for the ESP32.

    Response format (text/plain):
        86/2nd Q)5:34,45,56;6:01,04
        86/Lex 4)5:34,45;6:01
        86/Lex 5)5:38,52
        86/Lex 6)5:41,55;6:03
        Updated 5:34
    """
    try:
        data, is_cached, is_fallback = _fetch_and_cache()
        plaintext = _format_plaintext(data["stations"])

        logger.info(
            f"GET /subway → {len(data['stations'])} stations "
            f"(cached={is_cached}, fallback={is_fallback})"
        )

        return Response(plaintext, mimetype="text/plain")

    except Exception as e:
        logger.error(f"Error in /subway endpoint: {e}")
        logger.error(traceback.format_exc())
        return Response(
            "Service temporarily unavailable",
            status=500,
            mimetype="text/plain",
        )


@app.route("/subway/json", methods=["GET"])
def get_subway_json():
    """
    Debug endpoint — returns JSON with arrival times and metadata.

    JSON keeps the full H:MM times (no compact format) since it's for debugging.
    """
    try:
        data, is_cached, is_fallback = _fetch_and_cache()

        last_update = data["metadata"]["last_update"]
        now = datetime.now(NY_TZ)
        cache_age = int((now - last_update).total_seconds()) if last_update else 0

        updated_time = now.strftime("%-I:%M")

        # Build stations list with stop_id and direction from config
        stations_json = []
        for i, station_data in enumerate(data["stations"]):
            station_config = config.stations[i] if i < len(config.stations) else {}
            stations_json.append({
                "label": station_data["label"],
                "stop_id": station_config.get("stop_id", ""),
                "direction": station_config.get("direction", ""),
                "lines": station_data["lines"],
            })

        response = {
            "stations": stations_json,
            "metadata": {
                "last_update": last_update.isoformat() if last_update else None,
                "cached": is_cached,
                "cache_age_seconds": cache_age,
                "using_fallback": is_fallback,
                "updated": updated_time,
            },
        }

        logger.info(
            f"GET /subway/json → {len(data['stations'])} stations "
            f"(cached={is_cached}, fallback={is_fallback})"
        )

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error in /subway/json endpoint: {e}")
        logger.error(traceback.format_exc())
        return jsonify({
            "error": "Internal server error",
            "message": str(e),
        }), 500


@app.route("/health", methods=["GET"])
def get_health():
    """
    Health check endpoint — returns server status and configuration.

    Status values:
      - "healthy":   MTA data is available and fresh
      - "degraded":  Using fallback cache (MTA may be unreachable)
      - "unhealthy": No data available at all
    """
    normal_cache = cache.get("subway_data")
    fallback_cache = cache.get("subway_data_last_good")

    if normal_cache is not None:
        status = "healthy"
    elif fallback_cache is not None:
        status = "degraded"
    else:
        status = "unhealthy"

    uptime_seconds = int(time.time() - SERVER_START_TIME)

    stations_config = []
    for station in config.stations:
        stations_config.append({
            "label": station["label"],
            "stop_id": station["stop_id"],
            "lines": station["lines"],
            "direction": station["direction"],
        })

    response = {
        "status": status,
        "timestamp": datetime.now(NY_TZ).isoformat(),
        "config": {
            "stations": stations_config,
            "max_trains": config.max_trains,
            "cache_ttl": config.cache_ttl,
        },
        "system": {
            "cache_enabled": True,
            "last_successful_fetch": (
                _last_fetch["time"].isoformat()
                if _last_fetch["time"]
                else None
            ),
            "server_uptime_seconds": uptime_seconds,
        },
    }

    return jsonify(response)


# =============================================================================
# Error handlers
# =============================================================================

@app.errorhandler(404)
def not_found(error):
    """Handle requests to undefined endpoints with a helpful message."""
    return jsonify({
        "error": "Not found",
        "message": "The requested endpoint does not exist.",
        "available_endpoints": ["/subway", "/subway/json", "/health"],
    }), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle unexpected server errors."""
    logger.error(f"Internal server error: {error}")
    return jsonify({
        "error": "Internal server error",
        "message": "An unexpected error occurred.",
    }), 500


# =============================================================================
# Main entry point
# =============================================================================

if __name__ == "__main__":
    local_ip = get_local_ip()
    print("\n" + "=" * 60)
    print("  MTA Subway Server")
    print("=" * 60)
    print(f"  Stations:  {len(config.stations)}")
    for station in config.stations:
        print(f"    - {station['label']}: {station['stop_id']} "
              f"({station['direction']}) [{', '.join(station['lines'])}]")
    print(f"  Max trains: {config.max_trains} per line")
    print(f"  Cache TTL: {config.cache_ttl}s")
    print("-" * 60)
    print(f"  Local:     http://127.0.0.1:{config.flask_port}/subway")
    print(f"  Network:   http://{local_ip}:{config.flask_port}/subway")
    print(f"  Health:    http://{local_ip}:{config.flask_port}/health")
    print("=" * 60 + "\n")

    app.run(
        host=config.flask_host,
        port=config.flask_port,
        debug=(config.flask_env == "development"),
    )
