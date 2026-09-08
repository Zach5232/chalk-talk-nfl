# Keyed by team ABBREVIATION (matches games.csv's home_team column, e.g. "SEA", "BUF") --
# NOT full team names. This dict previously only had 3 of 32 teams and was keyed by full
# name ("Buffalo Bills"), which meant pull_weather_for_week()'s STADIUMS.get(home) lookup
# was silently failing for every team except by accident. Filled out to all 32 teams ahead
# of the real 2026 season kickoff (2026-09-09).
#
# roof: "dome" (fully enclosed, weather is not a factor -- pull_weather_for_week skips
# these entirely), "retractable" (usually closed but can vary -- still worth pulling),
# "outdoor" (real weather always matters).
STADIUMS = {
    "ARI": {"lat": 33.5276, "lon": -112.2626, "roof": "retractable"},   # State Farm Stadium
    "ATL": {"lat": 33.7554, "lon": -84.4008,  "roof": "retractable"},   # Mercedes-Benz Stadium
    "BAL": {"lat": 39.2780, "lon": -76.6227,  "roof": "outdoor"},       # M&T Bank Stadium
    "BUF": {"lat": 42.7738, "lon": -78.7870,  "roof": "outdoor"},       # Highmark Stadium
    "CAR": {"lat": 35.2258, "lon": -80.8528,  "roof": "outdoor"},       # Bank of America Stadium
    "CHI": {"lat": 41.8623, "lon": -87.6167,  "roof": "outdoor"},       # Soldier Field
    "CIN": {"lat": 39.0955, "lon": -84.5161,  "roof": "outdoor"},       # Paycor Stadium
    "CLE": {"lat": 41.5061, "lon": -81.6995,  "roof": "outdoor"},       # Huntington Bank Field
    "DAL": {"lat": 32.7473, "lon": -97.0945,  "roof": "retractable"},   # AT&T Stadium
    "DEN": {"lat": 39.7439, "lon": -105.0201, "roof": "outdoor"},       # Empower Field at Mile High
    "DET": {"lat": 42.3400, "lon": -83.0456,  "roof": "dome"},          # Ford Field
    "GB":  {"lat": 44.5013, "lon": -88.0622,  "roof": "outdoor"},       # Lambeau Field
    "HOU": {"lat": 29.6847, "lon": -95.4107,  "roof": "retractable"},   # NRG Stadium
    "IND": {"lat": 39.7601, "lon": -86.1639,  "roof": "retractable"},   # Lucas Oil Stadium
    "JAX": {"lat": 30.3239, "lon": -81.6373,  "roof": "outdoor"},       # EverBank Stadium
    "KC":  {"lat": 39.0489, "lon": -94.4839,  "roof": "outdoor"},       # GEHA Field at Arrowhead
    "LV":  {"lat": 36.0909, "lon": -115.1833, "roof": "dome"},          # Allegiant Stadium
    "LAC": {"lat": 33.9535, "lon": -118.3392, "roof": "dome"},          # SoFi Stadium (fixed roof)
    "LA":  {"lat": 33.9535, "lon": -118.3392, "roof": "dome"},          # SoFi Stadium (fixed roof)
    "MIA": {"lat": 25.9580, "lon": -80.2389,  "roof": "outdoor"},       # Hard Rock Stadium
    "MIN": {"lat": 44.9737, "lon": -93.2581,  "roof": "dome"},          # U.S. Bank Stadium
    "NE":  {"lat": 42.0909, "lon": -71.2643,  "roof": "outdoor"},       # Gillette Stadium
    "NO":  {"lat": 29.9511, "lon": -90.0812,  "roof": "dome"},          # Caesars Superdome
    "NYG": {"lat": 40.8135, "lon": -74.0745,  "roof": "outdoor"},       # MetLife Stadium
    "NYJ": {"lat": 40.8135, "lon": -74.0745,  "roof": "outdoor"},       # MetLife Stadium
    "PHI": {"lat": 39.9008, "lon": -75.1675,  "roof": "outdoor"},       # Lincoln Financial Field
    "PIT": {"lat": 40.4468, "lon": -80.0158,  "roof": "outdoor"},       # Acrisure Stadium
    "SEA": {"lat": 47.5952, "lon": -122.3316, "roof": "outdoor"},       # Lumen Field
    "SF":  {"lat": 37.4030, "lon": -121.9700, "roof": "outdoor"},       # Levi's Stadium
    "TB":  {"lat": 27.9759, "lon": -82.5033,  "roof": "outdoor"},       # Raymond James Stadium
    "TEN": {"lat": 36.1665, "lon": -86.7713,  "roof": "outdoor"},       # Nissan Stadium
    "WAS": {"lat": 38.9076, "lon": -76.8645,  "roof": "outdoor"},       # Northwest Stadium
}
