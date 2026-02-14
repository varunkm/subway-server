# NYC Subway E-Ink Fridge Clock — Server

A Flask server that fetches real-time NYC subway arrivals from the MTA and serves them as wall clock times over HTTP. Built to run on a Raspberry Pi as part of a larger project: a battery-powered e-ink display on a fridge that shows upcoming train times.

Times are displayed as wall clock values (e.g. "3:45") rather than relative times ("5 min away") because the e-ink client only refreshes every 5 minutes — wall clock times stay accurate between refreshes.

## Example Output

`GET /subway`

```
4: 3:45, 3:52, 4:01
5: 3:47, 3:59, 4:08
6: 3:44, 3:51, 4:05
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config/config.example.json config/config.json  # edit with your stop/lines
cp .env.example .env

python server/app.py
```

### Configuration

Edit `config/config.json` with your station:

```json
{
  "stop_id": "631N",
  "lines": ["4", "5", "6"],
  "direction": "N",
  "max_trains": 3
}
```

To find your stop ID, download the [MTA stations CSV](http://web.mta.info/developers/data/nyct/subway/Stations.csv), find your station's GTFS Stop ID, and append "N" (northbound) or "S" (southbound).

### API Endpoints

| Endpoint | Format | Purpose |
|---|---|---|
| `GET /subway` | Plain text | Primary — designed for ESP32 consumption |
| `GET /subway/json` | JSON | Debugging — includes cache metadata |
| `GET /health` | JSON | Health check |

## Tech Stack

- **Framework:** Flask with in-memory caching (60s TTL)
- **MTA Data:** [nyct-gtfs](https://pypi.org/project/nyct-gtfs/) — no API key required
- **Resilience:** Falls back to stale cached data when the MTA API is unavailable
