# Phase 2 – Betrieb und Wiederherstellung

Der Dienst fragt die Bafang-Cloud standardmässig alle 30 Minuten ab und
importiert geprüfte Fahrten nach Endurain. Er verwendet Passwort-Login und die
beiden lesenden Fahrt-Endpunkte. Persistente Daten liegen unter `data/sync/`.

## Start

Wie in der README: `secrets/bafang_account` und `secrets/bafang_password` mit Modus 600, `.env` nach
`.env.example`, optional `BAFANG_DEVICE_ID` und passende `LOCAL_UID`/`LOCAL_GID` setzen.
Keine Zugangsdaten in `.env.example` oder Compose eintragen; Details in [LOGIN.md](LOGIN.md).

```bash
docker compose -p momas-sync --profile sync build bafang-sync
# Einmaliger Test ohne Dauerdienst:
docker compose -p momas-sync --profile sync run --rm --no-deps bafang-sync once
# Danach den separaten Dienst starten:
docker compose -p momas-sync --profile sync up -d --no-deps bafang-sync
```

Für vorhandene Docker-Installationen nur diesen Projekt- und Servicenamen
verwenden. Der Sync-Dienst öffnet keine Ports und verändert keinen anderen Stack.
`restart: unless-stopped` startet ihn nach einem Prozessabbruch wieder. Ein
unhealthy-Status allein führt in Docker nicht zu einem automatischen Neustart.

Ohne Docker, für lokale Diagnose:

```bash
export BAFANG_AUTH_MODE=login
export BAFANG_ACCOUNT_FILE="$PWD/secrets/bafang_account"
export BAFANG_PASSWORD_FILE="$PWD/secrets/bafang_password"
export BAFANG_DEVICE_ID="deine-device-id"
export DATA_DIR="$PWD/data/sync"
PYTHONPATH=src python3 -m momas_sync.service once
PYTHONPATH=src python3 -m momas_sync.service serve
```

## Daten und Synchronisierung

- `sync.sqlite3`: schema-versionierte SQLite-Datenbank (aktuell Version 2, ergänzt um Uploadstatus in Phase 3),
  mit eindeutiger Bafang-ID, Originalmetadaten, Dateihashes, Downloadversuchen,
  Zeitprüfung und Sync-Status. WAL und `synchronous=FULL` sind aktiv.
- `originals/rides/<hash-id>/<inhaltshash>.json`: versionierte Originalmetadaten.
- `originals/gpx/<hash-id>/<inhaltshash>.gpx`: unveränderte, versionierte Downloads.
- `exports/<hash-id>.gpx` und `.json`: separat geprüfte Importkopie samt Herkunft,
  Dateihashes, Zeitvergleich und angewendeter Korrektur.
- `pages/`: zuletzt abgerufene Listen-Seiten. Bei einem kürzeren oder fehlgeschlagenen
  Lauf können ältere Seitendateien zurückbleiben; SQLite ist die Statusquelle.
- `status.json`: letzter abgeschlossener Zyklus, nur Status, Zähler und Zeit des
  letzten Erfolgs. Aktueller Betriebsstatus und Heartbeat stehen in SQLite.

Alle Seiten werden bis zur ersten leeren Seite durchlaufen. Es wird nicht beim
Erreichen bekannter Fahrten abgebrochen, weil eine Sortiergarantie fehlt.
Wiederholte Seiten werden abgewiesen, Überschneidungen innerhalb eines Laufs
anhand der ID dedupliziert. `SYNC_MAX_PAGES` begrenzt den Abruf; das Erreichen der
Grenze ohne leere Seite ergibt `degraded`, keinen vollständigen Erfolg.
Die gemeldete statt angeforderte Seitengröße wird berücksichtigt, indem niemals
allein wegen einer kurzen Seite abgebrochen wird. Gleichzeitige Cloud-Änderungen
können die Pagination verschieben; der nächste vollständige Lauf gleicht nach.

Bereits vorhandene und intakte Downloads werden übersprungen. Fehlende oder
beschädigte GPX-Dateien werden erneut geladen. Fehlende/beschädigte Importkopien
werden aus dem Original neu aufgebaut. Bei geänderten Originalmetadaten wird
der Download erneut geprüft und die alte Importkopie ungültig; archivierte
Versionen bleiben erhalten. Entfernte Cloud-Fahrten werden lokal nicht gelöscht.
Dateien werden vor dem Status-Commit atomar geschrieben und mit SHA-256 geprüft.
Ein Crash kann unreferenzierte Dateien hinterlassen, aber keine absichtlich als
fertig markierte Datei, die noch nicht geschrieben wurde. Ein Dateilock verhindert
zwei aktive Writer für dasselbe Verzeichnis. Das Datenverzeichnis muss auf einem
lokalen Dateisystem mit SQLite-/Lock-Unterstützung liegen, nicht auf NFS/SMB.

## Zeitkorrektur der Importkopien

App-Zeitzone konfigurierbar, Standard: `Europe/Zurich`. Für jede Fahrt werden API-Start und
API-Ende mit dem ersten/letzten GPX-Trackpunkt verglichen:

- Beide Grenzen innerhalb ±5 Sekunden: keine Korrektur; Kopie bytegleich.
- Beide Grenzen bei +28 800 Sekunden, jeweils innerhalb ±5 Sekunden:
  Importkopie um **minus 8 Stunden** verschieben.
- Fehlende/mehrdeutige Zeiten, nicht monotone GPX-Zeitreihe, unvollständig zeitlich
  markierte Trackpunkte oder anderer Offset: `review`, keine Importkopie.

Die Originaldatei bleibt unverändert. Die wenigen Sekunden Differenz zwischen
Fahrtstatus und Trackaufzeichnung werden nicht künstlich glattgezogen. Bei einer
Korrektur wird XML neu serialisiert; Koordinaten, Werte und Extension-Strukturen
bleiben erhalten. Es wird weder geraten noch eine beliebige Stundenverschiebung
auf API-Grenzen angepasst. Bei mehrdeutigen lokalen Zeiten während der
Zeitumstellung ist ein manueller Abgleich nötig. Das UTC+8-Muster ist im bisherigen Praxistest bestätigt, nicht als universeller Bafang-Fehler bewiesen.

Ein späterer Importer muss ausschliesslich SQLite-Einträge mit `status=ready`
verwenden und deren Hash erneut prüfen. Ein Export wird bei erneuter Prüfung oder
Fehlern entfernt. Ein beliebiges altes GPX aus Phase 1 darf nicht automatisch
importiert werden.

## Fehler und Healthcheck

HTTP 408/425/429 und 5xx sowie Netzwerk-/Transportfehler werden begrenzt wiederholt.
Exponentieller Backoff mit Jitter, standardmässig vier Versuche, beginnt bei zwei
Sekunden und ist auf 60 Sekunden begrenzt. `Retry-After` ist eine zusätzliche
serverseitige Mindestwartezeit und kann diesen Backoff-Cap überschreiten.
Wartezeiten bleiben mit Heartbeat und SIGTERM-Unterbrechung reaktionsfähig.
Nicht wiederholt werden HTTP 401/403, bekannte entsprechende JSON-Fehlercodes,
sonstige 4xx und Schema-/Validierungsfehler. Unbekannte Bafang-Fehlercodes gelten
als API-Fehler; ihre genaue Bedeutung wird ohne Beleg nicht erraten.

Zyklus-Status: `ok`, `degraded`, `auth_error`, `error`; beim Betrieb zusätzlich
`starting`, `syncing`, `stopped`. Letzter erfolgreicher Zeitpunkt bleibt bei einem
Fehler erhalten. Im Login-Modus wird ein ablaufender Token durch erneute
Anmeldung ersetzt. Bei Zurückweisung einer Fahrtanfrage wird einmal erneut
angemeldet. Abgelehnte Zugangsdaten oder eine erneut abgelehnte Session ergeben
`auth_error` und sperren weitere Login-Versuche bis zu geänderten Zugangsdaten
oder `auth-reset`, siehe [LOGIN.md](LOGIN.md). Nach Secret-Wechsel den
Container neu erzeugen, damit auch ein neuer Secret-Datei-Inode gemountet wird:

```bash
docker compose -p momas-sync --profile sync up -d --no-deps --force-recreate bafang-sync
```

Secrets werden zu Beginn jedes Zyklus neu gelesen. Umgebungsänderungen benötigen
ebenfalls eine Neuerstellung des Containers. TLS-Prüfung bleibt aktiv; kein Token
wird per Redirect weitergeleitet. Keine HTTP-Debuglogs aktivieren.

```bash
docker compose -p momas-sync exec bafang-sync momas-worker status
docker compose -p momas-sync exec bafang-sync momas-worker healthcheck
docker compose -p momas-sync logs --tail 20 bafang-sync
```

Healthcheck liest SQLite ausschliesslich lesend. Gesund sind nur `ok`/`syncing`
mit frischem Heartbeat (max. 120 Sekunden oder HTTP-Timeout +30 Sekunden) und
letztem Erfolg innerhalb `HEALTH_STALE_SECONDS`, standardmässig 3900 Sekunden.
Review-Fahrten machen den Dienst absichtlich `degraded`/unhealthy, bis die
Zeitprüfung geklärt ist. Der Dienst läuft weiter und versucht beim nächsten
Zyklus erneut. Heartbeat wird auch während der 30-minütigen Pause aktualisiert.

SIGTERM/SIGINT beendet den Dienst nach dem aktuellen HTTP-Aufruf oder sofort in
Wartezeiten. Bafang-HTTP-Timeout standardmässig 30 Sekunden, Endurain-Uploadtimeout 120 Sekunden,
Compose-Stopfrist 150 Sekunden;
bei erhöhtem Timeout die Stopfrist entsprechend anpassen. Logs enthalten nur
feste Ereignis-/Fehlercodes, Zählwerte und Betriebszeitpunkte, keine GPS-Daten,
Fahrt-IDs, Tokens oder ungefilterte Exception-Texte.

## Backup und Restore

Ein geprüftes Backup muss Originaldateien, Importkopien und SQLite zusammen
enthalten. Für einen konsistenten Snapshot den **eigenen** Sync-Dienst stoppen;
der Backup-Befehl verweigert den Zugriff bei aktivem Writer. Die SQLite-Backup-API
wird verwendet, nicht eine unkoordinierte Kopie der WAL-Dateien.

```bash
docker compose -p momas-sync stop bafang-sync
docker compose -p momas-sync --profile sync run --rm --no-deps bafang-sync backup /data/backups/snapshot-01
docker compose -p momas-sync --profile sync run --rm --no-deps bafang-sync verify-backup /data/backups/snapshot-01
docker compose -p momas-sync --profile sync up -d --no-deps bafang-sync
```

Das Ziel muss neu und ausserhalb von `data/sync/` liegen. Das Backup wird erst nach
Prüfung atomar sichtbar. `manifest.json` enthält SHA-256 für alle gesicherten
Dateien. `verify-backup` prüft Dateien, SQLite-Integrität und alle DB-Dateireferenzen.
Snapshots anschliessend verschlüsselt auf getrennten Speicher übertragen; die
lokale Kopie im selben Laufwerk ist noch kein Schutz vor Plattenausfall. Secrets
separat sichern. Backup ist derzeit ein manueller Befehl, keine eingerichtete
geplante Sicherung.

Wiederherstellung auf einem leeren Testverzeichnis:

```bash
cp -a data/backups/snapshot-01 data/restore-test
DATA_DIR="$PWD/data/restore-test" PYTHONPATH=src python3 -m momas_sync.service verify-backup "$PWD/data/restore-test"
# Mit den Login-Secrets aus obiger Konfiguration; Test ohne Uploads:
ENDURAIN_ENABLED=false DATA_DIR="$PWD/data/restore-test" PYTHONPATH=src python3 -m momas_sync.service once
```

Der Testlauf muss vorhandene intakte Downloads überspringen. Für eine echte
Wiederherstellung den Dienst stoppen, das bisherige `data/sync/` separat sichern
und das geprüfte Snapshot-Verzeichnis als neues `data/sync/` verwenden; Besitzrechte
beibehalten. Keine Test- und Produktionswriter auf dasselbe Verzeichnis richten.

## Update

Vor einem Update Backup anlegen. Dann nur den eigenen Dienst neu bauen und
neu erstellen:

```bash
docker compose -p momas-sync --profile sync build bafang-sync
docker compose -p momas-sync --profile sync up -d --no-deps bafang-sync
```

SQLite-Schemaversionen werden geprüft; eine unbekannte neuere Version wird nicht
heruntergestuft. Bei späteren Migrationen sind Rollbacks mit passender Datenbank-
Sicherung erforderlich. Endurain ist ein unabhängiger Stack, siehe
[offizielle Endurain-Dokumentation](https://docs.endurain.com/).
