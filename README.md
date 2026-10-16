# Momas Sync → Endurain

A self-hosted Python service that archives rides from **Momas Red / Bafang Cloud**
and automatically imports them into **Endurain**. Supports direct email/password
login, Docker Compose, and Komodo. No Strava dependency.

- Fetches rides every 30 minutes, with pagination and retries with backoff.
- Stores original JSON and GPX locally, with separate, validated TCX import files.
- Uses SQLite for recovery, metadata, and upload status per ride and destination.
- Preserves power readings, including 0 W, and cadence in TCX.
- Includes a private token cache, health checks, and consistent backups.
- Requires no inbound ports; Endurain runs as a separate application.

An independent community project, not affiliated with Momas, Bafang, or Endurain.
The undocumented cloud API may change. Tested with Momas Red Android 1.0.1 and
Endurain **v0.19.3**; test imports yourself when using other versions.

## Quick start with Docker Compose

Requirements: Docker with Compose v2, a Momas account with an email and password,
and a reachable Endurain instance. Google and Apple login are not implemented.

1. Clone this repository and enter the project directory.
2. Create the private directories and configuration:

   ```bash
   cp .env.example .env
   mkdir -p data secrets
   chmod 700 data secrets
   ```

3. Create three files using a local editor or secret manager:

   | File | Contents |
   | --- | --- |
   | `secrets/bafang_account` | Momas Red email address |
   | `secrets/bafang_password` | Momas Red password |
   | `secrets/endurain_api_key` | Endurain API key with `activities:upload` permission |

   ```bash
   chmod 600 secrets/bafang_account secrets/bafang_password secrets/endurain_api_key
   ```

   To try local archiving without Endurain first, create an empty
   `secrets/endurain_api_key` file. Do not put credentials in Compose, Git, issues,
   or shell commands. Secret files avoid interpolation of `$` and other special
   characters.

4. In `.env`, set `LOCAL_UID` and `LOCAL_GID` to the output of `id -u` and `id -g`.
   This user must be able to read the secrets and write to `data/`.
   Set `APP_TIMEZONE` to the timezone used by the app, such as `Europe/Zurich`.
   Set `ENDURAIN_URL` to the reachable base URL, without `/settings/security`.
   Inside a container, `localhost` refers to that container itself.
   Leave `BAFANG_DEVICE_ID` empty to generate a persistent device identifier.

5. Build the image, verify login, and test local archiving.
   **Uploads remain disabled during these checks:**

   ```bash
   docker compose -p momas-sync --profile sync build bafang-sync
   docker compose -p momas-sync --profile sync run --rm --no-deps bafang-sync login-test
   docker compose -p momas-sync --profile sync run --rm --no-deps \
     -e ENDURAIN_ENABLED=false bafang-sync once
   ```

   `login_verified` confirms successful authentication. The sync should report
   `status: ok`. If it reports `review` or `degraded`, resolve timezone and
   timestamp validation issues first. Before importing, inspect a file in
   `data/sync/exports/`; originals are stored separately in `data/sync/originals/`.

6. Set `ENDURAIN_ENABLED=true` in `.env` and start automatic imports:

   ```bash
   docker compose -p momas-sync --profile sync up -d --no-deps bafang-sync
   docker compose -p momas-sync exec -T bafang-sync momas-worker status
   docker compose -p momas-sync logs --tail 20 bafang-sync
   ```

   The first enabled run imports **all validated rides that have not yet been
   uploaded by this service**. Compare the results in Endurain with the originals,
   especially start time, distance, power, and cadence. The upload key cannot
   automatically detect activities previously imported outside this service.

## Timestamps and import limitations

GPX timestamps may differ from those shown in the app. The service compares
**both** the start and end against the original Bafang timestamps. Matching
boundaries allow an unchanged import; the observed eight-hour offset is corrected
in a separate copy only when both boundaries confirm it. Other discrepancies or
ambiguous timestamps require review. Originals are never overwritten.
See [operations](PHASE2.md) for details.

Endurain v0.19.3 skips 0 W samples in its GPX importer, so automatic imports use
TCX. The service validates the API response summary, but the upload key does not
allow a full download of the stored server-side samples. In this Endurain version,
speed may be missing from the overview while still appearing in activity details.
See [Endurain imports](PHASE3.md) for details.

Uploads with uncertain outcomes after a connection failure are not automatically
retried. This prevents duplicate activities; these cases require a manual check
in Endurain.

## Operations and documentation

The detailed guides below are currently in German:

- [Login, token expiry, and resetting authentication](LOGIN.md)
- [Configuration, health checks, backups, restore, and updates](PHASE2.md)
- [Endurain imports, TCX, and duplicate prevention](PHASE3.md)
- [Installing or migrating a Komodo stack](deploy/komodo/README.md)
- [Privacy and reporting security issues](SECURITY.md)
- [Development and publishing](CONTRIBUTING.md)

Install Endurain separately using its official documentation:
<https://docs.endurain.com/>. No changes to Endurain source code are required.

## Development

Requires Python 3.11 or newer, with no external Python runtime dependencies.
Minimal systems must have the IANA timezone database installed.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
```

All test data is synthetic. CI checks Python 3.11–3.13, the Docker build, and
Compose with a test configuration. Licensed under the [MIT License](LICENSE).
