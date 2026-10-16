# Direkter Momas-Red-Login

Ab Version 0.4.0 meldet sich der Dienst mit vorhandener Momas-Red-E-Mail und
Passwort an. Login, lesender Fahrtzugriff, GPX-Download und Wiederaufnahme nach
Container-Neustart wurden mit einem echten Konto geprüft. Konten werden nicht
registriert und Cloud-Fahrten nicht geändert. Der genaue natürliche Ablauf
von Tokens wird zusätzlich mit synthetischen Tests geprüft.

## Konfiguration

- `BAFANG_AUTH_MODE=login`: Passwort-Login, niemals automatischer Rückfall auf einen Token.
- `BAFANG_ACCOUNT_FILE`: Datei mit der E-Mail; alternativ `BAFANG_ACCOUNT`.
- `BAFANG_PASSWORD_FILE`: Datei mit dem Passwort; alternativ `BAFANG_PASSWORD`.
- `BAFANG_DEVICE_ID`: optional eine vorhandene `userDeviceId`.
  Ohne Angabe wird eine zufällige 16-stellige Gerätekennung erzeugt und privat
  unter `DATA_DIR/auth/device_id` gespeichert. Sie bleibt bei Neustarts erhalten.

Die Compose-Dateien verwenden `secrets/bafang_account` und
`secrets/bafang_password`. Dateirechte 600, Verzeichnisrechte 700 und zum
Container passende UID/GID verwenden. Passwort-Leerzeichen und `$` bleiben in
Secret-Dateien erhalten; ein abschliessender Zeilenumbruch wird entfernt.
Keine Shell-Expansion oder Compose-Interpolation des Passworts erforderlich.

Für lokale Diagnose ohne Docker:

```bash
export BAFANG_AUTH_MODE=login
export BAFANG_ACCOUNT_FILE="$PWD/secrets/bafang_account"
export BAFANG_PASSWORD_FILE="$PWD/secrets/bafang_password"
# Optional für vorhandene Installationen: BAFANG_DEVICE_ID setzen
export DATA_DIR="$PWD/data/login-check"
PYTHONPATH=src python3 -m momas_sync.service login-test
```

Docker-Test nach dem Build, mit eigenem Cache und ohne Aktivitätsuploads:

```bash
docker compose -p momas-sync --profile sync build bafang-sync
docker compose -p momas-sync --profile sync run --rm --no-deps \
  -e DATA_DIR=/data/login-check bafang-sync login-test
```

Exit 0 und `login_verified` bedeuten Login plus erfolgreichen lesenden
API-Zugriff. Die Ausgabe enthält nur Ereignis und Fahrtanzahl. Der Cache wird
auch beim Test angelegt und bei weiteren Tests verwendet. Für eine frische
Anmeldung zuerst mit demselben DATA_DIR `auth-reset` ausführen.

## Verfahren und Belege

Die öffentlich verfügbare Android-App `com.momasbikes.ebike` Version 1.0.1
enthält im bestehenden Login-Bildschirm:

- `Service.login`: Form-POST `/BaFangCyclingApi/login/10001`.
- `MomasLogin`-Button: `account` sowie lowercase MD5 des UTF-8-Passworts.
- `AppResponse.Login`: `data.token` und `data.expiredTimeStamp`.
- `TokenHeaderRequestInterceptor`: die bekannten AppClient-/userDeviceId-Header.

MD5 ist hier eine Vorgabe des Cloud-Protokolls. Es ersetzt keine TLS-Prüfung.
Der Passwort-Hash ist ebenfalls vertraulich und wird weder gespeichert noch
geloggt. Der Dienst sendet keine Spracheinstellungen oder Profiländerungen.

APKPure-Paketquelle:
<https://apkpure.net/momas/com.momasbikes.ebike/download>.
SHA256 des untersuchten Basis-APK:
`fa57b0581742e68dab88fc3a66fd26759b3e8d596ada0e605bfbd94cacd5ed99`.
APK und dekompilierter Drittanbieter-Code liegen ausserhalb des Projekts und
gehören nicht zur Veröffentlichung.

Die App enthält zusätzlich einen kombinierten Registrierungs-/Login-Ablauf mit
Bildcode unter `login/v2/10010`. Dieser wird nicht verwendet; der bestehende
Passwort-Endpunkt funktioniert mit dem live geprüften Konto. Google-/Apple-Login,
Neuregistrierung und Konten ohne Momas-Passwort sind nicht unterstützt.

## Token und Fehler

`DATA_DIR/auth/session.json` enthält Token, Ablaufzeit und Session-Verwaltung,
atomar geschrieben mit Modus 600. E-Mail, Passwort, Passwort-MD5 und der
User-Block aus der Login-Antwort werden nicht archiviert. Eine mit zufälligem
Schlüssel berechnete HMAC bindet den Cache an die konfigurierten Zugangsdaten
und die Gerätekennung. Bei Secret-Wechsel wird der alte Token nicht verwendet.

Der Cache wird über Container-Neustarts hinweg benutzt. Ablaufzeiten in Epoch-
Millisekunden oder -Sekunden werden normalisiert. Vor Ablauf (60 Sekunden
Reserve) erfolgt eine neue Anmeldung. Es wurde kein separater Refresh-Endpunkt
nachgewiesen; der Dienst verwendet erneut den bestätigten Passwort-Login.

Bei HTTP/API 401 oder 403 erfolgt höchstens eine erneute Anmeldung je Zyklus
und eine Wiederholung derselben lesenden Fahrtanfrage. Wird auch die neue
Session abgelehnt, werden weitere Login-Versuche dauerhaft gesperrt.
Login-Code 202 sowie HTTP/API 401/403 sperren abgelehnte Zugangsdaten ebenfalls.
Netzwerkfehler, Rate-Limits und temporäre Serverfehler nutzen den bestehenden
begrenzten Backoff. Fehlerschemata und Servertexte werden nicht geloggt.

Nach Behebung eines Account-Problems entweder Secrets ändern und den Container
neu erstellen oder den Cache gezielt zurücksetzen. `auth-reset` benötigt den
Writer-Lock; daher erst den Dienst stoppen:

```bash
docker compose -p momas-sync stop bafang-sync
docker compose -p momas-sync --profile sync run --rm --no-deps bafang-sync auth-reset
docker compose -p momas-sync --profile sync up -d --no-deps --force-recreate bafang-sync
```

Backups des Datenverzeichnisses enthalten den vertraulichen Token-Cache:
verschlüsselt und zugriffsbeschränkt sichern. Account-/Passwort-Secrets separat
sichern. Keine Secrets, Cache-Dateien, Originaldaten oder `.env` auf GitHub laden.

## Kompatibilität und Tests

`BAFANG_AUTH_MODE=token` bleibt für bestehende Installationen verfügbar;
`compose.token.yaml` beschreibt die alte Token-Konfiguration. Ohne expliziten
Modus wählt `auto` Login, sobald Account oder Passwort konfiguriert sind,
andernfalls den bisherigen Token. Ein unvollständiger Login ergibt einen
Konfigurationsfehler und keinen Token-Fallback.

Automatisierte Tests verwenden ausschliesslich synthetische Zugangsdaten und
Antworten. Sie prüfen das echte Request-Format, Token-Cache, Neustart,
Secret-Wechsel, Ablauf, begrenzte Wiederanmeldung, persistente Sperre,
Netzwerkfehler, Redirect-Verweigerung und Datenschutz. Natürlicher Tokenablauf
ist simuliert; die neue Session und der Fahrtzugriff sind live bestätigt.
