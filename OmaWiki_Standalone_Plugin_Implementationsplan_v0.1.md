---
title: "OmaWiki – Implementationsplan als erstes eigenständiges OmaRag-Plugin"
project: "OmaRag / OmaWiki"
document_version: "0.1"
plugin_version_target: "0.1.0"
host_version_target: "OmaRag 0.4"
status: "Implementationsspezifikation"
date: "2026-07-25"
language: "de"
technical_basis:
  omarag_tui: "Rust + Ratatui"
  omarag_backend: "omaragd"
  haiku_rag: "0.70.x"
  okf: "0.2"
  plugin_runtime: "eigener Python-Prozess"
  plugin_transport: "bidirektionales JSON-RPC 2.0 über stdio"
---

# OmaWiki – Implementationsplan als erstes eigenständiges OmaRag-Plugin

## 1. Kurzentscheidung

**OmaWiki wird das erste offizielle, aber vollständig eigenständige Plugin für OmaRag.** Es wird:

- in einem **separaten Repository** entwickelt,
- unabhängig versioniert, installiert, aktualisiert und entfernt,
- in einem **eigenen Prozess** ausgeführt,
- nicht in den Python-Prozess von `omaragd` importiert,
- nicht direkt mit LanceDB, Haiku RAG oder Ollama kommunizieren,
- ausschließlich dokumentierte, berechtigungsgeprüfte OmaRag-Hostdienste verwenden,
- sein Wissen als normales, auch ohne OmaRag lesbares **OKF-0.2-Bundle** speichern,
- seine TUI-Ansichten deklarativ an OmaRag anmelden,
- bei einem Absturz weder das TUI noch den RAG-Kern mitreißen.

OmaWiki ist zugleich die **Referenzimplementierung des OmaRag-Pluginmodells**. Es darf keine privaten Sonderwege benutzen, die späteren Drittplugins nicht zur Verfügung stehen.

> **Leitgedanke:** OmaRag verwaltet Quellen, Modelle, Ressourcen, Jobs und Sicherheit. OmaWiki organisiert daraus abgeleitetes Wissen. Haiku RAG bleibt die autoritative Quelle für Originalbelege.

---

# 2. Zielbild

OmaWiki ergänzt OmaRag um eine dauerhafte Wissensschicht:

```text
Originaldokumente
      ↓
Haiku RAG
      ↓
Originaltreffer, Seiten, Tabellen, Bilder und Belege
      ↓
OmaRag Hostdienste
      ↓
OmaWiki Plugin
      ├── Begriffe und Aussagen extrahieren
      ├── bestehende Konzepte erkennen
      ├── OKF-Änderungsvorschläge erzeugen
      ├── Aussagen gegen Originalquellen prüfen
      ├── Review und Freigabe verwalten
      └── Wiki-Suche und Wissensgraph bereitstellen
      ↓
OKF-Bundle aus Markdown + YAML-Frontmatter
```

## 2.1 Rollenverteilung

| Komponente | Verantwortung |
|---|---|
| **Haiku RAG** | Originaldokumente verarbeiten, durchsuchen und mit Seiten-/Abschnittsbezug belegen |
| **omaragd** | Pluginprozess überwachen, Berechtigungen prüfen, Jobs speichern, Modelle vermitteln, Ereignisse persistieren |
| **OmaWiki** | Wissen extrahieren, strukturieren, verlinken, prüfen, reviewbar speichern und durchsuchen |
| **OKF-Bundle** | Offene, portable und menschenlesbare Wissensquelle |
| **Ratatui-TUI** | Pluginansichten mit OmaRag-eigenen Widgets darstellen |
| **Ollama/Provider** | Modelle ausführen; Zugriff ausschließlich über den OmaRag-Modellbroker |

## 2.2 Produktversprechen

OmaWiki soll nicht bloß automatisch Zusammenfassungen ablegen. Es soll:

1. **jede fachliche Aussage auf Originalbelege zurückführen**,
2. **Änderungen als kontrollierbare Vorschläge statt als blindes Überschreiben erzeugen**,
3. **manuelle Bearbeitungen respektieren**,
4. **Widersprüche sichtbar machen**,
5. **veraltete Wissensseiten erkennen**,
6. **ohne proprietäre Datenbank lesbar bleiben**,
7. **als Plugin vollständig deaktivierbar und entfernbar sein**.

---

# 3. Verbindliche Architekturentscheidungen

## 3.1 Eigenständiges First-Party-Plugin

OmaWiki ist ein **First-Party-Plugin** mit eigener Veröffentlichung, aber ohne privilegierte interne API.

Vorgesehene Identitäten:

```text
Anzeigename:        OmaWiki
Plugin-ID:          de.omarag.omawiki
Paketname:          omarag-omawiki
Prozess:            omawiki-plugin
Repository:         omarag/omawiki-plugin
Pluginprotokoll:    ORPP 1.0
OKF-Zielversion:    0.2
```

## 3.2 Prozessisolierung statt In-Process-Import

Nicht verwenden:

- Rust-Dynamic-Library-Plugins mit instabiler ABI,
- beliebigen Python-Import in `omaragd`,
- Monkey-Patching von Haiku RAG,
- direkte LanceDB-Zugriffe,
- Parsing der Haiku-CLI-Ausgabe,
- Ausführung von Plugin-ANSI oder Ratatui-Code im Host,
- direkten Netzwerkzugriff des Plugins auf Ollama oder Cloudanbieter.

Stattdessen läuft pro aktiviertem Workspace ein eigener Pluginprozess:

```text
omaragd
   ├── Haiku-Adapter
   ├── Job- und Ereignisspeicher
   ├── Modellbroker
   ├── Plugin-Host
   │      └── omawiki-plugin  [Workspace Baustoffkunde]
   └── HTTP/JSON + SSE zum TUI
```

### Vorteile

- Abstürze bleiben isoliert.
- Python-Abhängigkeiten kollidieren nicht mit Haiku RAG.
- Pluginupdates erfordern keinen Umbau des OmaRag-Kerns.
- Spätere Plugins können in Rust, Python, Go oder anderen Sprachen entstehen.
- Rechte können pro Prozess und Workspace begrenzt werden.
- Das Plugin kann unabhängig getestet und veröffentlicht werden.

## 3.3 Kein zweiter dauerhafter Systemdienst

OmaWiki wird **kein zusätzlich zu `omaragd` separat zu verwaltender Daemon**.

- `omaragd` bleibt der einzige vom Nutzer zu betreibende Hintergrunddienst.
- Der Pluginprozess ist ein überwachte Kindprozess von `omaragd`.
- Bei aktiven Wiki-Jobs bleibt er am Leben.
- Im Leerlauf darf er nach einer konfigurierbaren Zeit beendet werden.
- Beim nächsten Zugriff startet `omaragd` ihn erneut.
- Schließt der Nutzer das TUI, laufen `omaragd` und aktive Pluginjobs weiter.

## 3.4 OKF ist die Quelle der Wahrheit

Das OKF-Bundle ist die dauerhafte Wissensquelle. Die Plugin-SQLite-Datenbank enthält ausschließlich:

- Suchindizes,
- abgeleitete Graphkanten,
- Jobcheckpoints,
- Reviewvorschläge,
- Hashes,
- Caches,
- technische Zustände.

Die SQLite-Datenbank muss jederzeit aus dem OKF-Bundle rekonstruierbar sein.

## 3.5 Quellenindex und Wikiindex bleiben getrennt

```text
Haiku-RAG-Index
= autoritative Originalquellen

OmaWiki-Index
= abgeleitete, zusammengefasste Wissensseiten
```

Eine Wikiseite darf niemals als Originalbeleg für sich selbst verwendet werden. Im verifizierten Hybridmodus führt OmaWiki nur zu relevanten Originaldokumenten und Chunks; die endgültige Antwort wird gegen Haiku-RAG-Treffer geprüft.

---

# 4. Technische Grundlage

Planungsbasis ist Haiku RAG `0.70.x`. Die aktuelle Dokumentation beschreibt hybride Suche, Kontextausweitung, Quellenmetadaten, Bildsuche und öffentliche Methoden für eigene Verarbeitungspipelines. Suchergebnisse können Dokumenttitel, Seiten, Überschriften und strukturbezogene Metadaten tragen; diese Informationen werden über den OmaRag-Adapter normalisiert und dem Plugin nur über freigegebene Hostmethoden angeboten.[^haiku-python] Haiku RAG `0.70.0` ergänzt außerdem Bildanhänge für Fragen und Analysen.[^haiku-changelog]

OmaWiki zielt auf **OKF 0.2**. OKF definiert ein bewusst minimales Bundle aus Markdown-Dateien mit YAML-Frontmatter, optionalen `index.md`- und `log.md`-Dateien sowie Feldern für Provenienz, Erzeugung, Verifikation und Lebenszyklus.[^okf-spec] Die Implementierung muss unbekannte zusätzliche Frontmatter-Felder tolerieren und darf ein Bundle nicht allein wegen fehlender optionaler Felder, unbekannter Typen oder beschädigter Querverweise ablehnen.[^okf-conformance]

---

# 5. Repository- und Paketstruktur

## 5.1 Eigenes Repository

```text
omawiki-plugin/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── CHANGELOG.md
├── SECURITY.md
├── plugin.toml
├── src/
│   └── omawiki_plugin/
│       ├── __init__.py
│       ├── __main__.py
│       ├── rpc/
│       ├── host/
│       ├── okf/
│       ├── compiler/
│       ├── verifier/
│       ├── review/
│       ├── query/
│       ├── graph/
│       ├── storage/
│       ├── ui/
│       └── diagnostics/
├── schemas/
│   ├── plugin-manifest.schema.json
│   ├── settings.schema.json
│   ├── rpc/
│   └── structured-output/
├── prompts/
│   ├── extractor/
│   ├── resolver/
│   ├── compiler/
│   └── verifier/
├── migrations/
│   ├── state/
│   └── bundle/
├── fixtures/
│   ├── okf-valid/
│   ├── okf-invalid/
│   ├── source-documents/
│   └── model-responses/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── security/
│   └── e2e/
└── packaging/
    ├── build_plugin.py
    ├── docker/
    └── sbom/
```

## 5.2 Implementierungssprache

Die erste Version wird in **Python 3.12+** umgesetzt, jedoch in einer eigenen isolierten Runtime.

Begründung:

- strukturierte LLM-Ausgaben lassen sich mit Pydantic zuverlässig modellieren,
- Markdown-, YAML- und Git-Werkzeuge sind ausgereift,
- OmaRag verwaltet ohnehin eine Python-Laufzeit für Haiku RAG,
- die Trennung erfolgt auf Prozessebene und nicht über gemeinsam importierte Pakete,
- die Pluginprotokollschicht bleibt sprachneutral.

Deterministische, performancekritische Teile können später in Rust ausgelagert werden, ohne das Pluginprotokoll zu ändern.

## 5.3 Isolierte Runtime-Slots

```text
~/.local/share/omarag/plugins/de.omarag.omawiki/
├── versions/
│   ├── 0.1.0/
│   │   ├── runtime/
│   │   ├── plugin.toml
│   │   └── package-info.json
│   └── 0.1.1/
├── current -> versions/0.1.1
└── rollback -> versions/0.1.0
```

Ein Update wird zunächst in einen neuen Slot installiert und geprüft. Erst nach erfolgreichem Healthcheck wird `current` atomar umgeschaltet.

---

# 6. OmaRag Plugin Host

OmaWiki kann erst als echtes Plugin umgesetzt werden, wenn OmaRag eine kleine, generische Plugin-Hostschicht besitzt.

## 6.1 Aufgaben des Hosts

Der Plugin Host in `omaragd` übernimmt:

- Pluginpakete erkennen, prüfen, installieren und aktualisieren,
- Manifeste validieren,
- Signaturen und Prüfsummen prüfen,
- Kompatibilität mit Host- und Protokollversion prüfen,
- Rechte vor Aktivierung anzeigen und speichern,
- pro Workspace Pluginprozesse starten und überwachen,
- bidirektionale RPC-Nachrichten vermitteln,
- Hostfähigkeiten berechtigungsgeprüft anbieten,
- Pluginjobs in die zentrale Queue einordnen,
- Pluginereignisse in den zentralen Eventstore übernehmen,
- TUI-Beiträge registrieren,
- Logs erfassen und Geheimnisse redigieren,
- Prozessabstürze erkennen und Jobs fortsetzen,
- Plugins deaktivieren, ohne deren Nutzerinhalte zu löschen.

## 6.2 Generische Pluginfähigkeiten

Das erste Protokoll kennt nur wenige, klar begrenzte Capability-Typen:

```text
settings_provider
ui_contributor
job_provider
knowledge_provider
diagnostics_provider
```

OmaWiki verwendet alle fünf:

| Capability | Verwendung durch OmaWiki |
|---|---|
| `settings_provider` | Workspacebezogene OmaWiki-Konfiguration |
| `ui_contributor` | Wiki-Browser, Review, Qualität und Einstellungen |
| `job_provider` | Scan, Compile, Verify, Lint, Reindex und Export |
| `knowledge_provider` | Wiki-Suche und Quellhinweise für Hybridfragen |
| `diagnostics_provider` | OKF-, Index- und Pluginzustand für das Qualitätscockpit |

## 6.3 Kein privilegierter OmaWiki-Sonderweg

Alle Funktionen, die OmaWiki benötigt, werden als allgemeine Hostfähigkeit beschrieben. Ein späteres anderes Wissensplugin könnte denselben Vertrag verwenden.

Das bedeutet insbesondere:

- kein direkter Import von `omawiki_plugin` in `omaragd`,
- keine privaten Python-Objekte über Prozessgrenzen,
- keine direkte SQLite-Verbindung zur OmaRag-Betriebsdatenbank,
- keine direkte Kenntnis des Ratatui-Komponentenbaums,
- keine direkte LanceDB-Verbindung.

---

# 7. Pluginmanifest

## 7.1 Beispiel

```toml
[plugin]
id = "de.omarag.omawiki"
name = "OmaWiki"
version = "0.1.0"
protocol = "1.0"
kind = "process"
scope = "workspace"
entrypoint = ["{runtime}/bin/python", "-m", "omawiki_plugin", "serve", "--stdio"]
min_host_version = "0.4.0"
max_host_version = "<0.6.0"
license = "Apache-2.0"

[compatibility]
okf_read = ["0.1", "0.2"]
okf_write = "0.2"
haiku_capabilities = [
  "hybrid_search",
  "document_metadata",
  "chunk_provenance"
]

[capabilities]
settings_provider = true
ui_contributor = true
job_provider = true
knowledge_provider = true
diagnostics_provider = true

[permissions.host]
documents_list = true
documents_read_metadata = true
documents_read_structure = true
chunks_read = true
search_sources = true
citations_resolve = true
models_generate = true
embeddings_generate = false
jobs_report = true
notifications_send = true

[permissions.filesystem]
read = [
  "workspace:/okf/**",
  "plugin-state:/**"
]
write = [
  "workspace:/okf/**",
  "plugin-state:/**"
]

[permissions.runtime]
network = []
secrets = []
process_spawn = false
shell = false

[contributions.navigation]
id = "wiki"
label = "Wiki"
requested_key = "w"
order = 50

[[contributions.commands]]
id = "wiki.compile"
label = "Wiki aktualisieren"

[[contributions.commands]]
id = "wiki.lint"
label = "Wiki prüfen"

[[contributions.commands]]
id = "wiki.review"
label = "Wiki-Änderungen prüfen"
```

## 7.2 Manifestregeln

- Plugin-ID im Reverse-DNS-Format.
- Semantische Versionierung.
- Exakte Protokollversion oder unterstützter Bereich.
- Alle Rechte explizit.
- Keine impliziten Netzwerk- oder Dateirechte.
- UI-Tastenkürzel sind Anfragen; der Host darf Konflikte umbelegen.
- Unbekannte Manifestfelder werden für Vorwärtskompatibilität erhalten, aber nicht automatisch als Rechte interpretiert.
- Das installierte Manifest wird mit der signierten Paketversion verglichen.

---

# 8. OmaRag Plugin Protocol – ORPP 1.0

## 8.1 Transport

Für lokale Plugins wird **bidirektionales JSON-RPC 2.0 über stdin/stdout** verwendet.

```text
stdin/stdout  → ausschließlich RPC
stderr        → strukturierte Pluginlogs
```

Vorteile:

- keine zusätzlichen Ports,
- kein lokales Tokenmanagement,
- Prozess und Verbindung besitzen denselben Lebenszyklus,
- sprachneutral,
- leicht testbar,
- der Host kann den Prozess eindeutig einem Workspace zuordnen.

Für spätere Container- oder Remoteplugins kann dasselbe Nachrichtenmodell über Unix Domain Sockets übertragen werden. HTTP ist nicht Bestandteil des ersten Plugin-MVP.

## 8.2 Initialisierung

Der Host startet den Prozess und sendet:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "plugin.initialize",
  "params": {
    "protocol_version": "1.0",
    "host": {
      "name": "OmaRag",
      "version": "0.4.0",
      "api_version": "1.0"
    },
    "plugin": {
      "id": "de.omarag.omawiki",
      "version": "0.1.0"
    },
    "workspace": {
      "id": "ws-baustoffkunde",
      "name": "Baustoffkunde",
      "okf_root": "/workspace/okf",
      "state_root": "/plugin-state"
    },
    "grants": [
      "documents.list",
      "documents.metadata.read",
      "documents.structure.read",
      "chunks.read",
      "sources.search",
      "citations.resolve",
      "models.generate",
      "jobs.report"
    ],
    "host_capabilities": {
      "visual_grounding": true,
      "question_images": true,
      "model_broker": true,
      "event_replay": true
    },
    "locale": "de-DE"
  }
}
```

Antwort:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocol_version": "1.0",
    "plugin_state_version": 1,
    "capabilities": {
      "settings_provider": "1.0",
      "ui_contributor": "1.0",
      "job_provider": "1.0",
      "knowledge_provider": "1.0",
      "diagnostics_provider": "1.0"
    },
    "status": "ready"
  }
}
```

Danach sendet der Host `plugin.initialized` als Notification.

## 8.3 Lebenszyklusmethoden

```text
plugin.initialize
plugin.initialized
plugin.health
plugin.activate
plugin.deactivate
plugin.prepare_update
plugin.migrate
plugin.shutdown
```

## 8.4 Jobmethoden

```text
plugin.job.start
plugin.job.resume
plugin.job.pause
plugin.job.cancel
plugin.job.snapshot
```

Der Host besitzt die globale Job-ID. Das Plugin liefert zusätzlich einen plugininternen Checkpoint.

## 8.5 UI-Methoden

```text
plugin.ui.describe
plugin.ui.load_view
plugin.ui.invoke_action
plugin.ui.complete
```

Das Plugin liefert Daten und deklarative Viewmodelle, niemals Terminalausgaben.

## 8.6 Knowledge-Provider-Methoden

```text
plugin.knowledge.search
plugin.knowledge.get
plugin.knowledge.resolve_source_hints
plugin.knowledge.related
```

## 8.7 Hostmethoden für das Plugin

```text
host.documents.list
host.documents.get_metadata
host.documents.get_structure
host.documents.get_chunks
host.search.sources
host.citations.resolve
host.media.resolve
host.models.generate
host.embeddings.generate
host.jobs.progress
host.jobs.checkpoint
host.events.publish
host.notifications.send
host.audit.record
```

Jede Hostmethode prüft die erteilten Grants. Der Pluginprozess darf fehlende Rechte nicht durch direkte Dateisystem-, Netzwerk- oder Datenbankzugriffe umgehen.

## 8.8 Fehlerformat

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "error": {
    "code": -32021,
    "message": "Quelle konnte nicht aufgelöst werden.",
    "data": {
      "omarag_code": "SOURCE_REFERENCE_UNRESOLVED",
      "retryable": false,
      "user_action": "Dokument erneut indexieren oder Referenz entfernen.",
      "correlation_id": "ow-err-92a"
    }
  }
}
```

## 8.9 Zeitlimits, Abbruch und Heartbeat

- Lange Methoden müssen Jobmethoden verwenden.
- Normale RPC-Aufrufe erhalten kurze Hostzeitlimits.
- Abbruch erfolgt kooperativ über `plugin.job.cancel`.
- Der Pluginprozess sendet während aktiver Jobs Heartbeats.
- Bei ausbleibendem Heartbeat wird der Prozess beendet und aus dem letzten Checkpoint neu gestartet.
- RPC-IDs, Job-IDs und Plugin-Checkpoint-IDs bleiben getrennt.

---

# 9. Berechtigungs- und Sicherheitsmodell

## 9.1 Default-Deny

OmaWiki erhält ausschließlich die im Manifest angeforderten und vom Nutzer bestätigten Rechte.

Standard für OmaWiki 0.1:

```text
Netzwerkzugriff:       nein
Geheimnisse:           keine
Prozessstart:          nein
Shell:                 nein
Lesen außerhalb OKF:   nein
Schreiben außerhalb OKF und Pluginstatus: nein
Direkter Ollama-Zugriff: nein
Direkter Haiku-Zugriff: nein
```

## 9.2 Modellzugriff nur über Broker

Der Pluginprozess darf keine Ollama-URL oder API-Schlüssel erhalten. Er stellt Modellanfragen an `host.models.generate`.

Der Host entscheidet:

- welches Modell tatsächlich verwendet wird,
- ob es lokal oder in der Cloud läuft,
- ob eine Cloudfreigabe vorliegt,
- wie viel RAM und CPU verfügbar sind,
- ob eine laufende Indexierung pausiert werden muss,
- welches Token- und Zeitlimit gilt,
- ob Ergebnisse aus dem Cache verwendet werden können.

## 9.3 Dateisystembegrenzung

Schreibbar sind nur:

```text
workspace:/okf/**
plugin-state:/**
```

Der Host normalisiert alle Pfade und verhindert:

- `..`-Ausbrüche,
- Symlink-Ausbrüche,
- absolute fremde Pfade,
- Schreiben über Hardlinks,
- Dateinamen mit Steuerzeichen,
- Überschreiben reservierter Workspace-Dateien.

## 9.4 Optionale Linux-Sandbox

Auf Linux wird nach Möglichkeit eine zusätzliche Prozesssandbox verwendet:

- eigener Mount-Namespace,
- Pluginruntime read-only,
- nur die beiden erlaubten Verzeichnisse schreibbar,
- kein Netzwerk-Namespace,
- eingeschränkte Umgebungsvariablen,
- Ressourcenlimits,
- kein Zugriff auf SSH-Agent, Desktop-Schlüsselbund oder Docker-Socket.

Fällt die technische Sandbox aus, bleiben die brokerbasierten Rechte aktiv; OmaRag zeigt jedoch sichtbar an, dass nur die logische Isolation verfügbar ist.

## 9.5 Prompt-Injection-Schutz

Dokumentinhalte gelten als **unvertrauenswürdige Daten**, nicht als Anweisungen.

Verbindliche Regeln:

- Quelltext wird klar abgegrenzt in Modellanfragen eingefügt.
- Das Plugin gibt dem Modell keine Werkzeuge zur Datei-, Netzwerk- oder Prozessausführung.
- Modellantworten müssen ein JSON-Schema erfüllen.
- Das Modell darf keine freien Zielpfade festlegen.
- Raw-HTML und aktive Inhalte werden in generierten Seiten standardmäßig abgelehnt.
- Externe Bild- und Skriptreferenzen werden nicht automatisch geladen.
- Ein Dokument kann die Systemregeln des Compilers nicht überschreiben.

---

# 10. Workspace- und Speicherlayout

```text
Baustoffkunde.omarag/
├── workspace.toml
├── haiku.rag.yaml
├── sources.yaml
├── database/
├── okf/                                  # portable Wissensquelle
│   ├── index.md
│   ├── log.md
│   ├── purpose.md
│   ├── schema.md
│   ├── concepts/
│   ├── entities/
│   ├── comparisons/
│   ├── glossary/
│   ├── conflicts/
│   └── references/
│       └── documents/
└── .omarag/
    └── plugins/
        └── de.omarag.omawiki/
            ├── config.toml
            ├── state.sqlite
            ├── cache/
            ├── staging/
            ├── locks/
            └── diagnostics/
```

## 10.1 Uninstallationsregel

Beim Entfernen des Plugins:

- bleibt `okf/` vollständig erhalten,
- wird die Pluginruntime entfernt,
- kann der technische Zustand optional gelöscht werden,
- fragt OmaRag getrennt, ob Cache und `state.sqlite` entfernt werden sollen,
- wird das OKF-Bundle niemals ohne separate Bestätigung gelöscht.

## 10.2 Portabilität

Ein exportiertes OKF-Bundle benötigt weder OmaWiki noch OmaRag. Relative Links und Standardfrontmatter müssen außerhalb des Systems lesbar bleiben.

---

# 11. OKF-Profil für OmaWiki

## 11.1 Zielversion

OmaWiki schreibt **OKF 0.2** und liest:

- OKF 0.2 nativ,
- OKF 0.1 im Kompatibilitätsmodus,
- neuere unbekannte Minor-Versionen bestmöglich und read-only, bis Schreibkompatibilität bestätigt wurde.

Der Root-Index deklariert:

```markdown
---
okf_version: "0.2"
---
```

## 11.2 Kernregeln

Jede Konzeptdatei besitzt:

- UTF-8,
- YAML-Frontmatter,
- ein nichtleeres `type`,
- einen Markdown-Body.

OmaWiki verwendet die standardisierten OKF-Felder:

```yaml
type:
title:
description:
resource:
tags:
sources:
generated:
verified:
status:
stale_after:
```

OKF erlaubt `status` nur als `draft`, `stable` oder `deprecated`.[^okf-lifecycle] Daher sind folgende Anzeigen **abgeleitete UI-Zustände** und keine zusätzlichen Werte im `status`-Feld:

```text
STALE       aus stale_after oder geänderter Quelle
CONFLICT    aus Konfliktobjekten und Lintbefunden
ORPHAN      aus fehlenden eingehenden Links
BROKEN      aus nicht auflösbaren Referenzen
```

## 11.3 OmaRag-Erweiterungsblock

Herstellerspezifische Daten liegen unter einem einzigen Namespace:

```yaml
omarag:
  page_id: "01J4..."
  workspace_id: "ws-baustoffkunde"
  managed_sections:
    definition: "sha256:..."
    applications: "sha256:..."
  source_document_ids:
    - "doc-42"
  source_chunk_ids:
    - "chunk-42-511"
  pages:
    doc-42: [112, 113]
  source_hash: "sha256:..."
  compiler:
    plugin_version: "0.1.0"
    prompt_version: "extractor-3"
    model_digest: "sha256:..."
    job_id: "job-194"
```

Unbekannte zusätzliche Frontmatter-Felder werden erhalten und beim Schreiben nicht entfernt.

## 11.4 Stabile Seitenidentität

Da Dateipfade verändert werden können, führt OmaWiki zusätzlich `omarag.page_id` als stabile UUID/ULID.

- Pfad: menschenlesbare Identität im Bundle.
- `page_id`: interne stabile Identität über Umbenennungen hinweg.
- Titel: veränderbare Anzeige.
- Aliasnamen: plugindefinierte Liste unter `omarag.aliases`.

## 11.5 Quellenreferenzen

OKF 0.2 verlangt innerhalb jedes `sources`-Eintrags ein `resource`. Für portable, stabile Referenzen verwendet OmaWiki bundleinterne Dokumentseiten:

```yaml
sources:
  - id: "src-betonbuch-p112-c511"
    resource: "/references/documents/doc-42.md"
    title: "Betontechnologie, Seite 112"
    last_modified: 2025-08-01
```

Die Referenzseite enthält:

```yaml
---
type: Source Document
title: Betontechnologie
resource: "omarag-document://ws-baustoffkunde/doc-42"
status: stable
omarag:
  document_id: doc-42
  original_uri_policy: private
  content_hash: "sha256:..."
---
```

Bei einem Export kann der Nutzer wählen:

```text
Privat       Originalpfade verbergen
Portabel     relative mitexportierte Quellen verwenden
Vollständig  Original-URI aufnehmen
```

## 11.6 Beleg pro Aussage

Fachliche Aussagen erhalten Fußnoten, deren Bezeichner mit `sources[].id` übereinstimmt. Dieses Mapping ist in OKF 0.2 ausdrücklich für claimbezogene Provenienz vorgesehen.[^okf-sources]

```markdown
XC4 beschreibt wechselnd nasse und trockene Bedingungen.[^src-betonbuch-p112-c511]

[^src-betonbuch-p112-c511]: Betontechnologie, Seite 112.
```

## 11.7 Erzeugung und Verifikation

```yaml
generated:
  by: "process:de.omarag.omawiki/qwen-local"
  at: "2026-07-25T15:30:00Z"

verified:
  - by: "process:omawiki-source-verifier"
    at: "2026-07-25T15:33:12Z"
  - by: "human:stephan"
    at: "2026-07-25T16:04:00Z"
```

`generated` beschreibt den Ersteller des aktuellen Inhalts; `verified` beschreibt unabhängige Prüfungen. Beide dürfen nicht vermischt werden.[^okf-trust]

---

# 12. Technisches Datenmodell

## 12.1 SQLite als abgeleiteter Zustand

`state.sqlite` läuft im WAL-Modus und enthält mindestens:

```text
schema_info
pages
page_aliases
page_sections
page_sources
claims
claim_evidence
links
document_state
compile_runs
compile_units
proposals
proposal_operations
reviews
conflicts
lint_findings
checkpoints
prompt_cache
model_cache
fts_pages
```

## 12.2 Zentrale Entitäten

### `pages`

```text
page_id
path
title
type
status
content_hash
frontmatter_hash
generated_at
stale_after
trust_tier
last_indexed_at
```

### `claims`

```text
claim_id
page_id
section_id
claim_text
claim_kind
is_numeric
is_normative
verification_state
claim_hash
```

### `claim_evidence`

```text
claim_id
source_id
document_id
chunk_id
page_number
support_verdict
evidence_hash
```

### `document_state`

```text
document_id
source_hash
last_seen_at
last_compiled_at
compiler_version
prompt_version
model_digest
```

### `proposals`

```text
proposal_id
job_id
base_bundle_hash
risk_level
status
created_at
reviewed_at
reviewed_by
```

### `proposal_operations`

```text
operation_id
proposal_id
operation_type
target_page_id
base_file_hash
payload_json
validation_state
```

## 12.3 Suchindex

Version 0.1 verwendet SQLite FTS5 für:

- Titel,
- Aliasse,
- Beschreibungen,
- Überschriften,
- Bodytext,
- Tags.

Ein semantischer Wiki-Vektorindex ist nicht Teil des MVP. Er kann später über `host.embeddings.generate` ergänzt werden, bleibt aber weiterhin vom Haiku-Quellenindex getrennt.

---

# 13. Modellbroker und Modellrollen

## 13.1 Kein eigener Providercode im Plugin

OmaWiki beschreibt nur Rollen und Anforderungen:

```text
extractor
resolver
compiler
verifier
```

Der OmaRag-Modellbroker ordnet diesen Rollen konkrete Modelle zu.

## 13.2 Rollen

### Extractor

Extrahiert aus Quellabschnitten strukturierte Kandidaten:

- Begriffe,
- Definitionen,
- Aussagen,
- Beziehungen,
- Aliasnamen,
- Normen,
- Zahlenwerte,
- Tabellenhinweise,
- mögliche Konflikte.

### Resolver

Ordnet Kandidaten bestehenden Konzeptseiten zu:

- identisch,
- Alias,
- engeres/weiteres Konzept,
- neue Seite,
- unsicher.

### Compiler

Erzeugt einen **typisierten Änderungsplan**, keinen freien Dateitext.

### Verifier

Prüft jede vorgeschlagene Aussage ausschließlich gegen Originalausschnitte:

```text
supported
partially_supported
unsupported
conflicting
```

## 13.3 Brokeranfrage

```json
{
  "role": "wiki.verifier",
  "workspace_id": "ws-baustoffkunde",
  "input": {
    "claim": "XC4 beschreibt wechselnd nasse und trockene Bedingungen.",
    "evidence": [
      {
        "document_id": "doc-42",
        "page": 112,
        "content": "..."
      }
    ]
  },
  "output_schema": "VerificationResult.v1",
  "policy": {
    "local_preferred": true,
    "cloud_data_class": "document_content",
    "temperature": 0.0,
    "max_output_tokens": 800
  }
}
```

## 13.4 Caching

Modellergebnisse werden nur wiederverwendet, wenn alle Hashbestandteile übereinstimmen:

```text
Eingabedaten
Promptversion
Ausgabeschema
Modell-ID und Modelldigest
Pluginversion
Sprachprofil
```

## 13.5 Hardwareprofile

### Yoga-Profil

- nur ein LLM-Aufruf gleichzeitig,
- Wiki Lite als Standard,
- Verarbeitung bevorzugt im Netzbetrieb,
- kleine Batches,
- Compiler und Verifier möglichst dasselbe Modell,
- keine parallele große RAG-Indexierung.

### 10-vCPU-/24-GiB-Server

- ein geladenes Hauptmodell bevorzugen,
- Extraktion in kleinen, checkpointfähigen Batches,
- andere Docker-Dienste über den OmaRag-Ressourcenwächter berücksichtigen,
- Wiki Deep ausschließlich geplant oder manuell,
- Chat erhält höhere Priorität als Wiki-Kompilierung.

---

# 14. Strukturierte Modelloutputs

## 14.1 Extraktion

```python
class ExtractedClaim(BaseModel):
    claim_id: str
    text: str
    kind: Literal["definition", "fact", "rule", "number", "relation", "example"]
    source_chunk_ids: list[str]
    source_pages: list[int]
    confidence_class: Literal["clear", "ambiguous"]

class ConceptCandidate(BaseModel):
    canonical_name: str
    proposed_type: str
    aliases: list[str]
    claims: list[ExtractedClaim]
    relations: list["RelationCandidate"]
```

## 14.2 Auflösung

```python
class ResolutionDecision(BaseModel):
    candidate_id: str
    action: Literal["merge", "create", "alias", "skip", "needs_review"]
    target_page_id: str | None
    rationale_code: str
```

## 14.3 Verifikation

```python
class VerificationResult(BaseModel):
    claim_id: str
    verdict: Literal[
        "supported",
        "partially_supported",
        "unsupported",
        "conflicting"
    ]
    supporting_source_ids: list[str]
    missing_qualifications: list[str]
```

Das Plugin verwirft Ausgaben, die das Schema nicht erfüllen. Eine automatische „Reparatur“ darf nur begrenzt und protokolliert erfolgen.

---

# 15. Typisiertes Patchmodell

## 15.1 Keine freien LLM-Dateipatches

Das Modell darf weder kompletten Markdowntext noch einen beliebigen Unified Diff direkt anwenden. Es erzeugt nur typisierte Operationen.

## 15.2 Operationen

```text
CreatePage
AddClaim
ReplaceClaim
RemoveClaim
AddSource
RemoveSource
AddLink
RemoveLink
AddAlias
RenamePage
DeprecatePage
CreateConflictPage
ResolveConflict
UpdateDescription
UpdateIndex
AppendLogEntry
```

## 15.3 Beispiel

```json
{
  "operation": "AddClaim",
  "target_page_id": "01J4XC4",
  "base_file_hash": "sha256:old",
  "section": "definition",
  "claim": {
    "text": "XC4 beschreibt wechselnd nasse und trockene Bedingungen.",
    "source_ids": ["src-betonbuch-p112-c511"]
  }
}
```

## 15.4 Deterministischer Renderer

Der Renderer:

- normalisiert erlaubte Slugs,
- bewahrt unbekannte Frontmatter-Felder,
- erzeugt Fußnoten,
- aktualisiert `sources`,
- aktualisiert `generated`,
- lässt vorhandene `verified`-Einträge nur bestehen, wenn der geprüfte Inhalt unverändert ist,
- setzt neue LLM-Seiten zunächst auf `draft`,
- schreibt in ein Staging-Verzeichnis,
- validiert das vollständige Ergebnis,
- erzeugt einen lesbaren Diff.

## 15.5 Optimistic Concurrency

Vor dem Anwenden wird `base_file_hash` geprüft.

Bei Abweichung:

```text
FILE_CHANGED_SINCE_PROPOSAL
```

Dann erfolgt kein Überschreiben. OmaWiki erstellt einen neuen Dreiwegevergleich:

```text
Basis des Vorschlags
aktuelle Nutzerdatei
vorgeschlagene Änderung
```

---

# 16. Wiki-Compiler-Pipeline

```text
1. Scan
2. Select
3. Extract
4. Resolve
5. Plan
6. Verify
7. Stage
8. Lint
9. Review
10. Commit
11. Reindex
12. Publish
```

## 16.1 Scan

- Dokumentbestand über Host-API lesen.
- Dokument- und Strukturhashes vergleichen.
- neue, geänderte und entfernte Dokumente bestimmen.
- abhängige Wikiseiten ermitteln.
- Jobumfang und grobe Kosten schätzen.

Ausgabe:

```text
12 neue Dokumente
3 geänderte Dokumente
1 entfernte Quelle
27 möglicherweise betroffene Wikiseiten
```

## 16.2 Select

Nicht jedes Chunk wird ungefiltert an ein LLM geschickt.

Auswahlkriterien:

- Überschriften und Abschnittsstruktur,
- Definitionen und Tabellen,
- wiederkehrende zentrale Begriffe,
- Quellmetadaten,
- Mindestinhaltslänge,
- Ausschluss von Impressum, Inhaltsverzeichnis-Dubletten und leeren OCR-Fragmenten,
- Corpus-Profil.

## 16.3 Extract

- verarbeitbare Batches bilden,
- Quell-IDs und Seiten beibehalten,
- strukturierte Kandidaten extrahieren,
- numerische und normative Aussagen kennzeichnen,
- unsichere Extraktionen nicht als Fakten übernehmen.

## 16.4 Resolve

Auflösung in dieser Reihenfolge:

1. stabile Seiten-ID,
2. exakter normalisierter Titel,
3. Alias,
4. FTS-Kandidaten,
5. Linknachbarschaft,
6. Resolvermodell.

Eine neue Seite wird nur vorgeschlagen, wenn:

- ein zentraler Fachbegriff vorliegt,
- mehrere Aussagen oder Beziehungen vorhanden sind,
- der Begriff von anderen Seiten referenziert werden soll,
- keine bestehende Seite ausreichend passt.

## 16.5 Plan

Der Compiler erzeugt typisierte Operationen und eine Risikoeinstufung.

## 16.6 Verify

Für jede fachliche Aussage:

1. angegebene Chunk-IDs über den Host auflösen,
2. Originalkontext laden,
3. Seite und Dokument prüfen,
4. deterministische Referenzprüfung,
5. LLM-Verifikation gegen ausschließlich diese Belege,
6. Ergebnis speichern.

Regeln:

- `unsupported` wird nicht übernommen.
- `partially_supported` erfordert Review oder präzisierte Formulierung.
- `conflicting` erzeugt einen Konfliktvorschlag.
- numerische und normative Aussagen erfordern mindestens einen exakt auflösbaren Originalbeleg.

## 16.7 Stage

Änderungen werden auf eine Kopie des betroffenen Teilbaums angewandt.

```text
plugin-state:/staging/<proposal-id>/bundle/
```

## 16.8 Lint

Deterministisch geprüft werden:

- YAML-Frontmatter,
- `type`,
- zulässige Lebenszykluswerte,
- Pfade,
- Quellenressourcen,
- Fußnoten-IDs,
- doppelte IDs,
- Linkziele,
- reservierte Dateien,
- UTF-8,
- verbotene aktive Inhalte,
- Selbstbelege,
- veraltete Hashreferenzen.

## 16.9 Review

Der Nutzer sieht nicht nur Textdiffs, sondern fachliche Operationen:

```text
XC4
+ Definition ergänzt
+ 2 Originalquellen verknüpft
+ Beziehung zu Betondeckung angelegt
! normative Aussage – manuelle Freigabe erforderlich
```

## 16.10 Commit

Nach Freigabe:

1. Workspace-Lock erwerben,
2. Basis-Hashes erneut prüfen,
3. Sicherheitskopie/Restorepunkt anlegen,
4. Dateien atomar ersetzen,
5. `index.md` aktualisieren,
6. `log.md` ergänzen,
7. optional Git-Commit erstellen,
8. Lock lösen.

## 16.11 Reindex

- FTS aktualisieren,
- Links neu aufbauen,
- Quellabhängigkeiten aktualisieren,
- Trust- und Stale-Zustände neu berechnen.

## 16.12 Publish

Der Host erhält:

- Jobabschluss,
- geänderte Seiten,
- Reviewstatus,
- Qualitätswarnungen,
- Benachrichtigung für TUI/Desktop.

---

# 17. Wiki Lite und Wiki Deep

## 17.1 Wiki Lite – MVP

Wiki Lite ist der erste produktive Compiler.

Enthalten:

- Dokumentreferenzseiten,
- Dokumentzusammenfassungen,
- Glossareinträge,
- zentrale Begriffsseiten,
- einfache Beziehungen,
- inkrementelle Aktualisierung,
- claimbezogene Quellen,
- Reviewvorschläge,
- FTS-Suche.

Nicht enthalten:

- großflächige globale Reorganisation,
- automatische Konfliktauflösung,
- komplexe historische Synthesen,
- automatische Verschmelzung vieler Seiten,
- Agentenschleifen ohne feste Grenze.

## 17.2 Wiki Deep – spätere Erweiterung

- dokumentübergreifende Vergleichsseiten,
- tiefe Konfliktanalyse,
- Norm- und Versionsentwicklung,
- Ursache-Wirkungs-Netze,
- Lückenanalyse,
- semantischer Lint,
- Vorschläge zur Taxonomieverbesserung.

Wiki Deep wird geplant oder manuell ausgeführt und besitzt eine niedrigere Jobpriorität als Chat, Retrieval und normale Indexierung.

---

# 18. Review- und Freigabepolitik

## 18.1 Standard: alle LLM-Inhalte als Vorschlag

In OmaWiki 0.1 werden LLM-generierte Inhaltsänderungen grundsätzlich zunächst als Proposal gespeichert.

Automatisch anwendbar sind nur deterministische Änderungen wie:

- FTS-Neuaufbau,
- `index.md` aus bereits freigegebenen Seiten neu erzeugen,
- technische Hashaktualisierung ohne Inhaltsänderung,
- Logeintrag nach einer bereits freigegebenen Änderung.

## 18.2 Risikostufen

| Stufe | Beispiel | Freigabe |
|---|---|---|
| 0 | Index neu erzeugen | automatisch möglich |
| 1 | neue Draft-Seite mit belegten Grundinformationen | je nach Policy |
| 2 | numerische, normative oder prüfungsrelevante Aussage | menschlich |
| 3 | Umbenennen, Deprecation, Löschen, Konfliktauflösung | immer menschlich |

## 18.3 Policies

```text
Manuell
Alle Inhaltsänderungen prüfen.

Ausgewogen
Unkritische neue Draft-Seiten können nach vollständiger Quellenprüfung übernommen werden; Risikostufe 2/3 bleibt manuell.

Automatisch
Nur für bewusst freigegebene, unkritische Workspaces; Risikostufe 3 bleibt manuell.
```

Voreinstellung für bautechnische Fach- und Normenbestände: **Manuell** in OmaWiki 0.1, später **Ausgewogen**.

## 18.4 Human Review

Nach Freigabe wird ergänzt:

```yaml
verified:
  - by: "human:stephan"
    at: "..."
```

Ein inhaltlich veränderter Abschnitt verliert nicht zwingend alle historischen Verifikationsdaten, aber OmaWiki muss sichtbar machen, welche Verifikation vor der letzten Änderung lag. Für den aktuellen Trust-Tier zählt nur eine zur aktuellen Inhaltsversion passende Verifikation.

---

# 19. Manuelle Bearbeitung und Abschnittsschutz

Das OKF-Bundle darf mit Obsidian, VS Code oder einem Texteditor bearbeitet werden.

## 19.1 Managed Sections

OmaWiki speichert Hashes der zuletzt von ihm verwalteten Abschnitte:

```yaml
omarag:
  managed_sections:
    definition: "sha256:..."
    applications: "sha256:..."
```

## 19.2 Verhalten

- Hash unverändert: Abschnitt darf als Vorschlag aktualisiert werden.
- Hash verändert: Abschnitt gilt als manuell bearbeitet.
- Manuell bearbeitete Abschnitte werden nie still überschrieben.
- Der Review zeigt einen Dreiwegevergleich.
- Der Nutzer kann einen Abschnitt dauerhaft als „nur manuell“ markieren.

## 19.3 Externer Dateiwächter

Änderungen im `okf/`-Verzeichnis führen zu:

1. erneuter Validierung,
2. FTS-Aktualisierung,
3. Graphaktualisierung,
4. Erkennung manuell geänderter Abschnitte,
5. Warnung bei beschädigtem Frontmatter.

---

# 20. Quellenänderungen, Veraltung und Konflikte

## 20.1 Abhängigkeitsgraph

```text
Dokument → Chunk → Source-ID → Claim → Wikiseite
```

Bei geänderter Quelle werden nur abhängige Claims und Seiten neu bewertet.

## 20.2 Stale-Zustand

Eine Seite erscheint als veraltet, wenn:

- `today >= stale_after`,
- ein referenziertes Dokument einen neuen Hash besitzt,
- ein referenzierter Chunk nicht mehr existiert,
- ein Dokument entfernt wurde,
- eine spätere Dokumentversion das alte Dokument ersetzt,
- die letzte Verifikation nicht zur aktuellen Inhaltsversion gehört.

## 20.3 Konflikte

OmaWiki löst widersprüchliche Quellen nicht automatisch auf.

Es erzeugt beispielsweise:

```text
okf/conflicts/betondeckung-xc4.md
```

mit:

```yaml
---
type: Knowledge Conflict
title: Abweichende Angaben zur Betondeckung bei XC4
status: draft
sources:
  - ...
---
```

Die Hauptseite erhält einen sichtbaren Konflikthinweis, bis eine fachliche Entscheidung getroffen wurde.

## 20.4 Löschen und Deprecation

- Automatisches physisches Löschen von Konzeptseiten ist verboten.
- Ersetzte Seiten werden bevorzugt `deprecated` und verlinken auf den Nachfolger.
- Physisches Löschen erfordert eine separate explizite Nutzeraktion.
- Entfernte Quellen bleiben in der Historie erkennbar.

---

# 21. Knowledge Provider und Abfragemodi

## 21.1 Generischer Knowledge-Provider-Vertrag

OmaWiki liefert:

```json
{
  "hit_id": "wiki:01J4XC4",
  "title": "Expositionsklasse XC4",
  "snippet": "...",
  "score": 0.91,
  "trust_tier": "human-reviewed",
  "status": "stable",
  "stale": false,
  "source_hints": [
    {
      "document_id": "doc-42",
      "chunk_ids": ["chunk-42-511"],
      "pages": [112]
    }
  ]
}
```

## 21.2 Quellenmodus

OmaWiki wird nicht verwendet. OmaRag antwortet nur aus Haiku-RAG-Originaltreffern.

## 21.3 Wikimodus

- OmaWiki sucht relevante Konzeptseiten.
- Das TUI kennzeichnet die Antwort als abgeleitetes Wikiwissen.
- Quellenstatus, Staleness und Trust-Tier werden sichtbar.
- Originalquellen bleiben aufrufbar.

## 21.4 Verifizierter Hybridmodus – Standardziel

```text
Frage
  ↓
OmaWiki findet Konzepte und Quellhinweise
  ↓
OmaRag führt Haiku-Suche gegen Originalquellen aus
  ↓
Originalchunks werden erweitert und gerankt
  ↓
Antwort wird aus Originalbelegen erzeugt
  ↓
Wiki liefert Struktur, nicht die letzte Beweisinstanz
```

Verbindliche Regel:

> Die endgültigen Zitate des Hybridmodus verweisen auf Originaldokumente, Seiten und Chunks – nicht nur auf Wikiseiten.

## 21.5 Selbstverstärkung verhindern

Hostsuche für die Verifikation erhält explizit:

```text
corpus = source
exclude_provider = de.omarag.omawiki
```

Damit kann eine vom LLM erzeugte Wikiseite nicht später ihre eigene Aussage bestätigen.

---

# 22. TUI-Integration als deklarativer Pluginbeitrag

## 22.1 Keine Plugin-Renderinglogik im Terminal

Das Plugin liefert nur Viewmodelle. Ratatui-Widgets, Farben, Fokus, Tastatursteuerung und Themes bleiben im OmaRag-TUI.

## 22.2 Standardisierte Hostwidgets

ORPP UI 1.0 unterstützt zunächst:

```text
NavigationItem
Command
StatusCard
TreeView
TableView
MarkdownView
DetailView
DiffView
FormView
ProgressView
ActionBar
```

## 22.3 OmaWiki-Hauptbereich

```text
[w] Wiki
```

Unteransichten:

```text
Browser
Review
Qualität
Konflikte
Einstellungen
```

## 22.4 Browser

```text
┌ Wiki-Baum ──────────┬ Konzeptseite ───────────────┬ Status ───────┐
│ ▾ Beton             │ Expositionsklasse XC4       │ STABLE        │
│   ▾ Dauerhaftigkeit │                              │               │
│     XC1             │ XC4 beschreibt ...          │ Quellen: 3    │
│     XC2             │                              │ Human geprüft │
│     XC3             │ Verwandte Konzepte          │ aktuell       │
│   → XC4             │ • XC3                       │               │
│     XD1             │ • Betondeckung              │ [Quellen]     │
│ ▾ Mauerwerk         │ • Karbonatisierung          │ [Verlauf]     │
└─────────────────────┴──────────────────────────────┴───────────────┘
```

## 22.5 Review

```text
Wiki-Review · Proposal 12/18

XC4
+ neue Definition
+ zwei Originalquellen
+ Link zu Betondeckung
! normative Aussage

[a] annehmen  [r] ablehnen  [e] bearbeiten
[s] Quelle    [d] Diff      [n] nächste Änderung
```

## 22.6 Qualität

```text
OKF 0.2                   ✓
Konzeptseiten             384
Drafts                     18
Stale                       7
Konflikte                   3
Orphans                    11
Unaufgelöste Quellen        0
Unbelegte Claims            4
Beschädigte Links           2
```

## 22.7 Themes

Das Plugin liefert keine Farbcodes. Es verwendet semantische Rollen:

```text
normal
muted
accent
success
warning
error
selection
```

Dadurch funktionieren alle OmaRag-/Omarchy-Themes unverändert.

## 22.8 Tastaturkonflikte

`w` wird nur im NAV-Modus als Wiki-Kürzel verwendet. Im Textmodus bleibt es normale Eingabe. Der Host löst Konflikte mit anderen Plugins und zeigt die tatsächliche Belegung in der Fußzeile.

---

# 23. Hintergrundjobs, Checkpoints und Fortschritt

## 23.1 Jobtypen

```text
wiki.scan
wiki.compile-lite
wiki.verify
wiki.lint
wiki.reindex
wiki.export
wiki.import
wiki.deep-analysis       später
```

## 23.2 Prioritäten

```text
1. Nutzerfrage und Quellenanzeige
2. Haiku-Retrieval
3. manuell gestartete RAG-Indexierung
4. Wiki-Reviewaktionen
5. Wiki Lite
6. Wiki-Verifikation
7. Wiki-Lint
8. Wiki Deep
```

## 23.3 Checkpoints

Nach jeder stabilen Einheit:

- Dokument gescannt,
- Extraktionsbatch abgeschlossen,
- Kandidat aufgelöst,
- Proposal gespeichert,
- Claim verifiziert,
- Staging erfolgreich gelintet,
- Commit abgeschlossen.

Der Checkpoint enthält keine geheimen Modellinhalte, sondern IDs, Hashes und Status.

## 23.4 Fortschrittsmodell

```text
Scan               15 / 15 Dokumente
Extraktion         221 / 340 Chunks
Konzepte           74 Kandidaten
Auflösung          51 / 74
Verifikation       183 / 246 Claims
Proposal           12 Seiten · 41 Operationen
Gesamt             68,4 %
ETA                 9–14 min · Konfidenz mittel
```

Die ETA wird aus gemessenen Phasenraten berechnet. Vor ausreichender Messbasis zeigt OmaRag „wird kalibriert“.

## 23.5 Wiederaufnahme

Bei Pluginabsturz:

1. Host markiert Prozess als ausgefallen.
2. Job bleibt `recovering`.
3. Plugin wird neu gestartet.
4. `plugin.job.resume` erhält letzten Host- und Plugincheckpoint.
5. bereits bestätigte Einheiten werden anhand von Hashes übersprungen.

---

# 24. Konfiguration

## 24.1 Datei

```text
.omarag/plugins/de.omarag.omawiki/config.toml
```

## 24.2 Beispiel

```toml
schema_version = 1
enabled = true
language = "de"
mode = "lite"
auto_compile = "after_ingest"
review_policy = "manual"

[scope]
include_tags = []
exclude_tags = ["private-no-wiki"]
include_document_types = ["pdf", "docx", "html", "md"]

[content]
create_document_pages = true
create_glossary = true
create_concept_pages = true
create_comparisons = false
max_new_concepts_per_document = 20
minimum_claims_for_page = 2

[verification]
require_source_per_claim = true
strict_numeric_claims = true
strict_normative_claims = true
allow_partial_support = false

[models]
extractor_profile = "wiki-fast"
resolver_profile = "wiki-fast"
compiler_profile = "wiki-balanced"
verifier_profile = "wiki-balanced"

[git]
enabled = false
auto_commit_after_review = true

[privacy]
export_original_paths = false
```

## 24.3 Schema-getriebene Einstellungen

Das Plugin liefert ein JSON-Schema und UI-Metadaten. OmaRag rendert das Formular und ruft zur Validierung auf:

```text
plugin.settings.validate
plugin.settings.apply
```

Änderungen werden klassifiziert:

```text
live
plugin_restart
reindex_required
recompile_recommended
```

---

# 25. Öffentliche OmaRag-API für Plugins

Die externe OmaRag-API bleibt generisch:

```http
GET    /v1/plugins
POST   /v1/plugins/install
POST   /v1/plugins/{plugin_id}/update
POST   /v1/plugins/{plugin_id}/rollback
DELETE /v1/plugins/{plugin_id}

GET    /v1/workspaces/{workspace}/plugins
POST   /v1/workspaces/{workspace}/plugins/{plugin_id}/enable
POST   /v1/workspaces/{workspace}/plugins/{plugin_id}/disable
GET    /v1/workspaces/{workspace}/plugins/{plugin_id}/status

GET    /v1/workspaces/{workspace}/plugins/{plugin_id}/views/{view_id}
POST   /v1/workspaces/{workspace}/plugins/{plugin_id}/actions/{action_id}
POST   /v1/workspaces/{workspace}/plugins/{plugin_id}/commands/{command_id}
```

Für den Query Router wird intern die ausgehandelte `knowledge_provider`-Capability verwendet. Externe Clients müssen keine plugininternen Prozessdetails kennen.

---

# 26. OmaWiki-Kommandos

## 26.1 Über die OmaRag-CLI

```bash
omarag plugin install ./OmaWiki-0.1.0.omaplugin
omarag plugin enable de.omarag.omawiki --workspace Baustoffkunde
omarag plugin status de.omarag.omawiki

omarag wiki init
omarag wiki status
omarag wiki scan
omarag wiki compile --mode lite
omarag wiki jobs
omarag wiki review
omarag wiki lint
omarag wiki search "Expositionsklasse XC4"
omarag wiki export Baustoffkunde.okf.zip
```

## 26.2 Standalone-Werkzeuge des Pluginpakets

Folgende Funktionen arbeiten auch ohne laufendes OmaRag:

```bash
omawiki validate ./okf
omawiki lint ./okf --deterministic
omawiki index ./okf
omawiki export ./okf --output bundle.zip
omawiki inspect ./okf/concepts/beton/xc4.md
```

Nicht standalone möglich:

- Kompilierung aus Haiku-Dokumenten,
- Quellenauflösung,
- Hybridfragen,
- Modellbroker-Aufrufe,
- visuelle Originalfundstellen.

---

# 27. Installation, Signatur und Updates

## 27.1 Pluginpaket

Dateiendung:

```text
.omaplugin
```

Inhalt:

```text
de.omarag.omawiki-0.1.0.omaplugin
├── plugin.toml
├── package.json
├── wheels/
├── uv.lock
├── schemas/
├── prompts/
├── README.md
├── LICENSE
├── SBOM.spdx.json
├── checksums.sha256
└── signature.minisig
```

## 27.2 Installationsablauf

1. Paket und Quelle anzeigen.
2. Signatur und Prüfsummen prüfen.
3. Manifest validieren.
4. angeforderte Rechte anzeigen.
5. Kompatibilität prüfen.
6. neuen Runtime-Slot installieren.
7. Plugin im isolierten Testworkspace starten.
8. Healthcheck und Contract-Test ausführen.
9. Version registrieren.
10. Aktivierung pro Workspace separat anbieten.

## 27.3 Updates

- niemals während eines aktiven Pluginjobs umschalten,
- Queue zuerst pausieren oder leeren,
- Plugin-Status sichern,
- neue Version parallel installieren,
- Migration im Dry-Run prüfen,
- Restorepunkt anlegen,
- atomar umschalten,
- bei Fehler automatisch zurückrollen.

## 27.4 AppImage

Das OmaRag-AppImage enthält:

- Pluginmanager,
- Signaturprüfung,
- offiziellen Pluginkatalog,
- optional das OmaWiki-Manifest als Installationsvorschlag.

Es enthält standardmäßig nicht die vollständige OmaWiki-Runtime. Diese wird nach Zustimmung installiert. Für Offlineumgebungen kann das `.omaplugin`-Paket neben dem AppImage bereitgestellt werden.

## 27.5 Docker

Im Standard-Dockerbetrieb läuft der Pluginprozess im selben `omaragd`-Container als separater Prozess. Plugin und Kern bleiben paket- und prozessseitig getrennt.

Varianten:

```text
omarag-core             ohne OmaWiki
omarag-with-omawiki     optionales Convenience-Image
Plugin-Volume           nachträglich installierte Pakete
```

Ein eigener OmaWiki-Container mit Unix-Socket-Transport ist eine spätere Erweiterung, nicht Teil des MVP.

---

# 28. Git-Integration

Git ist optional.

## 28.1 Nutzen

- menschenlesbare Historie,
- Diffs,
- Wiederherstellung,
- Review außerhalb von OmaRag,
- Austausch des OKF-Bundles.

OKF empfiehlt Git als mögliche und besonders passende Distributionsform für Bundles.[^okf-bundle]

## 28.2 Commit nach Freigabe

```text
wiki: update XC4 and concrete-cover concepts

Sources:
- Betontechnologie.pdf, pages 112–114
- Tabellenbuch Bautechnik.pdf, page 118

Proposal: wiki-proposal-194
Plugin: de.omarag.omawiki@0.1.0
Reviewed-by: human:stephan
```

## 28.3 Keine Git-Pflicht

Ohne Git verwendet OmaWiki atomare Dateioperationen und OmaRag-Workspace-Snapshots.

---

# 29. Diagnose und Beobachtbarkeit

## 29.1 Logs

- stdout ausschließlich RPC,
- stderr JSON Lines,
- Korrelations-ID pro Job und RPC,
- keine Dokumentvolltexte in normalen Logs,
- keine Modellprompts ohne expliziten Diagnosemodus,
- keine API-Schlüssel oder Providerheader.

## 29.2 Metriken

Lokal erfasst:

```text
Dokumente und Chunks verarbeitet
Extraktionsdauer
Tokenverbrauch pro Rolle
Cachetreffer
Kandidaten pro Dokument
Claims supported/partial/unsupported/conflicting
Proposal-Akzeptanzrate
Lintbefunde
Indexgröße
Pluginneustarts
```

Keine Telemetrie wird automatisch übertragen.

## 29.3 Diagnosepaket

```text
plugin-manifest.json
plugin-health.json
settings.redacted.toml
state-schema.json
bundle-summary.json
lint-report.json
last-jobs.json
last-errors.log
compatibility.json
```

Dokumenttexte und vollständige Wikiseiten werden nur nach separater Auswahl aufgenommen.

---

# 30. Teststrategie

## 30.1 Pluginprotokoll

- Initialisierung und Capability-Negotiation,
- fehlende Rechte,
- unbekannte Methoden,
- Timeouts,
- Abbruch,
- Prozessabsturz,
- Heartbeat-Ausfall,
- Wiederaufnahme,
- inkompatible Protokollversion,
- fehlerhafte Manifestangaben.

## 30.2 OKF-Konformität

Fixtures für:

- gültiges OKF 0.2,
- gültiges minimales Bundle,
- OKF 0.1,
- unbekannte Zusatzfelder,
- unbekannte Typen,
- fehlende optionale Dateien,
- beschädigte Links als Warnung,
- fehlendes `type` als Fehler,
- ungültiges YAML,
- doppelte Quellen-IDs,
- nicht auflösbare Fußnoten,
- falsche Lebenszykluswerte.

## 30.3 Patchsicherheit

- Pfad-Traversal,
- Symlink-Ausbruch,
- Dateirennen,
- veralteter Basis-Hash,
- manuelle Abschnittsänderung,
- doppeltes Anwenden derselben Operation,
- Prozessabsturz während Staging und Commit,
- unbekannte Frontmatter-Felder bleiben erhalten.

## 30.4 Modellrobustheit

- ungültiges JSON,
- Halluzination nicht vorhandener Chunk-IDs,
- Prompt-Injection im Dokument,
- zu lange Ausgaben,
- doppelte Kandidaten,
- widersprüchliche Aussagen,
- leere Antwort,
- Modelltimeout,
- Modellwechsel bei gleichem Cachekey ausgeschlossen.

## 30.5 Quellenprüfung

- jede claimbezogene Fußnote löst auf,
- Seite und Dokument stimmen,
- Wikiindex wird bei Verifikation ausgeschlossen,
- entfernte Chunks markieren Claims als stale,
- unsupported Claims gelangen nicht in den Commit.

## 30.6 TUI

Golden-/Buffer-Tests für:

- Wiki-Browser,
- Review-Diff,
- Qualitätsansicht,
- 80×24, 100×30, 140×40 und 200×60,
- Fokus-, Werkbank- und Zen-Layout,
- alle Omarchy-Themes,
- NAV- und TEXT-Modus,
- fehlendes oder abgestürztes Plugin.

## 30.7 Ende-zu-Ende

1. OmaWiki installieren.
2. Rechte bestätigen.
3. Workspace aktivieren.
4. OKF-Bundle initialisieren.
5. Test-PDF über OmaRag indexieren.
6. Wiki-Lite-Job starten.
7. TUI schließen.
8. Pluginprozess und Job laufen weiter.
9. TUI erneut öffnen.
10. verpasste Ereignisse werden aus dem Host-Eventstore rekonstruiert.
11. Proposal prüfen.
12. Originalquelle und Seite öffnen.
13. Änderung freigeben.
14. OKF-Datei extern bearbeiten.
15. erneuten Vorschlag starten.
16. Dreiwegekonflikt korrekt anzeigen.
17. Hybridfrage stellen.
18. endgültige Zitate verweisen auf Originaldokumente.
19. Plugin deaktivieren.
20. OKF-Bundle bleibt vollständig lesbar.

---

# 31. Sicherheitsprüfungen

Verbindliche Tests:

- Plugin kann keine fremden Workspace-Dateien lesen.
- Plugin kann keine fremden Workspace-Dateien schreiben.
- Plugin kann ohne Grant keine Dokumentchunks anfordern.
- Plugin kann keine Cloudanfrage ohne Hostfreigabe auslösen.
- Plugin erhält keine API-Schlüssel.
- Plugin kann keinen Prozess starten.
- Plugin kann keine Shell verwenden.
- manipuliertes Pluginpaket wird abgelehnt.
- Signaturfehler verhindert Aktivierung.
- Modelloutput kann keinen Zielpfad einschleusen.
- Markdownlinks können das Bundle nicht über unerlaubte relative Pfade verlassen.
- aktive HTML-/Scriptinhalte werden nicht automatisch ausgeführt.
- ein Pluginabsturz beeinträchtigt nicht Haiku-RAG-Chat oder TUI.

---

# 32. Implementierungsroadmap

## Phase 0 – Entscheidungen und Vertragsspitze

Umfang:

- ADRs für Prozessmodell, Transport, Berechtigungen und UI-Beiträge,
- ORPP-1.0-Minimalschema,
- Fake-Host in Python,
- Fake-Plugin in Rust oder Python,
- bidirektionales JSON-RPC,
- Start, Healthcheck, Shutdown und Fehlerfall,
- Contract-Test im CI.

Abnahme:

```text
omaragd startet ein fremdes Testplugin,
handelt Fähigkeiten aus,
ruft eine Methode auf,
beendet den Prozess kontrolliert
und bleibt bei dessen Absturz stabil.
```

## Phase 1 – Pluginmanager und Isolation

Umfang:

- Paketformat `.omaplugin`,
- Manifestvalidator,
- Versionsslots,
- Installation, Deinstallation und Rollback,
- Rechteanzeige,
- workspacebezogene Aktivierung,
- Pluginprozess-Supervisor,
- Logcapture,
- optionaler Linux-Sandboxadapter.

Abnahme:

- Plugin kann ohne bestätigte Rechte nicht aktiviert werden.
- Update und Rollback funktionieren atomar.
- Nutzerinhalte werden bei Deinstallation nicht entfernt.

## Phase 2 – Deklarative Plugin-UI

Umfang:

- NavigationItem,
- Command,
- TreeView,
- TableView,
- MarkdownView,
- DiffView,
- FormView,
- ActionBar,
- generische Pluginroute im TUI,
- API für Viewdaten und Aktionen.

Abnahme:

- Ein Testplugin erzeugt eine vollständig per Pfeiltasten bedienbare Ansicht, ohne Ratatui-Code auszuführen.

## Phase 3 – Deterministischer OmaWiki-Kern

Umfang:

- OKF-0.2-Parser,
- tolerant lesendes Frontmattermodell,
- Validator und Linter,
- Index-/Log-Generator,
- FTS5-Index,
- Linkgraph,
- Import und Export,
- standalone `omawiki validate` und `omawiki lint`.

Abnahme:

- Ein fremdes OKF-0.2-Bundle kann gelesen, geprüft, durchsucht und unverändert wieder ausgegeben werden.
- Unbekannte Felder bleiben erhalten.

## Phase 4 – OmaWiki-TUI ohne LLM

Umfang:

- Wiki-Browser,
- Seitendetails,
- Quellenstatus,
- Qualitätsansicht,
- manuelle Seite erstellen und bearbeiten,
- externe Dateiänderungen erkennen,
- Pluginsettings.

Abnahme:

- OmaWiki ist bereits als nützlicher OKF-Browser und -Editor verwendbar, auch ohne Modell.

## Phase 5 – Hostdienste für Quellen und Modelle

Umfang:

- Dokumentlisten-, Struktur-, Chunk- und Suchmethoden,
- Citation Resolver,
- Modellbroker mit strukturierten Outputs,
- Cloudpolicy und Ressourcenwächter,
- Rollenprofile,
- Prompt- und Modellcache,
- Auditereignisse.

Abnahme:

- Das Plugin kann eine strukturierte Extraktion anfordern, ohne Ollama oder Haiku direkt zu kennen.

## Phase 6 – Wiki Lite Compiler

Umfang:

- Scan,
- Select,
- Extract,
- Resolve,
- typisierte Patchpläne,
- Verifikation,
- Staging,
- deterministischer Lint,
- Proposals,
- manuelle Reviewfreigabe,
- atomarer Commit.

Abnahme:

- Ein indexiertes Fachbuch erzeugt reviewbare Draft-Konzeptseiten mit auflösbaren Originalquellen.
- Unsupported Claims werden nicht übernommen.

## Phase 7 – Hintergrundbetrieb und Wiederaufnahme

Umfang:

- Plugin-Jobprovider,
- Checkpoints,
- Fortschrittsphasen,
- ETA,
- Pause und Fortsetzen,
- Prozessneustart,
- TUI-Reconnect über zentralen Eventstore.

Abnahme:

- TUI kann während eines Compilerjobs geschlossen werden.
- Der Job läuft weiter oder pausiert gemäß Policy.
- Nach Rückkehr ist der vollständige Zustand sichtbar.

## Phase 8 – Knowledge Provider und Hybridmodus

Umfang:

- Wiki-FTS-Suche,
- KnowledgeHit-Modell,
- Quellhinweise,
- Query-Router-Integration,
- Wiki- und Hybridmodus,
- Originalzitate als Endbelege,
- Selbstverstärkungsschutz.

Abnahme:

- Eine Hybridfrage nutzt Wikiwissen zur Orientierung, zitiert jedoch ausschließlich Originalquellen aus Haiku RAG.

## Phase 9 – Verpackung und Releasehärtung

Umfang:

- signiertes `.omaplugin`,
- SBOM,
- AppImage-Installation,
- Docker-Convenience-Image,
- Updatekanal,
- Kompatibilitätsmatrix,
- Diagnosepaket,
- Dokumentation und Beispielworkspace.

Abnahme:

- OmaWiki kann separat installiert, aktualisiert, zurückgerollt und entfernt werden.

## Phase 10 – OmaWiki Deep

Später:

- semantischer Wikiindex,
- Vergleichsseiten,
- tiefe Konfliktanalyse,
- Versionswissen,
- Graphansicht,
- Git-Review,
- Atlas- und MCP-Beiträge,
- Lückenanalyse.

---

# 33. MVP-Abgrenzung

## 33.1 OmaWiki 0.1 muss enthalten

- eigenständiges signierbares Pluginpaket,
- eigener Prozess,
- ORPP-Handshake,
- workspacebezogene Rechte,
- OKF-0.2-Bundle,
- Parser, Validator und Lint,
- Browser und Qualitätsansicht,
- FTS-Suche,
- Quellenreferenzseiten,
- Wiki Lite für ausgewählte Dokumente,
- strukturierte Extraktion,
- typisierte Patchoperationen,
- Quellenverifikation,
- Proposal und manueller Review,
- atomarer Commit,
- Hintergrundjob und Wiederaufnahme,
- Hybrid-Kontextübergabe an OmaRag,
- Import und Export,
- Deinstallation ohne Datenverlust.

## 33.2 Nicht Teil von 0.1

- Plugin-Marktplatz für beliebige Drittanbieter,
- eigener Agentenloop,
- automatische tiefgreifende Taxonomieänderungen,
- automatische Konfliktauflösung,
- physisches automatisches Löschen,
- eigener Ollama- oder Cloudclient,
- direkter LanceDB-Zugriff,
- semantischer Wiki-Vektorindex,
- separater OmaWiki-Systemdienst,
- eigener MCP-Server,
- beliebige TUI-Widgets oder HTML-UIs,
- Mehrbenutzer-RBAC.

---

# 34. Abnahmekriterien für OmaWiki 0.1

OmaWiki 0.1 gilt als fertig, wenn:

1. das Plugin in einem eigenen Prozess läuft,
2. `omaragd` bei Pluginabsturz stabil bleibt,
3. das Plugin separat installierbar und entfernbar ist,
4. Plugin und Host Versionen und Capabilities aushandeln,
5. alle Rechte vor Aktivierung sichtbar sind,
6. das Plugin ohne Netzwerkrecht arbeitet,
7. das Plugin weder Haiku noch Ollama direkt aufruft,
8. das OKF-Bundle auch ohne OmaRag lesbar ist,
9. OKF 0.2 korrekt geschrieben wird,
10. unbekannte Frontmatter-Felder erhalten bleiben,
11. jede generierte fachliche Aussage auf eine Source-ID verweist,
12. jede Source-ID auf ein Originaldokument und möglichst Seite/Chunk auflösbar ist,
13. unsupported Claims nicht committed werden,
14. LLM-Ausgaben keine freien Dateipfade bestimmen,
15. manuell geänderte Abschnitte nicht still überschrieben werden,
16. neue LLM-Seiten zunächst als `draft` angelegt werden,
17. Review-Diffs im TUI bedienbar sind,
18. ein Wiki-Job nach TUI-Schließung fortgesetzt werden kann,
19. ein Pluginprozess nach Absturz aus einem Checkpoint wiederaufnimmt,
20. Wiki- und Haiku-Quellenindex logisch getrennt bleiben,
21. Hybridantworten Originaldokumente zitieren,
22. Plugin-Deinstallation das OKF-Bundle nicht löscht,
23. Update und Rollback atomar funktionieren,
24. AppImage und Docker das Plugin installieren können,
25. ein deterministischer Diagnose- und Lintbericht erzeugt werden kann.

---

# 35. Empfohlene Epics

## EPIC P1 – Plugin Host Foundation

- Manifestmodell
- Paketinstaller
- Version-Slots
- Signaturprüfung
- Process Supervisor
- ORPP
- Berechtigungen
- Workspaceaktivierung

## EPIC P2 – Plugin UI SDK Preview

- Standardwidgets
- Viewmodelle
- Navigation
- Commands
- Actions
- Settingsschema
- Themeintegration

## EPIC W1 – OKF Core

- Parser
- Roundtrip-Frontmatter
- Validator
- Linter
- Index/Log
- FTS
- Graph
- Import/Export

## EPIC W2 – Source Bridge

- Dokumente
- Struktur
- Chunks
- Suche
- Citations
- Source-Reference-Pages

## EPIC W3 – Model Broker

- Rollen
- Structured Output
- Cache
- Ressourcenpolicy
- Cloudpolicy
- Fehlerbehandlung

## EPIC W4 – Wiki Lite Compiler

- Scan
- Select
- Extract
- Resolve
- Patch Plan
- Verify
- Stage
- Commit

## EPIC W5 – Review und Qualität

- Proposal Queue
- Diff
- Quellenansicht
- Human Verify
- Stale
- Konflikte
- Lint Cockpit

## EPIC W6 – Knowledge Provider

- Wiki Search
- Related Concepts
- Source Hints
- Query Router
- Hybridmodus

## EPIC W7 – Hintergrundbetrieb

- Jobs
- Checkpoints
- ETA
- Pause/Resume
- Crash Recovery
- Event Replay

## EPIC W8 – Distribution

- `.omaplugin`
- SBOM
- AppImage
- Docker
- Update/Rollback
- Dokumentation

---

# 36. Architecture Decision Records

## ADR-OW-001: OmaWiki ist ein separater Prozess

Begründung: Isolation, unabhängige Abhängigkeiten, sprachneutrale Erweiterbarkeit und sichere Updates.

## ADR-OW-002: ORPP verwendet JSON-RPC über stdio

Begründung: kein Port, hostkontrollierter Lebenszyklus, einfache lokale Sicherheit und Testbarkeit.

## ADR-OW-003: OmaWiki besitzt keine direkten Haiku- oder Ollama-Zugriffe

Begründung: Datenschutz, Ressourcensteuerung, Cloudfreigaben und Kompatibilität bleiben zentral in OmaRag.

## ADR-OW-004: OKF ist die dauerhafte Quelle der Wahrheit

Begründung: Portabilität, Git-Fähigkeit, Lesbarkeit und Unabhängigkeit vom Plugin.

## ADR-OW-005: SQLite ist vollständig rekonstruierbar

Begründung: technische Indizes dürfen Nutzerwissen nicht einschließen, das nur dort existiert.

## ADR-OW-006: Modelloutputs werden in typisierte Operationen übersetzt

Begründung: Dateisicherheit, Reviewbarkeit und deterministische Validierung.

## ADR-OW-007: LLM-Inhalte beginnen als Proposal und Draft

Begründung: Fachwissen und Normangaben benötigen nachvollziehbare Kontrolle.

## ADR-OW-008: Quellen- und Wikiindex sind getrennt

Begründung: Verhindert Selbstbelege und rekursive Halluzinationsverstärkung.

## ADR-OW-009: Plugin-UI ist deklarativ

Begründung: TUI-Konsistenz, Themes, Tastaturbedienung und Prozesssicherheit.

## ADR-OW-010: Ein Pluginprozess wird pro Workspace gestartet

Begründung: engere Dateirechte, weniger Datenvermischung und einfachere Fehlerzuordnung.

## ADR-OW-011: OmaWiki 0.1 verwendet FTS5 statt eigenem Vektorindex

Begründung: schlanker MVP, geringe Abhängigkeiten und klare Trennung zum Quellen-RAG.

## ADR-OW-012: MCP ist nicht das interne Pluginprotokoll

Begründung: OmaWiki benötigt zusätzlich Lebenszyklus, UI-Beiträge, Jobcheckpoints und bidirektionale Hostdienste. Eine spätere MCP-Fassade kann auf dem stabilen Pluginvertrag aufbauen.

---

# 37. Hauptrisiken und Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
|---|---|
| Plugin-API wird zu früh zu breit | nur fünf Capability-Typen, OmaWiki als Referenz, versionierte Negotiation |
| Wiki halluziniert Inhalte | Claimobjekte, Originalbelege, Verifier, Draft und Review |
| Wiki bestätigt sich selbst | getrennte Indizes, `corpus=source`, Provider-Ausschluss |
| Nutzeränderungen gehen verloren | Managed-Section-Hashes, Optimistic Concurrency, Dreiwegevergleich |
| OKF entwickelt sich weiter | toleranter Reader, exakte Schreibversion, unbekannte Felder erhalten |
| Plugin benötigt zu viel RAM | Modellbroker, ein Modell zur Zeit, Wiki Lite, Caching und Checkpoints |
| Pluginprozess stürzt ab | Supervisor, zentrale Queue, Heartbeats und Resume |
| manipuliertes Pluginpaket | Signatur, Prüfsummen, SBOM und Runtime-Slots |
| Dokument enthält Prompt-Injection | Quelltext als Daten, keine Tools, JSON-Schema, deterministischer Renderer |
| zu viele ähnliche Seiten | Resolver, Aliasse, Mindestkriterien und Review |
| Quellen ändern sich | Hashgraph, Stale-Markierung und gezielte Neuverifikation |
| Plugin-Deinstallation zerstört Wissen | OKF außerhalb Pluginstatus; getrennte Löschbestätigung |
| TUI wird durch Plugin unübersichtlich | Hostwidgets, Navigationsreihenfolge, kontextabhängige Commands |

---

# 38. Empfohlene Veröffentlichungsfolge

## OmaRag 0.4

- Plugin Host Preview
- ORPP 1.0
- Pluginmanager
- Prozessisolierung
- deklarative UI
- Modell- und Quellenbroker

## OmaWiki 0.1

- OKF-Browser und Lint
- Wiki Lite
- Quellenverifikation
- Review
- FTS
- Hintergrundjobs
- Hybrid-Kontext

## OmaWiki 0.2

- ausgewogene Auto-Review-Policy
- bessere Entitätsauflösung
- Stale- und Konfliktworkflow
- Git-Integration
- erweiterte Diagnose

## OmaWiki 0.3

- semantischer Wikiindex
- Vergleichsseiten
- Graphansicht
- Wiki Deep
- Atlas- und MCP-Beiträge

---

# 39. Endgültige Empfehlung

OmaWiki sollte nicht als eingebautes OmaRag-Untermodul beginnen. Als erstes eigenständiges Plugin erzwingt es früh die richtigen Grenzen:

```text
OmaRag Core
= Betrieb, Sicherheit, Modelle, RAG, Jobs und TUI

OmaWiki Plugin
= Wissen organisieren, prüfen, reviewen und als OKF pflegen

OKF-Bundle
= portabler, menschenlesbarer und langfristiger Wissensbestand
```

Der wichtigste Qualitätsmechanismus lautet:

> **Das Modell darf Wissen vorschlagen. Der deterministische Compiler entscheidet, wie es gespeichert wird. Originalquellen entscheiden, ob es belegt ist. Der Nutzer entscheidet, ob es freigegeben wird.**

Damit wird OmaWiki nicht nur eine zusätzliche Funktion, sondern ein belastbarer Referenzfall für das gesamte künftige OmaRag-Pluginökosystem.

---

# Quellen und technische Referenzen

[^haiku-changelog]: Haiku RAG Changelog, Version 0.70.0 vom 25. Juli 2026: https://ggozad.github.io/haiku.rag/changelog/
[^haiku-python]: Haiku RAG Python API, hybride Suche, Kontextausweitung, Bildsuche und öffentliche Import-/Verarbeitungsmethoden: https://ggozad.github.io/haiku.rag/python/
[^okf-spec]: Open Knowledge Format Specification 0.2: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
[^okf-conformance]: OKF 0.2, Konformitäts- und Vorwärtskompatibilitätsregeln, insbesondere toleranter Umgang mit optionalen Feldern, unbekannten Typen und zusätzlichen Schlüsseln: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md#11-conformance
[^okf-lifecycle]: OKF 0.2, Lebenszyklusfelder `status` und `stale_after`: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
[^okf-sources]: OKF 0.2, `sources` und claimbezogene Fußnoten: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
[^okf-trust]: OKF 0.2, Trennung von `generated` und `verified`: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
[^okf-bundle]: OKF 0.2, Bundle-Struktur, `index.md`, `log.md` und Git als empfohlene Distributionsform: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
