# API-v1-Kompatibilitaet

- Additive JSON-Felder und neue Ereignistypen sind innerhalb von v1 erlaubt.
- Clients ignorieren unbekannte Felder und unbekannte Ereignistypen.
- Semantische Aenderungen oder entfernte Felder erfordern v2.
- `event_id` ist global monoton, `sequence` innerhalb eines Jobs/Runs monoton.
- Mutierende Startoperationen verwenden `Idempotency-Key`.
- SSE-Reconnects verwenden `Last-Event-ID`; Snapshots liefern den Ankerzustand.

Die Dateien `openapi.snapshot.json`, `events.schema.json` und
`workspace.schema.json` werden mit `scripts/generate_contracts.py` erzeugt und
in Contract-Tests auf unbeabsichtigte Aenderungen geprueft.
