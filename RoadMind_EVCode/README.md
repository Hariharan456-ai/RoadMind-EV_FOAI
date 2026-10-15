# RoadMind EV - Smart EV Trip Planner

## Run (3 steps, nothing to install)
1. Install Python 3.8+ (https://python.org) - tick "Add to PATH".
2. Open a terminal in this folder and run:   python app.py
3. Browser opens at http://localhost:8000 -> enter From, To, start time, battery details -> "Plan my trip".

Needs internet (live maps/restaurants). Free services used, no API keys: OSRM, Nominatim, Overpass (OpenStreetMap).
Optional better ratings: set GOOGLE_API_KEY (Windows: set GOOGLE_API_KEY=xxxx) before running.
Different port: set PORT=8080
