# Momas Sync → Endurain

Selbst gehosteter Python-Dienst, der Fahrten aus **Momas Red / Bafang Cloud**
archiviert und automatisch nach **Endurain** importiert. Direkter Login mit
E-Mail und Passwort, Docker Compose und Komodo; keine Strava-Abhängigkeit.

- Abruf alle 30 Minuten, mit Pagination und Retry/Backoff.
- Original-JSON und GPX lokal; separate, geprüfte Importkopien als TCX.
- SQLite für Wiederaufnahme, Metadaten und Uploadstatus pro Fahrt und Ziel.
- Leistung einschliesslich 0 W und Trittfrequenz bleiben in TCX erhalten.
- Privater Token-Cache, Healthcheck, konsistente Backups.
- Kein eingehender Port erforderlich; Endurain läuft als separate Anwendung.

Unabhängiges Community-Projekt ohne Verbindung zu Momas, Bafang oder Endurain.
Die undokumentierte Cloud-API kann sich ändern. Geprüft wurden Momas Red Android
1.0.1 und Endurain **v0.19.3**; andere Versionen benötigen einen eigenen Importtest.

## Schnellstart mit Docker Compose

Voraussetzungen: Docker mit Compose v2, ein Momas-Konto mit E-Mail/Passwort und
eine erreichbare Endurain-Instanz. Google-/Apple-Login ist nicht implementiert.

1. Repository klonen und in das Projektverzeichnis wechseln.
2. Private Verzeichnisse und Konfiguration anlegen:

   ```bash
   cp .env.example .env
   mkdir -p data secrets
   chmod 700 data secrets
   ```

3. Drei Dateien mit einem lokalen Editor oder Secret-Manager anlegen:

   | Datei | Inhalt |
   | --- | --- |
   | `secrets/bafang_account` | Momas-Red-E-Mail |
   | `secrets/bafang_password` | Momas-Red-Passwort |
   | `secrets/endurain_api_key` | Endurain-Key mit `activities:upload` |

   ```bash
   chmod 600 secrets/bafang_account secrets/bafang_password secrets/endurain_api_key
   ```

   Ohne Endurain zunächst eine leere `secrets/endurain_api_key` anlegen.
   Zugangsdaten nicht in Compose, Git, Tickets oder Shell-Befehle schreiben.
   Secret-Dateien vermeiden Probleme mit `$` und anderen Sonderzeichen.

4. In `.env` `LOCAL_UID`/`LOCAL_GID` auf die Ausgabe von `id -u`/`id -g` setzen.
   Dieser Benutzer muss die Secrets lesen und `data/` schreiben können.
   `APP_TIMEZONE` auf die Zeitzone der App einstellen, beispielsweise
   `Europe/Zurich`. `ENDURAIN_URL` ist die erreichbare Basis-URL ohne
   `/settings/security`. `localhost` im Container bezeichnet den Container selbst.
   `BAFANG_DEVICE_ID` kann leer bleiben; der Dienst erzeugt eine stabile Kennung.

5. Bauen, Login und lokalen Abruf prüfen. **Uploads bleiben dabei deaktiviert:**

   ```bash
   docker compose -p momas-sync --profile sync build bafang-sync
   docker compose -p momas-sync --profile sync run --rm --no-deps bafang-sync login-test
   docker compose -p momas-sync --profile sync run --rm --no-deps \
     -e ENDURAIN_ENABLED=false bafang-sync once
   ```

   `login_verified` bestätigt die Anmeldung. Der Sync muss `status: ok` melden.
   Bei `review`/`degraded` zuerst Zeitzone und Zeitprüfung klären.
   Vor dem Import eine Datei aus `data/sync/exports/` prüfen; Originale liegen
   separat unter `data/sync/originals/`.

6. Für den Endurain-Import `ENDURAIN_ENABLED=true` in `.env` setzen und starten:

   ```bash
   docker compose -p momas-sync --profile sync up -d --no-deps bafang-sync
   docker compose -p momas-sync exec -T bafang-sync momas-worker status
   docker compose -p momas-sync logs --tail 20 bafang-sync
   ```

   Der erste aktivierte Lauf importiert **alle noch nicht übertragenen, geprüften
   Fahrten**. Danach Endurain mit den Originaldaten vergleichen, insbesondere
   Startzeit, Distanz, Leistung und Kadenz. Bestehende, ausserhalb dieses Dienstes
   importierte Aktivitäten kann der Upload-Key nicht automatisch erkennen.

## Zeitstempel und Importgrenzen

GPX-Zeiten können von der App abweichen. Der Dienst vergleicht Anfang **und** Ende
mit den Bafang-Originalzeiten. Nur bei übereinstimmenden Grenzen wird unverändert
importiert; das beobachtete Muster von exakt acht Stunden Verschiebung wird in
einer separaten Kopie korrigiert. Andere oder mehrdeutige Zeitdaten benötigen
Prüfung. Die Originale werden nie überschrieben. Details: [Betrieb](PHASE2.md).

Endurain v0.19.3 ignoriert im GPX-Importer 0-W-Messpunkte; deshalb verwendet der
Automatikbetrieb TCX. Die API-Zusammenfassung wird geprüft, ein vollständiger
Rückdownload der serverseitigen Messpunkte ist mit dem Upload-Key nicht möglich.
In dieser Endurain-Version kann Geschwindigkeit in der Übersicht fehlen,
obwohl sie in den Aktivitätsdetails angezeigt wird. Details: [Import](PHASE3.md).

Unklare Uploads nach Verbindungsabbruch werden nicht automatisch wiederholt.
Das verhindert doppelte Aktivitäten; in solchen Fällen ist ein manueller
Abgleich mit Endurain erforderlich.

## Betrieb und Dokumentation

- [Login, Tokenablauf und Zurücksetzen der Anmeldung](LOGIN.md)
- [Konfiguration, Healthcheck, Backup, Restore und Updates](PHASE2.md)
- [Endurain-Import, TCX und Duplikatschutz](PHASE3.md)
- [Komodo-Stack installieren oder migrieren](deploy/komodo/README.md)
- [Datenschutz und Sicherheitsmeldungen](SECURITY.md)
- [Entwicklung und Veröffentlichung](CONTRIBUTING.md)

Endurain wird separat über dessen offizielle Installationsanleitung betrieben:
<https://docs.endurain.com/>. Der Sync benötigt keine Änderungen am Endurain-Code.

## Entwicklung

Python 3.11 oder neuer, keine externen Python-Laufzeitabhängigkeiten.
Auf minimalen Systemen muss die IANA-Zeitzonendatenbank installiert sein.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
```

Alle Testdaten sind synthetisch. CI prüft Python 3.11–3.13, den Docker-Build und
Compose mit Testkonfiguration. Die MIT-Lizenz steht in [LICENSE](LICENSE).
