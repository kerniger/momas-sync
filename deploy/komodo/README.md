# Komodo-Stack

Die [Compose-Datei](compose.yaml) baut das Image lokal. Ein vorab publiziertes
Container-Image wird nicht vorausgesetzt. Endurain bleibt ein eigener Stack.

## Verzeichnis auf dem Docker-Host

Standard: `/etc/komodo/stacks/momas-sync`. Über `MOMAS_ROOT` anpassbar. Prüfen,
welcher Hostpfad im Komodo-Periphery-Container sichtbar ist.

```text
momas-sync/
  compose.yaml       # aus deploy/komodo/compose.yaml
  app/               # ausgechecktes Repository mit Dockerfile und .dockerignore
  secrets/
    bafang_account
    bafang_password
    endurain_api_key
  data/
```

Dateirechte und Zugangsdaten wie im [Schnellstart](../../README.md) vorbereiten.
Der Build-Kontext ist nur `app/`; dessen `.dockerignore` immer mit übernehmen.
In Komodos Stack-Environment setzen:

- `LOCAL_UID` und `LOCAL_GID`: passend zu Daten- und Secret-Dateien.
- `APP_TIMEZONE`: App-Zeitzone, Standard `Europe/Zurich`.
- `ENDURAIN_URL`: eigene erreichbare Endurain-Basis-URL.
- `ENDURAIN_ENABLED=false`: bis zum abgeschlossenen Abruf-/Importtest.
- `BAFANG_DEVICE_ID`: optional; für Migration die bisherige Kennung übernehmen.
- `MOMAS_ROOT`: nur bei abweichendem Verzeichnis.

Komodo: Stackname `momas-sync`, eigener Docker-Server, **Files on Host**, Run
Directory auf das Root-Verzeichnis, Compose File `compose.yaml`, **Run Build**
aktivieren, **Auto Pull** deaktivieren. Kein Port-Mapping erforderlich.

## Test und Start

Im Root-Verzeichnis mit derselben Umgebung wie im Komodo-Stack:

```bash
docker compose build momas-sync
docker compose run --rm --no-deps -e DATA_DIR=/data/login-check momas-sync login-test
docker compose run --rm --no-deps -e ENDURAIN_ENABLED=false momas-sync once
```

Nach Prüfung der vorbereiteten Exporte `ENDURAIN_ENABLED=true` setzen und über
Komodo deployen. Alle noch nicht übertragenen, geprüften Fahrten werden importiert.
Healthcheck, Status und Endurain prüfen.

## Migration einer bestehenden Installation

1. Ziel vorbereiten und bauen; dort noch keinen Dauerdienst starten.
2. Nur den bisherigen Sync stoppen und einen aktuellen, geprüften Snapshot
   erstellen, siehe [Backup](../../PHASE2.md).
3. Diesen Snapshot als `data/sync/` auf dem Ziel ablegen. SQLite-Uploadstatus,
   `deliveries/` und Session-Cache müssen enthalten sein. Keine laufende
   SQLite-Datenbank samt WAL unkoordiniert kopieren. Secrets privat übertragen.
4. Dieselbe Endurain-Ziel-URL und dieselbe Gerätekennung verwenden. Uploadstatus
   ist nach Ziel-URL und Bafang-ID zugeordnet; ein URL-Wechsel kann Duplikate erzeugen.
5. Ziel starten. Bereits hochgeladene Fahrten müssen übersprungen werden.

Nur einen aktiven Sync betreiben; der Writer-Lock wirkt ausschliesslich auf das
lokale Datenverzeichnis. Nach neuen Uploads auf dem Ziel darf ein Rückwechsel
nicht mit dem veralteten Uploadstatus des Quellhosts erfolgen.
