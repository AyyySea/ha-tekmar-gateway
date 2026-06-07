# ha-tekmar-gateway

Unofficial Home Assistant integration for the Watts / Tekmar **Gateway 486**, exposing tekmarNet thermostats as HA `climate` entities (plus outdoor-temperature, humidity, and boiler sensors) through the manufacturer's cloud API.

> **Why this exists.** The older Gateway 482 speaks a documented serial protocol and has an excellent local HA integration ([WillCodeForCats/tekmar-482](https://github.com/WillCodeForCats/tekmar-482)). The newer 486 dropped that path entirely in favor of cloud-only access through the Watts Home service. This integration restores Home Assistant control by replaying the same cloud calls the official web app makes.

## Compatibility

- **Gateway**: Tekmar Gateway 486 (cloud-connected). Not the 482 or 484 — use [tekmar-482](https://github.com/WillCodeForCats/tekmar-482) for those.
- **Thermostats tested**: 552 (heat-only), 557 (heat/cool/auto + humidity). Other tekmarNet models reporting through the same API should work — capabilities are discovered per-device from the API's `settings.mode.enum`, not hardcoded by model.
- **Home Assistant**: 2024.12 or newer (the reauth flow uses config-entry helpers added in 2024.12).

## What you get

- One `climate` entity per thermostat: current temperature, setpoint(s), HVAC mode, action (heating/cooling/idle), emergency-heat preset where supported.
- Outdoor temperature sensor (from the gateway's outdoor probe).
- Humidity sensors for thermostats that report one.
- Per-boiler diagnostics: outlet/inlet temperature, output %, runtime hours, status.
- Token refresh handled automatically (tokens expire every 15 minutes).
- Reauth flow if the account password changes.
- Multi-building accounts supported (a chooser appears during setup).

## Installation

### HACS

1. HACS → ⋯ menu → **Custom repositories** → add this repo's URL, category **Integration**.
2. Install "Tekmar Gateway 486" and restart Home Assistant.
3. Settings → Devices & services → **Add Integration** → Tekmar Gateway 486.

### Manual

1. Copy `custom_components/tekmar_gateway/` into your HA config's `custom_components/` directory.
2. Restart Home Assistant.
3. Settings → Devices & services → **Add Integration** → Tekmar Gateway 486.

## Configuration

You'll be asked for an email and password.

**Recommended — create a dedicated user.** In the Tekmar web app at `gateway.tekmarcontrols.com`, go to Users → Invite and invite a separate email (a plus-alias works). Sign up via the invite link with a strong unique password and use those credentials here. This keeps the integration's credentials revocable independently of your personal account.

**Or use your main account.** Works fine, just less hygienic — that password ends up in HA's storage.

The poll interval defaults to 60 seconds and is adjustable under the integration's options. Anything faster than ~30 s is wasted: the gateway itself only polls the tekmarNet bus on roughly that cadence.

## How the auth works (for the curious)

Watts uses Azure AD B2C with a custom signup-or-signin policy. The integration:

1. Hits the B2C `/authorize` endpoint to start a session.
2. Parses the login page's embedded `SETTINGS` object for a CSRF token and transaction ID.
3. POSTs credentials to `/SelfAsserted`.
4. GETs `/api/CombinedSigninAndSignup/confirmed` and reads the 15-minute access token out of the redirect URL fragment.
5. Uses that token as a `jwt` cookie on `gateway.tekmarcontrols.com` API calls.

Tokens are cached in memory and re-acquired when within ~60 s of expiry. The sign-in leg runs through `requests` in an executor rather than aiohttp — the Cloudflare layer in front of B2C rejects aiohttp's requests outright (likely TLS-fingerprint based), and sign-in only happens every ~15 minutes so the executor overhead is irrelevant. The client IDs and policy names in `const.py` are public values that appear in every authorize URL the official web app issues; they are not secrets.

## Known limitations

- **Cloud-dependent.** If your internet, the Watts service, or the gateway's own connection goes down, the integration goes offline. The 486 has no local API — local control requires a Gateway 482.
- **Polling, not push.** State changes made at the physical thermostat appear within one poll interval.
- **No schedule editing.** Setpoints and modes only; schedules stay in the Watts app.
- **Fahrenheit only.** The gateway API reports °F in the installations observed; °C accounts are untested. Open an issue if yours differs.
- **Unofficial.** The API is undocumented and reverse-engineered; a vendor-side change could break it. The vendor ships roughly one batched update per year, so breakage should be infrequent — but expect bumps and please open an issue if you hit one.

## Acknowledgments

This project was developed with extensive assistance from Anthropic's Claude.

## License

MIT — see [LICENSE](LICENSE).
