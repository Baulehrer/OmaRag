# API-v1-Kompatibilitaet

- Additive JSON-Felder und neue Ereignistypen sind innerhalb von v1 erlaubt.
- Clients ignorieren unbekannte Felder und unbekannte Ereignistypen.
- Semantische Aenderungen oder entfernte Felder erfordern v2.
- `event_id` ist global monoton, `sequence` innerhalb eines Jobs/Runs monoton.
- Mutierende Startoperationen verwenden `Idempotency-Key`.
- SSE-Reconnects verwenden `Last-Event-ID`; Snapshots liefern den Ankerzustand.
- Seit 0.6 sind `progress_detail`, Dokument-Fingerprints und Pipeline-Statistiken additive Felder.
- `/search/explain` zeigt ausschließlich Ergebnisse öffentlicher Haiku-APIs; der authentifizierte
  Citation-Preview-Endpunkt liefert `image/png` und akzeptiert keinen frei wählbaren Dateipfad.
- Buchmetadaten, Qualitätswerte, Evidenz-IDs und `search_type` sind additive v1-Felder.
- Haiku wird als neueste stabile Kandidaten-Runtime installiert und erst nach dem öffentlichen
  API-Kompatibilitäts-Probe aktiviert; eine inkompatible Version ersetzt die laufende Runtime nicht.

Die Dateien `openapi.snapshot.json`, `events.schema.json` und
`workspace.schema.json` werden mit `scripts/generate_contracts.py` erzeugt und
in Contract-Tests auf unbeabsichtigte Aenderungen geprueft.
