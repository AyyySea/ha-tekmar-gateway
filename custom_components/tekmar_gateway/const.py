"""Constants for the Tekmar Gateway 486 integration."""

from __future__ import annotations

DOMAIN = "tekmar_gateway"

# ---- Azure B2C / Watts Tekmar auth ----
# These IDs are public — they appear in every OAuth /authorize URL the
# tekmar web app issues. They are not secrets.
B2C_AUTHORITY = "https://wattsb2cap02.b2clogin.com"
B2C_TENANT_PATH = "/wattsb2cap02.onmicrosoft.com"
B2C_POLICY = "B2C_1A_UnifiedSignUpOrSignIn"
B2C_CLIENT_ID = "d465d870-7af9-4bcc-9783-7b6d15fbdc07"
B2C_REDIRECT = "https://gateway.tekmarcontrols.com/auth.html"
B2C_SCOPE = (
    "openid offline_access "
    "https://wattsb2cap02.onmicrosoft.com/bcve486api/manage"
)

# ---- Gateway REST API ----
GATEWAY_BASE = "https://gateway.tekmarcontrols.com"

# ---- Polling ----
# Tokens expire in 15 min; we treat them as expired 60 s earlier as a buffer.
TOKEN_EXPIRY_BUFFER = 60
# Default poll interval - the gateway itself polls thermostats over the
# tekmarNet bus, so anything faster than ~30 s is wasted.
DEFAULT_SCAN_INTERVAL = 60

# ---- Config entry keys ----
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_BUILDING_ID = "building_id"
CONF_SCAN_INTERVAL = "scan_interval"

# ---- Tekmar mode → API string ----
# These are the strings the API accepts as PUT body to /settings/mode.
MODE_OFF = "off"
MODE_HEAT = "heat"
MODE_COOL = "cool"
MODE_AUTO = "auto"
MODE_EMERGENCY = "emergency"

# ---- Setpoint limits (used as fallbacks when the API doesn't expose them) ----
# Real limits come from the /devices/{addr}?filter=curr response's
# settings.heat.min/max and settings.cool.min/max. These are last-resort
# defaults if a thermostat is in a mode that hides those keys.
HEAT_MIN_DEFAULT = 40
HEAT_MAX_DEFAULT = 85
COOL_MIN_DEFAULT = 50
COOL_MAX_DEFAULT = 100
