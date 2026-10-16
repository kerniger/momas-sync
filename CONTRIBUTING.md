# Entwicklung und Veröffentlichung

Python 3.11–3.13 und Docker Compose v2 werden in CI geprüft. Die Python-Laufzeit
benötigt keine zusätzlichen Bibliotheken. Tests verwenden nur synthetische Daten
und senden keine Anfragen an echte Konten oder Dienste.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
```

Änderungen an Login, Zeitkorrektur oder Uploadstatus benötigen passende
Fehlerfalltests. Keine echten Tokens, Accounts, Fahrten, App-APKs oder
dekompilierten Drittanbieter-Quelltexte in Tests/Commits aufnehmen.

## Veröffentlichung vorbereiten

`check_release.py` prüft die von Git erfasste Dateiliste auf private Pfade und
auffällige Inhalte. Diese Prüfung ergänzt die manuelle Durchsicht; sie ist keine
Garantie, dass beliebige Geheimnisse erkannt werden.

```bash
git add README.md src tests scripts .github
python3 scripts/check_release.py
python3 scripts/check_release.py --zip dist/momas-sync-public.zip
```

Der ZIP-Export enthält ausschliesslich geprüfte, von Git erfasste Dateien aus
dem Arbeitsverzeichnis. Index und Arbeitsdateien müssen übereinstimmen. Änderungen zuerst prüfen und stagen; der Export ersetzt
keinen überprüften Commit. Laufzeitverzeichnisse bleiben auch bei erzwungenem
`git add -f` durch die Exportprüfung ausgeschlossen.

Vor dem ersten Push:

1. `git diff --cached` und die vollständige Dateiliste prüfen, Tests ausführen.
2. Lizenz prüfen; für dieses Projekt liegt die MIT-Lizenz bei.
3. Repository-Name und Sichtbarkeit festlegen, dann Repository erstellen und pushen.
4. GitHub Private Vulnerability Reporting und Secret Scanning aktivieren, sofern verfügbar.
5. Nur tatsächlich veröffentlichten Code taggen. Container werden derzeit lokal
   gebaut; es wird kein GHCR-Image oder automatischer Release vorausgesetzt.

Der erste öffentliche Stand baut auf Version 0.4.0 auf. Bei Änderungen am
Laufzeitverhalten Versionsnummer und Changelog aktualisieren. Bestehende
Installationen behalten Projektname, Datenvolume und Endurain-Ziel-URL.
