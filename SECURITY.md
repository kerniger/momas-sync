# Sicherheit und Datenschutz

Der Dienst verarbeitet Zugangsdaten und Standortverläufe. Secrets, `.env`,
Session-Cache, Originaldaten und Backups gehören ausschliesslich in die private
Installation. Sie werden weder in Git aufgenommen noch in das Docker-Image kopiert.
Die Beispieldaten in `tests/fixtures/` sind synthetisch.

## Betrieb

- Docker-Secrets oder Umgebungsvariablen verwenden; Dateirechte wie im Schnellstart.
- TLS-Prüfung bleibt für die Cloud und HTTPS-Endurain aktiv. Redirects werden abgewiesen.
- HTTP zu Endurain nur auf einem vertrauenswürdigen privaten Netz konfigurieren.
- Keine HTTP-Debuglogs aktivieren. Auch Passwort-MD5 und Token-Cache sind vertraulich.
- Daten und Backups verschlüsselt auf zugriffsbeschränktem Speicher sichern.
- Nur einen Sync pro Konto/Archiv betreiben und Uploadstatus bei Migration erhalten.

## Sicherheitsprobleme melden

Keine Zugangsdaten, originalen Fahrten, GPS-Koordinaten oder vollständigen
HTTP-Mitschnitte in öffentliche Issues oder Pull Requests aufnehmen.
Nach Veröffentlichung bevorzugt GitHubs private Meldung unter **Security →
Report a vulnerability** verwenden, sofern sie im Repository aktiviert ist.
Falls sie fehlt, in einem öffentlichen Issue lediglich um einen privaten
Kontaktweg bitten, ohne Details zur Schwachstelle oder private Daten anzugeben.

Es gibt derzeit keinen zugesicherten Supportzeitraum für ältere Versionen.
