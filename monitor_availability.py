#!/usr/bin/env python3
"""
Monitor availability of a specific room on an AirHost listing and send a
Discord notification when availability changes from unavailable to
available for a given date range.

This script fetches the main listing page to obtain the CSRF token and
session cookies. It then calls the internal rooms_availability endpoint
to check whether any inventory exists for the supplied date range and
room ID. The previous availability state is persisted to disk so that
alerts are only sent when the state changes.

Usage:
    python monitor_availability.py

Environment variables:
    TARGET_URL          The base URL of the listing (e.g. https://playandco.airhost.co/en/houses/612389)
    START_DATE          Start date in YYYY-MM-DD format (inclusive)
    END_DATE            End date in YYYY-MM-DD format (exclusive)
    ROOM_ID             Numeric ID of the room to monitor (e.g. 633845)
    DISCORD_WEBHOOK     Discord webhook URL used to post notifications
    STATE_FILE          Path to JSON file storing previous availability state (default: availability_state.json)
    USER_AGENT          Optional custom User‑Agent string
    STOP_ON_AVAILABLE   If set to any value, the script will exit after
                        sending the first availability notification.

The script respects the remote site by sending a plausible User‑Agent
string and by making only a single request per execution. For repeated
monitoring, run this script periodically (e.g. via cron or GitHub
Actions).

Note: Accessing undocumented internal endpoints may be fragile and
subject to anti‑scraping protections. If the request fails, the script
logs the error and exits without sending a notification.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import requests


def setup_logger() -> logging.Logger:
    """Configure and return a logger for console output."""
    logger = logging.getLogger("availability_monitor")
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


logger = setup_logger()


def get_env_var(name: str, default: Optional[str] = None) -> str:
    """Retrieve an environment variable or return default if not set."""
    value = os.getenv(name, default)
    if value is None:
        logger.error(f"Required environment variable {name} is not set.")
        sys.exit(1)
    return value


def fetch_csrf_token(session: requests.Session, url: str) -> str:
    """Fetch the listing page and extract the CSRF token.

    Args:
        session: requests.Session with cookies persisted.
        url: Full URL to the listing page.

    Returns:
        The CSRF token value as a string.

    Raises:
        ValueError: If the CSRF token cannot be found.
    """
    logger.debug(f"Fetching listing page at {url} to extract CSRF token.")
    response = session.get(url, timeout=30)
    response.raise_for_status()
    html = response.text
    # Extract CSRF token from a meta tag: <meta name="csrf-token" content="..." />
    match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    if not match:
        raise ValueError("CSRF token not found in page HTML.")
    token = match.group(1)
    logger.debug(f"Extracted CSRF token: {token[:8]}…")
    return token


def fetch_availability(
    session: requests.Session,
    base_url: str,
    start_date: str,
    end_date: str,
    room_id: str,
    csrf_token: str,
) -> dict:
    """Call the rooms_availability endpoint and return the JSON response.

    Args:
        session: requests.Session with cookies persisted.
        base_url: Base URL of the listing (no trailing slash).
        start_date: Start date (YYYY-MM-DD) inclusive.
        end_date: End date (YYYY-MM-DD) exclusive.
        room_id: Room ID to filter on (optional, may be None).
        csrf_token: CSRF token required by the endpoint.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        requests.HTTPError: If the HTTP request fails.
        ValueError: If the response is not JSON.
    """
    endpoint = base_url.rstrip("/") + "/rooms_availability"
    params = {
        "start_date": start_date,
        "end_date": end_date,
    }
    if room_id:
        params["room"] = room_id
    headers = {
        "X-CSRF-Token": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base_url,
    }
    logger.debug(f"Requesting availability from {endpoint} with params {params}.")
    response = session.get(endpoint, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError("Failed to decode JSON from availability response") from exc
    logger.debug(f"Received availability response: {str(data)[:200]}…")
    return data


def detect_available(data: Any) -> bool:
    """Recursively search the response dict/list for a truthy availability flag.

    The AirHost `rooms_availability` endpoint returns nested structures. We
    attempt to infer availability by looking for keys commonly used to
    represent inventory or availability. If any value associated with
    these keys evaluates to a positive integer or `True`, we consider the
    room available.

    Args:
        data: Parsed JSON response (dict or list).

    Returns:
        True if availability is detected, False otherwise.
    """
    # Keys that may indicate availability in the response. Based on common
    # patterns seen in AirHost's internal APIs and documented examples.
    availability_keys = {
        "available",
        "availability",
        "inventory",
        "inventories",
        "vacancy",
    }

    def search(obj: Any) -> bool:
        if isinstance(obj, dict):
            for key, value in obj.items():
                lkey = str(key).lower()
                if lkey in availability_keys:
                    # Interpret numeric or boolean values.
                    if isinstance(value, bool):
                        if value:
                            return True
                    elif isinstance(value, (int, float)):
                        if value > 0:
                            return True
                    elif isinstance(value, str):
                        # Strings like "available" may indicate availability.
                        if value.lower() == "available":
                            return True
                    # If the value is a container, continue searching.
                # Recursively search nested structures.
                if isinstance(value, (dict, list)):
                    if search(value):
                        return True
        elif isinstance(obj, list):
            for item in obj:
                if search(item):
                    return True
        return False

    return search(data)


def load_previous_state(path: Path) -> bool:
    """Load previous availability state from a JSON file.

    Args:
        path: Path to the JSON file storing the state.

    Returns:
        The previous availability state (True/False). Defaults to False if
        the file does not exist or cannot be parsed.
    """
    if not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("available", False))
    except Exception as exc:
        logger.warning(f"Failed to load previous state: {exc}. Defaulting to unavailable.")
        return False


def save_current_state(path: Path, available: bool) -> None:
    """Persist the current availability state to disk.

    Args:
        path: Path to the JSON file where state will be stored.
        available: Current availability flag to store.
    """
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump({"available": available}, f)
    except Exception as exc:
        logger.error(f"Failed to save state to {path}: {exc}")


def send_discord_notification(webhook_url: str, message: str) -> None:
    """Send a message to the provided Discord webhook URL.

    Args:
        webhook_url: Discord webhook endpoint.
        message: Message content to send.

    Raises:
        requests.HTTPError: If the webhook call fails.
    """
    payload = {
        "content": message,
        # Optionally you could customize username or avatar here if desired.
    }
    logger.info("Sending notification to Discord.")
    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()


def monitor_once() -> None:
    """Perform a single availability check and send a notification if necessary."""
    # Read configuration from environment variables.
    target_url = get_env_var("TARGET_URL")
    start_date = get_env_var("START_DATE")
    end_date = get_env_var("END_DATE")
    room_id = get_env_var("ROOM_ID")
    webhook_url = get_env_var("DISCORD_WEBHOOK")
    state_path = Path(os.getenv("STATE_FILE", "availability_state.json"))
    user_agent = os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) AccommodationMonitor/1.0 Safari/537.36",
    )
    stop_on_available = bool(os.getenv("STOP_ON_AVAILABLE"))

    logger.info(
        f"Checking availability for room {room_id} from {start_date} to {end_date} at {target_url}."
    )

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    try:
        csrf = fetch_csrf_token(session, target_url)
    except Exception as exc:
        logger.error(f"Failed to obtain CSRF token: {exc}")
        return

    try:
        availability_json = fetch_availability(
            session,
            base_url=target_url,
            start_date=start_date,
            end_date=end_date,
            room_id=room_id,
            csrf_token=csrf,
        )
    except Exception as exc:
        logger.error(f"Failed to fetch availability: {exc}")
        return

    currently_available = detect_available(availability_json)
    logger.info(f"Room availability is {'available' if currently_available else 'unavailable'}.")

    previous_available = load_previous_state(state_path)
    if currently_available and not previous_available:
        # Availability changed from unavailable to available – send notification.
        message = (
            f"🎉 The room (ID {room_id}) at {target_url} is now AVAILABLE "
            f"for {start_date} to {end_date}!"
        )
        try:
            send_discord_notification(webhook_url, message)
        except Exception as exc:
            logger.error(f"Failed to send Discord notification: {exc}")
        else:
            logger.info("Notification sent successfully.")
            # Optionally stop monitoring further if configured.
            if stop_on_available:
                logger.info("STOP_ON_AVAILABLE is set; exiting after first successful notification.")
                # Save state to avoid repeated notifications.
                save_current_state(state_path, currently_available)
                return

    # Save the current state for the next run.
    save_current_state(state_path, currently_available)


if __name__ == "__main__":
    monitor_once()