# Phase 3 – automatischer Endurain-Import

Der laufende Sync-Dienst überträgt neue geprüfte Fahrten automatisch an
Endurain v0.19.3. Ein Upload erfolgt nach erfolgreichem Bafang-Abruf und lokaler
Zeitprüfung, innerhalb desselben 30-Minuten-Zyklus.

## Konfiguration

```dotenv
ENDURAIN_ENABLED=true
ENDURAIN_URL=http://dein-interner-endurain-host:8094
```

Den API-Key als `secrets/endurain_api_key` mit Modus 600 hinterlegen. Compose
mountet ihn unter `/run/secrets/endurain_api_key`; kein Schlüssel im Image,
Compose-YAML, Log oder Git. Authentifizierung ausschliesslich im Header
`X-API-Key`. Der Key benötigt `activities:upload` und gehört zum gewünschten
Endurain-Benutzer. URL muss eine Basis-URL ohne `/settings/security` sein.
TLS-Verifikation bleibt bei HTTPS aktiv, Redirects werden nicht verfolgt.
Für die ausdrücklich konfigurierte private LAN-URL ist auch HTTP möglich.

```bash
docker compose -p momas-sync --profile sync build bafang-sync
docker compose -p momas-sync --profile sync up -d --no-deps bafang-sync
docker compose -p momas-sync exec -T bafang-sync momas-worker status
docker compose -p momas-sync exec -T bafang-sync momas-worker healthcheck
```

Mit `ENDURAIN_ENABLED=false` und Neuerstellung des eigenen Sync-Containers kann
nur der Upload deaktiviert werden; lokale Sicherung der Fahrten läuft weiter.

## Warum TCX?

Im Endurain-GPX-Parser der Version v0.19.3 werden explizite Leistungswerte von
0 W nicht als Messpunkte gespeichert. Beim ersten GPX-Testimport ergaben sich
deshalb eine überschätzte Durchschnittsleistung. Das ist kein Problem der lokalen Zeitkorrektur.

Der automatische Import verwendet **TCX**. Die Konvertierung erhält zeitgestempelte
Koordinaten, Höhe, Leistung einschliesslich 0 W, Kadenz und vorhandene Herzfrequenz.
Die Fahrt wird als `Biking` bezeichnet; TCX hat keine eigene E-Bike-Sportart.
Kilometer aus Bafang `trip` werden als Meter im TCX-Lap übertragen. Diese
Einheitenzuordnung wurde anhand der Live-Daten geprüft. Originalkalorien werden
übernommen, falls vorhanden; ohne dieses optionale Feld verlangt TCX einen
Kalorienwert, der dann 0 bedeutet (unbekannt). Keine Energie-/Akkuannahmen.

Original-JSON und -GPX bleiben unverändert. Die korrigierte GPX-Importkopie bleibt
neben der TCX-Datei vorhanden. Ungültige/mehrdeutige Zeitdaten sowie nicht darstellbare
Sensorwerte werden nicht stillschweigend geraten oder abgeschnitten.

Die TCX-Dateien wurden lokal mit genau dem in Endurain v0.19.3 verwendeten
`tcxreader==0.4.11` gegengeprüft: alle Trackpunkte, Leistungswerte einschliesslich
Nullwerten und Kadenzwerte bleiben enthalten. Diese Bibliothek war nur für die
Verifikation in einer separaten Testumgebung nötig; der Dienst selbst bleibt
bei Python-Standardbibliothek ohne zusätzliche Laufzeitabhängigkeiten.

## Uploadstatus und Duplikatschutz

SQLite-Schemaversion 2 ergänzt die Tabelle `uploads`; Version 1 wird beim Start
migriert. Eindeutig ist die Kombination aus Endurain-Basis-URL und Bafang-Fahrt-ID.
Alle erfolgreichen Uploads werden dauerhaft gespeichert, inklusive Ziel-ID,
Request-Datei, Request-/Response-Hash und privater Antwortdatei.

Vor dem POST wird der Status `inflight` persistiert. Bei Netzwerkfehlern, 5xx,
ungültiger Antwort oder Prozessabbruch bleibt das Ergebnis `unknown`/`inflight`.
Solche POSTs werden **nicht blind wiederholt**, weil der Server schon eine Fahrt
angelegt haben könnte. Das vermeidet Duplikate ohne eine nicht vorhandene
Idempotency-Key-Garantie. Die Bafang-GETs behalten ihren Retry mit Backoff.

Bekannte Ablehnungen werden `rejected`; diese Fahrt benötigt Fehlerbehebung und
Abgleich vor erneuter Freigabe. Andere neue Fahrten können weiterhin importiert
werden. Fehlgeschlagene/ungeklärte Uploads ergeben `degraded` und einen negativen
Healthcheck. Ein vorhandener Upload wird bei Metadatenänderungen oder einer
lokalen Wiederherstellung nicht automatisch nochmals angelegt. Updates und
Ersetzungen vorhandener Endurain-Aktivitäten benötigen einen separaten Abgleich.

## Verifikation

Nach einem TCX-Upload werden Distanz, lokale Start-/Endzeit, Dauer, Durchschnitt
und Maximum von Leistung/Kadenz mit den gesendeten Messwerten verglichen.
Endurain rundet bestimmte Kennzahlen auf ganze Zahlen; die Prüfung erlaubt diese
Rundung. Die zeitliche Darstellung in der Upload-Antwort ist lokalisiert und
enthält teilweise keinen Offset, weshalb die zurückgegebene Aktivitäts-Zeitzone
berücksichtigt wird. Sie darf nicht stillschweigend als UTC interpretiert werden.

`verified` bedeutet hier: die Uploadantwort und ihre Zusammenfassungen passen.
Es ist kein Beleg für einen vollständigen Rückdownload aller serverseitigen
Messpunkte. Die privaten Stream-Endpunkte von v0.19.3 erfordern JWT und stehen
dem Upload-Key nicht zur Verfügung. Der Dienst benötigt diese zusätzliche
Berechtigung nicht zum Importieren. Es wird keine Aktivität freigegeben, um eine
Prüfung zu ermöglichen. Wenn später ein Eigentümer-Readback möglich ist, kann
er die bereits durchgeführten lokalen Parser-/Zusammenfassungsprüfungen ergänzen.

Private Belege liegen unter
`data/sync/deliveries/<zielhash>/<fahrthash>/`:

- Unveränderte gesendete GPX- oder TCX-Datei.
- `response.json`: erfolgreiche API-Antwort.
- `comparison.json`: lokale Messwert-/Zusammenfassungsprüfung.

Backup/Restore aus [PHASE2.md](PHASE2.md) sichert auch diese Dateien und die
Upload-Tabelle. Die Prüfroutine validiert Upload-Dateireferenzen und ihre Hashes.
Ein Restore schützt vor lokalem Datenverlust, ersetzt aber keinen Abgleich von
Uploads, die erst **nach** dem Snapshot auf dem Server angekommen sind. Vor
Wiederaufnahme eines alten Snapshots solche späteren Aktivitäten abgleichen.

## Kompatibilität und bekannte Grenzen

Geprüfte Endurain-Version: v0.19.3. Andere Versionen vor dem Automatikbetrieb
mit eigenen Daten testen. Bestehende manuelle GPX-Imports werden nicht ersetzt
oder automatisch erkannt; der Upload-Key erlaubt kein Bearbeiten oder Löschen.
In v0.19.3 kann bei TCX die Geschwindigkeit in der Übersicht fehlen, obwohl
Geschwindigkeitsdaten in der Aktivitätsdetailansicht verfügbar sind.

Der Diagnosebefehl `endurain-test` stammt aus dem ursprünglichen GPX-Prototyp:
Er überträgt höchstens eine vorbereitete Fahrt als **GPX**. Er validiert nicht
den TCX-Automatikpfad und ist wegen des beschriebenen 0-W-Verlusts nicht der
empfohlene Einrichtungsweg. Der Schnellstart verwendet den regulären TCX-Import.
