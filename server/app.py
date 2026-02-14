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

from server.config import Config
from server.subway_service import SubwayService
from server.utils import get_local_ip


# =============================================================================
# Application setup
# =============================================================================

# Load and validate configuration (exits with error if invalid)
config = Config()

# Create Flask app
app = Flask(__name__)

# Configure caching — SimpleCache stores data in a Python dict in memory.
# This is perfect for a single-process server on a Raspberry Pi.
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
# This is a mutable dict so it can be updated from within functions
_last_fetch = {"time": None}


# =============================================================================
# Logging setup
# =============================================================================

def _setup_logging():
    """
    Configure logging based on the loaded config.

    Sets up two handlers:
      - Console (always) — so you can see output when running interactively
      - File (optional)  — if LOG_FILE is set in .env
    """
    # Determine the numeric log level from the config string
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    # Set up the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Log message format: timestamp - module - level - message
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Console handler (always active)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (only if LOG_FILE is configured)
    if config.log_file:
        file_handler = logging.FileHandler(config.log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


# Set up logging immediately
_setup_logging()
logger = logging.getLogger(__name__)


# =============================================================================
# Cache helpers
# =============================================================================
# We maintain two caches:
#   1. "subway_data" — normal cache with a 60-second TTL
#   2. "subway_data_last_good" — never expires, updated on every successful
#      fetch. Used as a fallback when MTA is unreachable.

def _fetch_and_cache():
    """
    Fetch fresh subway data, update both caches, and return the data.

    Returns:
        tuple: (data_dict, is_from_cache, is_fallback)
            - data_dict: The subway data (lines + metadata)
            - is_from_cache: True if served from normal cache
            - is_fallback: True if using the last-known-good fallback
    """
    # --- Check the normal cache first ---
    cached = cache.get("subway_data")
    if cached is not None:
        logger.debug("Cache hit — returning cached subway data")
        return cached, True, False

    # --- Cache miss — fetch fresh data from MTA ---
    logger.debug("Cache miss — fetching fresh data from MTA")
    data = subway_service.get_arrivals()

    # Check if the fetch actually succeeded (no error in metadata)
    if "error" not in data["metadata"]:
        # Success! Update both caches.
        cache.set("subway_data", data, timeout=config.cache_ttl)
        cache.set("subway_data_last_good", data, timeout=0)  # 0 = never expire
        _last_fetch["time"] = datetime.now(NY_TZ)
        return data, False, False

    # --- Fetch failed — try the fallback cache ---
    logger.warning("MTA fetch failed, checking fallback cache")
    fallback = cache.get("subway_data_last_good")
    if fallback is not None:
        logger.info("Returning fallback (last known good) data")
        return fallback, False, True

    # --- No fallback available either — return the error response ---
    logger.warning("No fallback cache available")
    return data, False, False


# =============================================================================
# Plaintext formatting
# =============================================================================

def _format_plaintext(lines_dict):
    """
    Format subway arrival data as plaintext for the ESP32.

    The ESP32 receives this text, parses it, and renders it on the
    e-ink display. The format is intentionally simple to parse:
        <line>: <time1>, <time2>, <time3>

    Args:
        lines_dict (dict): Mapping of line → list of time strings.
            Example: {'4': ['3:45', '3:52', '4:01'], '5': ['3:47']}

    Returns:
        str: Formatted plaintext. Example:
            "4: 3:45, 3:52, 4:01\\n5: 3:47"
            Returns "No trains scheduled" if lines_dict is empty.
    """
    if not lines_dict:
        return "No trains scheduled"

    # Build one line of text per train line
    output_lines = []
    for line, times in lines_dict.items():
        # Join times with ", " and prepend the line identifier
        # Example: "4: 3:45, 3:52, 4:01"
        output_lines.append(f"{line}: {', '.join(times)}")

    # Join all lines with newlines
    return "\n".join(output_lines)


# =============================================================================
# API Endpoints
# =============================================================================

@app.route("/subway", methods=["GET"])
def get_subway_plaintext():
    """
    Primary endpoint — returns plaintext wall clock times for the ESP32.

    Response format (text/plain):
        4: 3:45, 3:52, 4:01
        5: 3:47, 3:59, 4:08
        6: 3:44, 3:51, 4:05

    Returns "No trains scheduled" if no data is available.
    Returns "Service temporarily unavailable" on critical errors.
    """
    try:
        data, is_cached, is_fallback = _fetch_and_cache()
        plaintext = _format_plaintext(data["lines"])

        logger.info(
            f"GET /subway → {len(data['lines'])} lines "
            f"(cached={is_cached}, fallback={is_fallback})"
        )

        return Response(plaintext, mimetype="text/plain")

    except Exception as e:
        # Something went very wrong — log it and return an error message
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

    This endpoint is useful for:
      - Debugging: see exactly what data the server has
      - Monitoring: check cache status and fetch timing
      - Development: easier to work with than plaintext

    Response format (application/json):
        {
            "lines": {"4": ["3:45", "3:52", "4:01"], ...},
            "metadata": {
                "stop_id": "631N",
                "direction": "N",
                "last_update": "2026-02-13T15:42:18-05:00",
                "cached": true,
                "cache_age_seconds": 23,
                "using_fallback": false
            }
        }
    """
    try:
        data, is_cached, is_fallback = _fetch_and_cache()

        # Build the response with display-friendly metadata
        last_update = data["metadata"]["last_update"]
        now = datetime.now(NY_TZ)
        cache_age = int((now - last_update).total_seconds()) if last_update else 0

        response = {
            "lines": data["lines"],
            "metadata": {
                "stop_id": config.stop_id,
                "direction": config.direction,
                "last_update": last_update.isoformat() if last_update else None,
                "cached": is_cached,
                "cache_age_seconds": cache_age,
                "using_fallback": is_fallback,
            },
        }

        logger.info(
            f"GET /subway/json → {len(data['lines'])} lines "
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

    Not cached — always returns fresh information.

    Status values:
      - "healthy":   MTA data is available and fresh
      - "degraded":  Using fallback cache (MTA may be unreachable)
      - "unhealthy": No data available at all
    """
    # Determine health status based on available data
    normal_cache = cache.get("subway_data")
    fallback_cache = cache.get("subway_data_last_good")

    if normal_cache is not None:
        status = "healthy"
    elif fallback_cache is not None:
        status = "degraded"
    else:
        status = "unhealthy"

    # Calculate server uptime
    uptime_seconds = int(time.time() - SERVER_START_TIME)

    response = {
        "status": status,
        "timestamp": datetime.now(NY_TZ).isoformat(),
        "config": {
            "stop_id": config.stop_id,
            "lines": config.lines,
            "direction": config.direction,
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
    # Print a friendly startup banner
    local_ip = get_local_ip()
    print("\n" + "=" * 60)
    print("  MTA Subway Server")
    print("=" * 60)
    print(f"  Stop:      {config.stop_id} ({config.direction})")
    print(f"  Lines:     {', '.join(config.lines)}")
    print(f"  Max trains: {config.max_trains} per line")
    print(f"  Cache TTL: {config.cache_ttl}s")
    print("-" * 60)
    print(f"  Local:     http://127.0.0.1:{config.flask_port}/subway")
    print(f"  Network:   http://{local_ip}:{config.flask_port}/subway")
    print(f"  Health:    http://{local_ip}:{config.flask_port}/health")
    print("=" * 60 + "\n")

    # Start the Flask development server
    # In production on a Raspberry Pi, this is fine for single-user use.
    # For heavier loads, use gunicorn or similar WSGI server.
    app.run(
        host=config.flask_host,
        port=config.flask_port,
        debug=(config.flask_env == "development"),
    )
