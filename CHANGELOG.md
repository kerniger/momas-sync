# Changelog

## 0.4.0

- Direkter Momas-Red-Login mit E-Mail und Passwort.
- Privater persistenter Token-Cache, erneute Anmeldung bei Ablauf und begrenzte
  Wiederanmeldung nach Zurückweisung; Schutz vor wiederholten Fehlanmeldungen.
- Generierte persistente Gerätekennung, alternativ konfigurierbare userDeviceId.
- Docker Compose und Komodo mit Login-Secrets.

Enthaltene Grundlagen: lesender Cloud-Abruf mit Pagination, lokales JSON-/GPX-
Archiv, SQLite-Status, Zeitprüfung, TCX-Import über Endurains Upload-API,
Duplikatschutz, Retry/Backoff, Healthcheck und Backup/Restore.

Veröffentlichungsvorbereitung: allgemeine Dokumentation, MIT-Lizenz, GitHub-CI
und geprüfter Quelltextexport; persönliche Betriebsberichte bleiben privat.
