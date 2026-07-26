---
title: "OmaRag – vollständiger Implementations- und Designplan"
project: "OmaRag"
version: "0.3"
status: "Konsolidierte Gesamtspezifikation"
date: "2026-07-25"
language: "de"
basis:
  haiku_rag: "0.70.0"
  tui: "Rust + Ratatui"
  inference: "Ollama-first, weitere Provider optional"
document_scope:
  - Architektur
  - API
  - TUI
  - Betrieb
  - Qualität
  - Distribution
  - Roadmap
---

# OmaRag – vollständiger Implementations- und Designplan v0.3

## Projektsteckbrief

| Merkmal | Festlegung |
|---|---|
| Projektname | **OmaRag** |
| Zweck | Lokale, hardwarebewusste und überprüfbare RAG-Werkstatt für Haiku RAG |
| Erstes Frontend | Terminal User Interface mit **Rust + Ratatui** |
| RAG-Grundlage | **Haiku RAG**, Planungsbasis `0.70.0` |
| Integrationsprinzip | Haiku RAG bleibt unverändert und wird ausschließlich über öffentliche APIs angebunden |
| Architektur | **API-first**, Frontend, Betriebslogik und RAG-Kern strikt getrennt |
| Backend-Dienst | Einziger kleiner, persistenter Prozess `omaragd` |
| Transport | HTTP/JSON für Befehle, wiederaufnehmbare Server-Sent Events für Streams |
| Zentrale Einheit | **Workspace** mit eigener Konfiguration, Datenbank, Quellen, Tests und Backups |
| Inferenz | Ollama lokal als Standard; Cloud- und Remoteprovider nur bewusst aktiviert |
| Speicher | LanceDB ausschließlich über Haiku RAG; SQLite-WAL für Queue, Events und Betriebsdaten |
| Bedienung | Drei Layouts, zwei Bedienebenen, vollständige Pfeiltastensteuerung und Merktasten |
| Qualität | Testindexierung, Corpus-Profiler, Belegmodus, Doctor, Regressionstests und A/B-Labor |
| Hintergrundbetrieb | Indexierung, Downloads und Wartung laufen ohne geöffnetes TUI weiter |
| Distribution | AppImage, Docker Compose und native CLI |
| Spätere Frontends | Atlas, Weboberfläche, Desktop-GUI, MCP-Clients |
| Nicht-Ziel | Eigenes RAG, eigene Modellruntime, allgemeines Agentenframework oder frühe Mehrbenutzerplattform |

> **Projektkern:** OmaRag ist kein bloßer Wrapper um eine bestehende CLI. Es ist eine stabile, frontendunabhängige Betriebs- und Qualitätsschicht für Haiku RAG. Das Ratatui-TUI ist der erste Client, nicht der Kern des Systems.

---

## Änderungen in v0.3

Version `0.3` führt den bisherigen Architektur- und Betriebsplan zu einer vollständigen Produktspezifikation zusammen. Neu verbindlich aufgenommen werden:

1. **Workspaces** als zentrale, portable Einheit für Wissensbestände
2. zwei unabhängige Bedienebenen **Einfach** und **Werkstatt**
3. **Indexierungs-Testlauf** mit ausgewählten repräsentativen Seiten
4. **Corpus Profiler** für Dokumenttyp, OCR-Anteil, Tabellen, Bilder, Sprache und Duplikate
5. **Kapazitätsplaner** für dauerhaften und temporären Speicherbedarf
6. **Ressourcenwächter** mit CPU-, RAM-, Temperatur-, Akku- und Ruhezeitregeln
7. persistente Ereignisse, **SSE-Replay**, Snapshots und idempotente Befehle
8. sichere **Datenbank-Tags**, externe Backups und Rollback vor riskanten Änderungen
9. ein strukturiertes **Qualitätscockpit**
10. workspacebezogene **Regressionstests** für Retrieval, Antwort und Zitate
11. nachvollziehbare **Belegmodi** statt eines irreführenden KI-Konfidenzwerts
12. ein dauerhafter **Quellenmanager**
13. Metadaten für Gültigkeit, Ausgaben und ersetzte Dokumente
14. gleichwertige lokale, Docker- und Remote-Backendprofile
15. ein reproduzierbares **A/B-Labor** für Modelle und RAG-Einstellungen
16. eine schlanke, skriptfähige **CLI**
17. ein datenschutzbereinigtes **Diagnosepaket**
18. eine optionale **OmaRag-MCP-Fassade** vor Haikus vorhandener MCP-Funktion
19. ein von Anfang an berechtigungsorientiertes Pluginmodell
20. kleine Komfortfunktionen wie Export, angeheftete Quellen und Desktopmeldungen

Die Erweiterungen verändern das Grundprinzip nicht:

- Haiku RAG wird nicht geforkt.
- LanceDB wird nicht direkt aus Rust beschrieben.
- `omaragd` bleibt der einzige OmaRag-Hintergrundprozess.
- Empfehlungen bleiben beratend.
- Local-first, Nachvollziehbarkeit und Wiederherstellbarkeit haben Vorrang vor Funktionsfülle.

### Planungsreferenz

Am **25. Juli 2026** ist Haiku RAG `0.70.0` die aktuelle veröffentlichte Version. Sie ergänzt Bildanhänge für Fragen und Analysen. Die in `0.69.0` eingeführten nativen Pydantic-AI-Capabilities bleiben für die Adapterarchitektur besonders relevant. OmaRag bindet dennoch keine feste Versionsnummer in das Frontend ein, sondern verwendet Capability-Erkennung und eine gepflegte Kompatibilitätsmatrix.

---

# 1. Zielbild

**OmaRag** wird ein schlankes, lokales und API-zentriertes Frontend für Haiku RAG:

- **Rust + Ratatui** für das TUI
- **Haiku RAG bleibt unverändert**
- keine direkte Abhängigkeit des TUI von Haiku-internen Python-Klassen
- keine Auswertung formatierter CLI-Ausgaben
- vollständige Bedienung der Haiku-RAG-Konfiguration
- austauschbares Frontend durch eine stabile **OmaRag API**
- lokaler Betrieb als Standard
- Cloudanbieter nur nach sichtbarer und technischer Freigabe
- Erweiterbarkeit für Atlas, GUI, Weboberfläche und spätere Plugins

Die wichtigste Architekturentscheidung lautet:

> **OmaRag wird kein bloßer Ratatui-Wrapper um die Haiku-CLI, sondern ein API-first-System mit einem kleinen Haiku-Adapterdienst.**

Das TUI ist damit lediglich ein Client. Später können eine Atlas-Extension, eine Weboberfläche oder eine Desktop-GUI dieselbe API verwenden.

---

# 2. Versionsbasis und Kompatibilitätsstrategie

Zum Planungsstand vom **25. Juli 2026** ist Haiku RAG `0.70.0` die aktuelle veröffentlichte Grundlage. Version `0.70.0` ergänzt Bildanhänge für `ask` und `analyze`; Version `0.69.0` stellte den Kern auf native, deferred Pydantic-AI-Capabilities um und entfernte die frühere Skill- und Sub-Agent-Schicht.

Für OmaRag gilt:

| Bereich | Festlegung |
|---|---|
| Entwicklungsziel | Haiku RAG `0.70.x` |
| erster Kompatibilitätsadapter | `haiku-v070` |
| Übergangsadapter | `haiku-v069` |
| optionaler Fallback | `0.68.x`, nur soweit ohne Sonderarchitektur vertretbar |
| Integration | ausschließlich über öffentliche Haiku-Python-APIs |
| Versionserkennung | beim Start über Paketmetadaten und Capability-Handshake |
| Frontendvertrag | unabhängig von konkreten Haiku-Datenklassen |
| Paketbindung | freigegebener Versionsbereich plus Lockfile |
| Updates | erst planen, dann prüfen, sichern, anwenden und gegebenenfalls zurückrollen |
| Datenbanken | Migrationen niemals implizit durch das TUI erzwingen |

OmaRag darf weder rohe Pydantic-AI-Ereignisse noch interne Haiku-Datenklassen über seine öffentliche API ausgeben. Der Adapter übersetzt sie in stabile OmaRag-Domainmodelle.

### 2.1 Capability-Erkennung statt Versionslogik

Das TUI fragt Funktionen ab:

```json
{
  "streaming_chat": true,
  "question_images": true,
  "analysis_images": true,
  "visual_grounding": true,
  "multimodal_reranking": true,
  "database_tags": true,
  "evaluation": true
}
```

Das TUI darf nicht selbst ableiten:

```text
haiku_version >= 0.70 → Bildanhänge erlauben
```

Stattdessen entscheidet ausschließlich die vom Adapter gelieferte Capability-Angabe. So kann OmaRag auch Backports, optionale Extras und künftige API-Änderungen korrekt behandeln.

### 2.2 Update-Regel

Vor jeder neuen OmaRag-Version und vor jedem vom Nutzer angeforderten Haiku-Update wird geprüft:

1. Welche Haiku-RAG-Version ist aktuell und von OmaRag freigegeben?
2. Haben sich öffentliche Python-APIs oder Konfigurationsmodelle verändert?
3. Sind neue oder entfernte Capabilities vorhanden?
4. Sind Datenbankmigrationen notwendig?
5. Ist ein Rebuild erforderlich oder genügt eine Migration?
6. Funktionieren die OmaRag-Contract- und Regressionstests?
7. Kann auf den vorherigen Runtime-Slot zurückgeschaltet werden?

Die OmaRag API bleibt stabil. Versionsänderungen von Haiku sollen im Regelfall nur Adapter, Kompatibilitätsmatrix und Tests betreffen.

### 2.3 Kompatibilitätsmatrix

```yaml
haiku:
  "0.70":
    adapter: haiku-v070
    support: preferred
    capabilities:
      question_images: true
      analysis_images: true
      native_capabilities: true

  "0.69":
    adapter: haiku-v069
    support: compatible
    capabilities:
      question_images: false
      native_capabilities: true

  "0.68":
    adapter: haiku-v068
    support: best-effort
    capabilities:
      multimodal_reranking: true
      native_capabilities: false
```

Die Matrix wird signiert ausgeliefert, kann aktualisiert werden und besitzt immer einen Offline-Fallback.

---
# 3. Zielarchitektur

```text
┌──────────────────────────────────────────────────────────────────────┐
│                         OmaRag Clients                              │
│                                                                      │
│ Ratatui-TUI │ CLI │ Atlas │ spätere GUI/Web-App │ MCP-Fassade       │
│             gemeinsamer omarag-domain + OmaRagClient                 │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
            HTTP/JSON + wiederaufnehmbare SSE, API v1
       Loopback │ Unix Socket │ SSH-Tunnel/VPN │ Containernetz
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│                           omaragd                                   │
│              einziger kleiner OmaRag-Hintergrundprozess              │
│                                                                      │
│ API Gateway             │ Auth/Policy          │ Workspace Manager  │
│ Config/Secrets          │ Queue/Scheduler      │ Event Store        │
│ Resource Governor       │ Dependency Manager  │ Backend Profiles   │
│ Hardware Profiler       │ Model Catalog        │ Capacity Planner   │
│ Corpus Profiler         │ Quality/Evaluation   │ Backup Manager     │
│ Source Manager          │ Media Selector       │ Diagnostics        │
├──────────────────────────────────────────────────────────────────────┤
│                         Haiku Adapter                               │
│                                                                      │
│ haiku-v070 │ haiku-v069 │ Capability Map │ Event Normalizer         │
│ öffentliche Pipelineprimitive │ Ingester-Adapter │ MCP-Adapter       │
└───────────────┬───────────────────────────────┬──────────────────────┘
                │                               │
┌───────────────▼────────────────┐  ┌───────────▼──────────────────────┐
│           Haiku RAG            │  │      Inferenz-Provider           │
│                                │  │                                  │
│ Docling · Chunking · LanceDB   │  │ Ollama lokal · vLLM · APIs      │
│ Retrieval · Reranking · QA     │  │ Chat · Embed · Rerank · Vision  │
│ Doctor · Tags · Evaluation     │  │                                  │
└────────────────────────────────┘  └──────────────────────────────────┘
```

Haiku RAG bleibt für Konvertierung, Chunking, Embeddings, LanceDB, hybride Suche, Reranking, Kontextausweitung, Quellen, visuelle Verankerung und Antwortfähigkeiten zuständig. OmaRag ergänzt keine konkurrierende RAG-Engine, sondern eine robuste Betriebs-, Beratungs-, Darstellungs- und Qualitätsschicht.

## 3.1 Workspace als zentrale Domäneneinheit

Jeder Wissensbestand wird als Workspace behandelt:

```text
Workspace
├── Identität und Manifest
├── Haiku-Konfiguration
├── LanceDB-Datenbank
├── Quellen und Synchronisationsregeln
├── Dokument-Metadatenoverlays
├── Modell- und Verarbeitungsprofile
├── Sessions
├── Queuebezug und Jobhistorie
├── Qualitäts- und Regressionstests
├── Snapshots und Backups
└── Datenschutz- und Backendrichtlinien
```

Globale Komponenten wie Ollama, Haiku-Runtime, Modellkatalog und Hardwareinventar werden nicht dupliziert. Workspacegebunden sind dagegen alle Einstellungen, die Reproduzierbarkeit oder Datenidentität beeinflussen.

## 3.2 Nur ein OmaRag-Hintergrundprozess

`omaragd` ist ein adaptiver Daemon:

- Das TUI oder die CLI startet ihn automatisch, wenn kein geeigneter Dienst läuft.
- Solange ein Indexierungs-, Download-, Backup-, Evaluations- oder Wartungsjob aktiv ist, bleibt er nach dem Schließen eines Clients am Leben.
- Ohne Jobs und Clients kann er sich nach konfigurierbarer Leerlaufzeit beenden.
- Im integrierten Modus läuft er als `systemd --user`-Dienst.
- In Docker ist er der Hauptprozess des Backend-Containers.
- Es wird kein zweiter OmaRag-Worker-Prozess als Pflichtbestandteil eingeführt.
- Interne Worker dürfen als Tasks oder kontrollierte Subprozesse existieren, gehören aber zum Lebenszyklus von `omaragd`.

Der vorhandene Haiku-Ingester kann für kontinuierliche Quellen verwendet werden. Für instrumentierte manuelle Importe verwendet OmaRag öffentliche Pipelineprimitive, um Fortschritt, Checkpoints und Fehlerphasen präzise abzubilden.

## 3.3 Verantwortungsgrenzen

| Komponente | Verantwortlich | Nicht verantwortlich |
|---|---|---|
| `omarag` TUI | Darstellung, Eingabe, Navigation, lokale UI-Präferenzen | Queue, Modelle, PDF-Verarbeitung |
| `omarag` CLI | Skriptfähige Befehle und JSON-Ausgabe | eigene Geschäftslogik |
| `omaragd` | API, Workspaces, Jobs, Updates, Hardware, Qualität, Backups, Medien | Terminalrendering |
| Haiku-Adapter | Versionsübersetzung und Capability-Normalisierung | UI-Entscheidungen |
| Haiku RAG | Dokumentpipeline, Retrieval, Reranking, QA, LanceDB | Paketinstallation und Layouts |
| Ollama/Provider | Modellverwaltung und Inferenz | RAG- und Workspacezustand |
| SQLite | Queue, Ereignisse, Betriebsdaten, Historie | Vektor- und Dokumentretrieval |
| LanceDB | Wissensbestand über Haiku RAG | direkter Zugriff durch Rust-Clients |

## 3.4 Warum weiterhin ein Python-Adapter?

1. Haiku RAG und Pydantic AI bleiben in ihrer nativen Python-Umgebung.
2. Öffentliche Haiku-APIs können ohne CLI-Parsing verwendet werden.
3. Versionswechsel bleiben im Adapter isoliert.
4. Rust benötigt weder PyO3 noch einen eingebetteten Python-Interpreter.
5. TUI, CLI, Atlas und weitere Frontends verwenden denselben Vertrag.
6. Ein Python- oder Modellfehler beendet nicht automatisch das Terminal.
7. Haikus Pydantic-Konfigurationsschema kann direkt exportiert und validiert werden.
8. Doctor, Tags, Migrationen und Evaluationsfunktionen bleiben originalgetreu nutzbar.

## 3.5 Lokale und entfernte Topologien

### Einzelplatz

```text
TUI ── Loopback/Unix Socket ── omaragd ── Haiku + Ollama
```

### Host-TUI mit Docker-Backend

```text
AppImage-TUI ── 127.0.0.1 ── Docker: omaragd + Ollama
```

### Notebook mit Server-Backend

```text
TUI auf Yoga ── SSH-Tunnel oder VPN ── omaragd auf CPU-Server
```

### Atlas

```text
Atlas-Extension ── OmaRag API oder MCP-Fassade ── omaragd
```

Die Hardware- und Modellberatung bezieht sich stets auf das **Backend**, nicht auf den Rechner, auf dem das TUI angezeigt wird.

## 3.6 Bewusst nicht verwenden

- Parsing formatierter Haiku-CLI-Ausgaben als reguläre Schnittstelle
- direkte LanceDB-Schreibzugriffe aus Rust
- stilles Verändern der System-Python-Installation
- automatisches `sudo` ohne sichtbaren Installationsplan
- ein zweiter permanenter OmaRag-Dienst neben `omaragd`
- hart codierte, schnell veraltende Modellbestenlisten
- blockierende Bilddekodierung oder Netzwerkzugriffe im Ratatui-Renderpfad
- ungeprüfte Plugins im Hauptprozess
- exakte, aber unzuverlässige ETA-Angaben
- ein einzelner, scheinbar präziser „KI-Vertrauenswert“

---
# 4. Projektstruktur

```text
omarag/
├── Cargo.toml
├── rust/
│   ├── crates/
│   │   ├── omarag-domain/          # stabile, frontendunabhängige Typen
│   │   ├── omarag-client/          # HTTP/SSE/Unix-Socket-Clients
│   │   ├── omarag-app/             # State, Actions, Reducer, Effects
│   │   ├── omarag-tui/             # Ratatui-Komponenten
│   │   ├── omarag-cli/             # nichtinteraktive CLI
│   │   ├── omarag-launcher/        # Daemonstart und Backendprofile
│   │   ├── omarag-hardware/        # Inventar und Backendmesswerte
│   │   ├── omarag-theme/           # semantische Themes, Omarchy
│   │   ├── omarag-media/           # Terminalbilder und Viewer
│   │   ├── omarag-bootstrap/       # AppImage, Runtime, Dienstintegration
│   │   └── omarag-updater/         # Updatepläne und Signaturprüfung
│   └── bins/
│       ├── omarag/                  # TUI
│       └── omarag-cli/              # CLI-Einstieg
│
├── python/
│   ├── pyproject.toml
│   └── omarag_bridge/
│       ├── __init__.py
│       ├── main.py
│       ├── api/
│       │   ├── meta.py
│       │   ├── workspaces.py
│       │   ├── config.py
│       │   ├── databases.py
│       │   ├── documents.py
│       │   ├── sources.py
│       │   ├── search.py
│       │   ├── runs.py
│       │   ├── sessions.py
│       │   ├── jobs.py
│       │   ├── events.py
│       │   ├── media.py
│       │   ├── hardware.py
│       │   ├── models.py
│       │   ├── quality.py
│       │   ├── evaluations.py
│       │   ├── backups.py
│       │   ├── dependencies.py
│       │   ├── diagnostics.py
│       │   └── backends.py
│       ├── adapters/
│       │   ├── base.py
│       │   ├── haiku_v068.py
│       │   ├── haiku_v069.py
│       │   └── haiku_v070.py
│       ├── services/
│       │   ├── workspace_service.py
│       │   ├── config_service.py
│       │   ├── database_service.py
│       │   ├── document_service.py
│       │   ├── source_service.py
│       │   ├── session_service.py
│       │   ├── job_service.py
│       │   ├── event_service.py
│       │   ├── capability_service.py
│       │   ├── quality_service.py
│       │   ├── backup_service.py
│       │   └── diagnostic_service.py
│       ├── jobs/
│       │   ├── store.py
│       │   ├── scheduler.py
│       │   ├── state_machine.py
│       │   ├── checkpoints.py
│       │   └── idempotency.py
│       ├── pipeline/
│       │   ├── preflight.py
│       │   ├── corpus_profiler.py
│       │   ├── instrumented_ingest.py
│       │   ├── retrieval.py
│       │   └── evidence.py
│       ├── progress/
│       │   ├── work_units.py
│       │   ├── estimator.py
│       │   └── history.py
│       ├── hardware/
│       │   ├── inventory.py
│       │   ├── resource_governor.py
│       │   └── runtime_metrics.py
│       ├── catalog/
│       │   ├── models.py
│       │   ├── compatibility.py
│       │   └── signatures.py
│       ├── media/
│       │   ├── candidates.py
│       │   ├── ranking.py
│       │   └── thumbnails.py
│       ├── quality/
│       │   ├── doctor.py
│       │   ├── regression.py
│       │   ├── ab_lab.py
│       │   └── citation_checks.py
│       ├── backup/
│       │   ├── tags.py
│       │   ├── export.py
│       │   └── restore.py
│       ├── runtime/
│       │   ├── dependencies.py
│       │   ├── process_manager.py
│       │   ├── service_manager.py
│       │   └── update_manager.py
│       └── models/
│           ├── api.py
│           ├── domain.py
│           ├── events.py
│           └── errors.py
│
├── api/
│   ├── openapi.snapshot.json
│   ├── events.schema.json
│   ├── workspace.schema.json
│   ├── backup.schema.json
│   └── compatibility.md
│
├── catalog/
│   ├── models.schema.json
│   ├── models.fallback.json
│   ├── compatibility.schema.json
│   └── compatibility.fallback.json
│
├── packaging/
│   ├── appimage/
│   ├── docker/
│   ├── systemd/
│   └── release/
│
├── fixtures/
│   ├── documents/
│   ├── workspaces/
│   ├── configs/
│   └── events/
│
└── tests/
    ├── contract/
    ├── integration/
    ├── e2e/
    ├── rendering/
    └── migration/
```

## 4.1 Rust-Module

### `omarag-domain`

Enthält ausschließlich frontendunabhängige Datentypen:

- `BackendMeta`
- `BackendProfile`
- `CapabilitySet`
- `WorkspaceSummary`
- `WorkspaceManifest`
- `DatabaseInfo`
- `DocumentSummary`
- `SourceDefinition`
- `SearchRequest`
- `SearchHit`
- `RunRequest`
- `Citation`
- `EvidenceReport`
- `MediaReference`
- `ConfigDraft`
- `ConfigImpact`
- `JobSnapshot`
- `QualityReport`
- `EvaluationSuite`
- `BackupManifest`
- `DomainEvent`
- `OmaRagError`

Dieses Crate darf weder Ratatui noch `reqwest` importieren.

### `omarag-client`

Definiert den gemeinsamen Clientvertrag sowie Implementierungen:

- `HttpOmaRagClient`
- `UnixSocketOmaRagClient`
- `MockOmaRagClient`
- `RecordedOmaRagClient` für reproduzierbare UI-Tests

Kernvertrag, gekürzt:

```rust
#[async_trait]
pub trait OmaRagClient: Send + Sync {
    async fn meta(&self) -> OmaResult<BackendMeta>;
    async fn health(&self) -> OmaResult<HealthReport>;

    async fn list_workspaces(&self) -> OmaResult<Vec<WorkspaceSummary>>;
    async fn open_workspace(&self, id: WorkspaceId) -> OmaResult<WorkspaceState>;
    async fn create_workspace(&self, request: CreateWorkspace) -> OmaResult<WorkspaceState>;

    async fn config_schema(&self, workspace: WorkspaceId) -> OmaResult<ConfigSchema>;
    async fn effective_config(&self, workspace: WorkspaceId) -> OmaResult<EffectiveConfig>;
    async fn validate_config(&self, draft: ConfigDraft) -> OmaResult<ValidationReport>;
    async fn save_config(&self, draft: ConfigDraft, etag: String) -> OmaResult<ApplyReport>;

    async fn list_documents(
        &self,
        workspace: WorkspaceId,
        request: PageRequest,
    ) -> OmaResult<Page<DocumentSummary>>;

    async fn profile_corpus(
        &self,
        workspace: WorkspaceId,
        request: CorpusProfileRequest,
    ) -> OmaResult<JobId>;

    async fn preview_ingest(
        &self,
        workspace: WorkspaceId,
        request: IngestPreviewRequest,
    ) -> OmaResult<JobId>;

    async fn ingest(
        &self,
        workspace: WorkspaceId,
        request: IngestRequest,
        idempotency_key: IdempotencyKey,
    ) -> OmaResult<JobId>;

    async fn search(&self, request: SearchRequest) -> OmaResult<Vec<SearchHit>>;
    async fn start_run(&self, request: RunRequest) -> OmaResult<RunId>;
    async fn cancel_run(&self, run_id: RunId) -> OmaResult<()>;

    async fn job_snapshot(&self, job_id: JobId) -> OmaResult<JobSnapshot>;
    async fn pause_job(&self, job_id: JobId) -> OmaResult<()>;
    async fn resume_job(&self, job_id: JobId) -> OmaResult<()>;

    async fn subscribe_events(
        &self,
        request: EventSubscription,
    ) -> OmaResult<BoxStream<'static, OmaResult<DomainEvent>>>;
}
```

Dadurch kann TUI, CLI oder ein späteres Frontend mit demselben Mock- und Contract-Testbestand entwickelt werden.

### `omarag-app`

Enthält die eigentliche Anwendungslogik:

```text
AppState
   ↓
Action
   ↓
update(state, action)
   ↓
Effect
   ↓
OmaRagClient
   ↓
neue Action
```

Das Modul kennt Ratatui nicht und bildet Navigation, Workspacewechsel, Jobs, Dialoge, Qualitätsansichten und Reconnect-Logik als deterministische Zustandsübergänge ab.

### `omarag-tui`

Enthält nur:

- Ratatui-Komponenten
- Layouts
- Tastatursteuerung
- Formulare
- Theme-Tokens
- Terminalfähigkeiten
- Media-Widgets
- Darstellung des `AppState`

### `omarag-cli`

Verwendet denselben Client wie das TUI. Es implementiert keine parallele Geschäftslogik und liefert wahlweise menschenlesbare oder stabile JSON-Ausgabe.

### `omarag-launcher`

Verantwortlich für:

- vorhandenen Daemon erkennen
- lokalen Daemon starten
- Unix Socket oder freien Loopback-Port bestimmen
- kurzlebiges Zugriffstoken erzeugen
- Healthcheck und Capability-Handshake
- Backendprofile laden
- SSH-Tunnel kontrolliert starten
- Log- und Diagnosepfade festlegen
- AppImage- und Docker-Verbindungen herstellen

## 4.2 Python-Schichten

### `api/`

Dünne HTTP-Schicht ohne komplexe Geschäftslogik. Sie prüft Authentifizierung, Workspacebezug, ETags, Idempotency-Key und Requestgrenzen.

### `services/`

Orchestriert fachliche Abläufe. Services dürfen Adapter und Stores verwenden, aber nicht direkt UI-Annahmen treffen.

### `adapters/`

Übersetzt zwischen öffentlichen Haiku-APIs, Pydantic-AI-Streams und stabilen OmaRag-Domainmodellen.

### `jobs/`

Besitzt die persistente Zustandsmaschine, Ereignisfolgen, Checkpoints und Wiederaufnahme.

### `pipeline/`

Enthält nur zusätzliche Orchestrierung um Haiku herum:

- Preflight
- Corpusprofil
- instrumentierte Ausführung
- Belegprüfung
- keine eigene alternative RAG-Engine

### `quality/`

Bündelt Doctor, Regression, Vergleichsläufe und Quellenintegritätsprüfungen.

## 4.3 Zuständigkeitsregeln

- Hardwareerkennung ist überwiegend Rust, weil Linuxsysteme über `/proc`, `/sys`, PCI und cgroups effizient ausgelesen werden können.
- Backendseitige Laufzeitmetriken werden vom Daemon erfasst und über die API geliefert.
- Modell- und Haiku-Kompatibilität liegt im Daemon, damit alle Frontends dieselben Empfehlungen erhalten.
- Bildauswahl geschieht im Daemon; das TUI rendert vorbereitete Medien.
- Queue, Ereignisse und Checkpoints liegen im Daemon und überleben den Lebenszyklus des TUI.
- Workspace-Manifeste sind portable Dateien; aktive Locks und Tokens gehören nicht hinein.
- Qualitätsberichte und Testdefinitionen sind exportierbar, temporäre Telemetrie bleibt lokal.
- Backups und Restore laufen nie direkt im TUI-Prozess.

---
# 5. Architektur des TUI

Ratatui arbeitet im Immediate-Mode. Jeder Frame wird vollständig aus dem aktuellen Zustand aufgebaut. Netzwerkzugriffe, Dateizugriffe, Bilddekodierung, SSH-Aufbau oder Modellausführungen dürfen niemals im Renderpfad stattfinden.

## 5.1 Ereignisschleife

```text
┌──────────────────────────────────┐
│ Terminal-Eingaben                │
│ Backend-SSE und Replay           │
│ interne Actions                  │
│ Timer und Reconnect              │
│ Desktop-/Systemsignale           │
└────────────────┬─────────────────┘
                 ▼
           mpsc<Action>
                 │
                 ▼
          update(AppState)
                 │
           ┌─────┴─────┐
           ▼           ▼
         State       Effects
           │           │
           │      Hintergrundtasks
           │           │
           └─────Actions zurück
                 │
                 ▼
               render()
```

Ein zentraler `tokio::select!`-Loop verarbeitet:

- Terminalereignisse
- interne Actions
- Server-Sent Events
- periodische Status- und Ressourcensamples
- Reconnect-Timer
- Dateiwächter für lokale Themes
- Shutdown-Signale

## 5.2 Zustandsmodell

```rust
pub struct AppState {
    pub route: Route,
    pub interaction_level: InteractionLevel,
    pub layout_mode: LayoutMode,

    pub connection: ConnectionState,
    pub backend: Option<BackendMeta>,
    pub backends: BackendProfilesState,

    pub workspace: WorkspaceState,
    pub workspace_switcher: WorkspaceSwitcherState,

    pub chat: ChatState,
    pub library: LibraryState,
    pub sources: SourcesState,
    pub jobs: JobState,
    pub search: SearchState,
    pub quality: QualityState,
    pub evaluations: EvaluationState,
    pub backups: BackupState,
    pub settings: SettingsState,
    pub system: SystemState,

    pub overlay: Option<Overlay>,
    pub notifications: Vec<Notification>,
    pub terminal: TerminalCapabilities,
    pub last_event_id: Option<EventId>,
}
```

Alle sichtbaren Zustände liegen in `AppState`. Komponenten dürfen keine versteckten Netzwerk-, Job- oder Workspacezustände besitzen.

## 5.3 Action-Modell

Beispiele:

```rust
pub enum Action {
    QuitRequested,
    Navigate(Route),
    SetInteractionLevel(InteractionLevel),
    SetLayout(LayoutMode),

    BackendConnectRequested(BackendProfileId),
    BackendConnected(BackendMeta),
    BackendDisconnected(String),

    WorkspacesLoadRequested,
    WorkspacesLoaded(Vec<WorkspaceSummary>),
    WorkspaceOpenRequested(WorkspaceId),
    WorkspaceOpened(WorkspaceState),

    QuestionChanged(String),
    RunRequested,
    RunStarted(RunId),
    RunEventReceived(DomainEvent),
    RunCancelRequested,
    RunFailed(OmaRagError),

    DocumentsLoadRequested,
    DocumentsLoaded(Page<DocumentSummary>),
    CorpusProfileRequested(CorpusProfileRequest),
    IngestPreviewRequested(IngestPreviewRequest),
    IngestRequested(IngestRequest),
    JobEventReceived(JobEvent),

    QualityScanRequested,
    QualityReportLoaded(QualityReport),
    RegressionRunRequested(SuiteId),
    BackupCreateRequested,
    BackupRestoreRequested(BackupId),

    ConfigLoadRequested,
    ConfigLoaded(EffectiveConfig),
    ConfigFieldChanged(ConfigPath, ConfigValue),
    ConfigValidateRequested,
    ConfigValidated(ValidationReport),
    ConfigSaveRequested,
    ConfigSaved(ApplyReport),

    EventReplayStarted(EventId),
    EventReplayCompleted(EventId),
    NotificationDismissed(NotificationId),
}
```

## 5.4 Effect-Modell

```rust
pub enum Effect {
    None,
    ConnectBackend(BackendProfileId),
    LoadWorkspaces,
    OpenWorkspace(WorkspaceId),
    LoadDocuments(PageRequest),
    ProfileCorpus(CorpusProfileRequest),
    PreviewIngest(IngestPreviewRequest),
    StartIngest(IngestRequest, IdempotencyKey),
    StartRun(RunRequest),
    CancelRun(RunId),
    SubscribeEvents(EventSubscription),
    LoadJobSnapshot(JobId),
    RunQualityScan(WorkspaceId),
    RunRegression(SuiteId),
    CreateBackup(BackupRequest),
    RestoreBackup(RestoreRequest),
    ValidateConfig(ConfigDraft),
    SaveConfig(ConfigDraft, String),
    ShowNotification(Notification),
}
```

Die `update`-Funktion bleibt deterministisch. Nebenwirkungen werden getrennt ausgeführt und liefern anschließend neue Actions.

## 5.5 Komponentenvertrag

Jede TUI-Komponente implementiert sinngemäß:

```rust
pub trait Component {
    fn route(&self) -> Route;
    fn handle_key(&mut self, key: KeyEvent, ctx: &UiContext) -> Option<Action>;
    fn update(&mut self, state: &AppState);
    fn render(&self, frame: &mut Frame, area: Rect, theme: &Theme);
}
```

Komponenten:

- fragen nicht selbst die API ab
- starten keine Tokio-Tasks
- besitzen keine Geheimnisse
- ändern den globalen Zustand nur über Actions
- müssen mit TestBackend renderbar sein

## 5.6 Reconnect- und Replayablauf

```text
Verbindung verloren
  ↓
UI bleibt lesbar, Eingaben werden kontrolliert gesperrt
  ↓
exponentieller Reconnect mit Obergrenze
  ↓
GET /events mit Last-Event-ID
  ↓
fehlende Ereignisse nachziehen
  ↓
für aktive Jobs zusätzlich Snapshot vergleichen
  ↓
AppState konsistent fortsetzen
```

Doppelte Ereignisse werden anhand `event_id` und `sequence` verworfen.

## 5.7 Lokale UI-Präferenzen

Folgende Einstellungen werden pro Client gespeichert, nicht im Workspace:

- Layout
- Bedienebene
- Theme
- Spaltenbreiten
- Bilddarstellungsmodus
- letzte Route
- sichtbare Hilfetexte
- Tastaturpräferenzen
- Desktopbenachrichtigungen

Fachliche und reproduzierbare Einstellungen gehören dagegen in den Workspace.

---
# 6. OmaRag API v1

## 6.1 Transport und Grundregeln

Für Version 1:

- HTTP/1.1 oder HTTP/2 hinter einem geeigneten Proxy
- JSON für normale Anfragen
- Server-Sent Events für Token-, Job- und Systemstreams
- Standardbindung an Unix Socket oder `127.0.0.1`
- zufälliges Bearer-Token im eingebetteten Modus
- API-Präfix `/v1`
- OpenAPI 3.1
- strukturierte Fehlerobjekte
- ETags für veränderbare Ressourcen
- `Idempotency-Key` für startende oder mutierende Operationen
- `Last-Event-ID` für wiederaufnehmbare Ereignisstreams
- `X-OmaRag-Workspace` nur als Komfortheader; der Workspace bleibt zusätzlich Teil des Pfads oder Requests

### Warum SSE statt WebSocket?

SSE genügt für:

- Token-Streaming
- Werkzeug- und Capability-Ereignisse
- Indexierungsfortschritt
- Statusmeldungen
- Quellen und Medien
- Modellpulls
- Quality- und Evaluationsläufe
- Replay nach Verbindungsunterbrechung

Befehle wie Start, Pause, Abbruch oder Konfigurationsänderungen laufen über normale HTTP-Anfragen. Das erleichtert Browser-, CLI-, Atlas- und Proxyintegration.

## 6.2 Metadaten und Capability-Handshake

```http
GET /v1/meta
GET /v1/health
GET /v1/readiness
```

Beispiel:

```json
{
  "api_version": "1.0",
  "omarag_version": "0.3.0",
  "haiku_version": "0.70.0",
  "adapter": "haiku-v070",
  "backend_id": "server-alsfeld",
  "capabilities": {
    "streaming_chat": true,
    "question_images": true,
    "analysis_images": true,
    "multimodal_search": true,
    "multimodal_reranking": true,
    "visual_grounding": true,
    "database_tags": true,
    "native_ingester": true,
    "evaluation": true,
    "event_replay": true,
    "workspaces": true
  }
}
```

`health` beantwortet, ob der Prozess lebt. `readiness` prüft zusätzlich Runtime, aktive Workspace-Datenbank und notwendige Provider.

## 6.3 Workspaces

```http
GET    /v1/workspaces
POST   /v1/workspaces
GET    /v1/workspaces/{workspace_id}
PATCH  /v1/workspaces/{workspace_id}
DELETE /v1/workspaces/{workspace_id}

POST   /v1/workspaces/{workspace_id}/open
POST   /v1/workspaces/{workspace_id}/close
POST   /v1/workspaces/{workspace_id}/clone
POST   /v1/workspaces/{workspace_id}/export
POST   /v1/workspaces/import
GET    /v1/workspaces/{workspace_id}/status
```

Ein Workspace kann nur gelöscht werden, wenn:

- kein mutierender Job aktiv ist,
- ein expliziter Bestätigungswert mitgesendet wird,
- der Nutzer zwischen „nur aus OmaRag entfernen“ und „Daten physisch löschen“ wählt.

## 6.4 Konfiguration

```http
GET  /v1/workspaces/{workspace_id}/config/schema
GET  /v1/workspaces/{workspace_id}/config/effective
POST /v1/workspaces/{workspace_id}/config/validate
PUT  /v1/workspaces/{workspace_id}/config
POST /v1/workspaces/{workspace_id}/config/reload
GET  /v1/workspaces/{workspace_id}/config/profiles
POST /v1/workspaces/{workspace_id}/config/profiles
POST /v1/workspaces/{workspace_id}/config/impact
```

`effective` liefert:

- aktive Werte
- Standardwerte
- Quelle jedes Werts
- ausgeblendete Geheimnisse
- ETag
- Warnungen
- Rebuild-, Migrations- oder Neustartbedarf

## 6.5 Datenbank und Lebenszyklus

```http
GET  /v1/workspaces/{workspace_id}/database
GET  /v1/workspaces/{workspace_id}/database/info
GET  /v1/workspaces/{workspace_id}/database/doctor
POST /v1/workspaces/{workspace_id}/database/migrate
POST /v1/workspaces/{workspace_id}/database/rebuild
POST /v1/workspaces/{workspace_id}/database/create-index
POST /v1/workspaces/{workspace_id}/database/vacuum
GET  /v1/workspaces/{workspace_id}/database/history
```

Alle mutierenden Datenbankaktionen werden von `omaragd` serialisiert.

## 6.6 Dokumente

```http
GET    /v1/workspaces/{workspace_id}/documents
POST   /v1/workspaces/{workspace_id}/documents/ingest
POST   /v1/workspaces/{workspace_id}/documents/preview
GET    /v1/workspaces/{workspace_id}/documents/{document_id}
PATCH  /v1/workspaces/{workspace_id}/documents/{document_id}
DELETE /v1/workspaces/{workspace_id}/documents/{document_id}

GET    /v1/workspaces/{workspace_id}/documents/{document_id}/chunks
GET    /v1/workspaces/{workspace_id}/documents/{document_id}/versions
GET    /v1/workspaces/{workspace_id}/documents/{document_id}/processing-report
```

Ein Ingest-Auftrag:

```json
{
  "sources": [
    {
      "type": "file",
      "path": "/home/user/Buecher/beton.pdf"
    }
  ],
  "tags": ["Baustoffkunde", "Beton"],
  "metadata": {
    "collection": "Fachbücher",
    "document_type": "textbook"
  },
  "processing_profile": "bau-fachbuch",
  "duplicate_policy": "review",
  "validity_policy": "prefer-current"
}
```

## 6.7 Corpus-Profiler und Testindexierung

```http
POST /v1/workspaces/{workspace_id}/corpus/profile
GET  /v1/workspaces/{workspace_id}/corpus/profile/{job_id}
POST /v1/workspaces/{workspace_id}/ingest-previews
GET  /v1/workspaces/{workspace_id}/ingest-previews/{preview_id}
POST /v1/workspaces/{workspace_id}/ingest-previews/{preview_id}/accept
```

Ein Previewrequest kann repräsentative Seiten automatisch wählen oder explizit festlegen:

```json
{
  "source": "/home/user/Buecher/beton.pdf",
  "selection": {
    "mode": "representative",
    "max_pages": 12,
    "include": ["title", "toc", "text", "tables", "images", "scans"]
  }
}
```

Die Annahme eines Previews erzeugt einen normalen, idempotenten Importjob.

## 6.8 Quellenmanager

```http
GET    /v1/workspaces/{workspace_id}/sources
POST   /v1/workspaces/{workspace_id}/sources
GET    /v1/workspaces/{workspace_id}/sources/{source_id}
PATCH  /v1/workspaces/{workspace_id}/sources/{source_id}
DELETE /v1/workspaces/{workspace_id}/sources/{source_id}

POST   /v1/workspaces/{workspace_id}/sources/{source_id}/test
POST   /v1/workspaces/{workspace_id}/sources/{source_id}/preview-sync
POST   /v1/workspaces/{workspace_id}/sources/{source_id}/sync
POST   /v1/workspaces/{workspace_id}/sources/{source_id}/pause
```

## 6.9 Suche und Chat

```http
POST /v1/workspaces/{workspace_id}/search
POST /v1/workspaces/{workspace_id}/search/image
POST /v1/workspaces/{workspace_id}/runs
GET  /v1/workspaces/{workspace_id}/runs/{run_id}
DELETE /v1/workspaces/{workspace_id}/runs/{run_id}
```

`search` führt nur Retrieval aus. `runs` startet Fragebeantwortung oder Analyse.

Ein Runrequest:

```json
{
  "session_id": "session-7",
  "mode": "rag",
  "question": "Welche Unterschiede bestehen zwischen XC3 und XC4?",
  "images": [],
  "evidence_mode": "strict",
  "document_policy": "current-only",
  "filters": {
    "tags": ["Beton"]
  }
}
```

## 6.10 Ereignisse und Replay

```http
GET /v1/events
GET /v1/workspaces/{workspace_id}/events
GET /v1/jobs/{job_id}/events
GET /v1/runs/{run_id}/events
GET /v1/jobs/{job_id}/snapshot
GET /v1/runs/{run_id}/snapshot
```

Clients senden `Last-Event-ID`. Ist die Ereignishistorie bereits bereinigt, antwortet der Server mit einem Snapshotanker und liefert anschließend neue Ereignisse.

## 6.11 Jobs

```http
GET    /v1/jobs
POST   /v1/jobs
GET    /v1/jobs/{job_id}
POST   /v1/jobs/{job_id}/pause
POST   /v1/jobs/{job_id}/resume
DELETE /v1/jobs/{job_id}
POST   /v1/jobs/{job_id}/retry
POST   /v1/jobs/{job_id}/priority
GET    /v1/jobs/{job_id}/log
```

Jeder startende Request akzeptiert einen `Idempotency-Key`. Ein bereits erfolgreich angelegter Job wird beim Wiederholen zurückgegeben, nicht dupliziert.

## 6.12 Sessions

```http
GET    /v1/workspaces/{workspace_id}/sessions
POST   /v1/workspaces/{workspace_id}/sessions
GET    /v1/workspaces/{workspace_id}/sessions/{session_id}
PATCH  /v1/workspaces/{workspace_id}/sessions/{session_id}
DELETE /v1/workspaces/{workspace_id}/sessions/{session_id}
```

Eine Session speichert:

- Titel und Verlauf
- Dokumentfilter
- angeheftete Quellen
- Belegmodus
- Modell- und Pipelineidentität
- Quellen- und Medienverweise
- reproduzierbare Runparameter

## 6.13 Quellen, Zitate und Medien

```http
GET /v1/workspaces/{workspace_id}/citations/{chunk_id}
GET /v1/workspaces/{workspace_id}/chunks/{chunk_id}/context
GET /v1/workspaces/{workspace_id}/chunks/{chunk_id}/visualization
GET /v1/media/{media_id}
GET /v1/media/{media_id}/thumbnail
GET /v1/media/{media_id}/source
POST /v1/media/select
```

Eine Quellenangabe enthält mindestens:

```json
{
  "chunk_id": "chunk-123",
  "document_id": "doc-42",
  "document_title": "Betontechnologie",
  "edition": "2025",
  "validity_status": "current",
  "pages": [112, 113],
  "headings": ["Frischbeton", "Konsistenzklassen"],
  "excerpt": "...",
  "retrieval_rank": 1,
  "rerank_score": 0.91,
  "media": [
    {
      "id": "media-9",
      "kind": "table",
      "page": 113,
      "mime_type": "image/png"
    }
  ]
}
```

## 6.14 Qualität und Doctor

```http
GET  /v1/workspaces/{workspace_id}/quality
POST /v1/workspaces/{workspace_id}/quality/scan
GET  /v1/workspaces/{workspace_id}/quality/issues
POST /v1/workspaces/{workspace_id}/quality/issues/{issue_id}/repair
GET  /v1/workspaces/{workspace_id}/quality/evidence/{run_id}
```

Reparaturen werden als Jobs behandelt. Automatisches Löschen ist nicht erlaubt.

## 6.15 Regression und Evaluation

```http
GET    /v1/workspaces/{workspace_id}/evaluations/suites
POST   /v1/workspaces/{workspace_id}/evaluations/suites
GET    /v1/workspaces/{workspace_id}/evaluations/suites/{suite_id}
PATCH  /v1/workspaces/{workspace_id}/evaluations/suites/{suite_id}
DELETE /v1/workspaces/{workspace_id}/evaluations/suites/{suite_id}

POST   /v1/workspaces/{workspace_id}/evaluations/runs
GET    /v1/workspaces/{workspace_id}/evaluations/runs/{run_id}
GET    /v1/workspaces/{workspace_id}/evaluations/compare
```

## 6.16 A/B-Labor

```http
POST /v1/workspaces/{workspace_id}/experiments
GET  /v1/workspaces/{workspace_id}/experiments/{experiment_id}
POST /v1/workspaces/{workspace_id}/experiments/{experiment_id}/run
POST /v1/workspaces/{workspace_id}/experiments/{experiment_id}/promote
```

Eine Beförderung zum Standardprofil erfordert erfolgreichen Config-Impact-Check.

## 6.17 Backups, Tags und Restore

```http
GET  /v1/workspaces/{workspace_id}/snapshots
POST /v1/workspaces/{workspace_id}/snapshots
POST /v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/restore
DELETE /v1/workspaces/{workspace_id}/snapshots/{snapshot_id}

GET  /v1/workspaces/{workspace_id}/backups
POST /v1/workspaces/{workspace_id}/backups
GET  /v1/workspaces/{workspace_id}/backups/{backup_id}
POST /v1/workspaces/{workspace_id}/backups/{backup_id}/verify
POST /v1/workspaces/{workspace_id}/backups/{backup_id}/restore
```

Snapshots verwenden nach Möglichkeit Haiku-/LanceDB-Tags. Externe Backups sind davon getrennte physische Sicherungen.

## 6.18 Kapazität und Ressourcen

```http
GET  /v1/hardware
POST /v1/hardware/refresh
GET  /v1/resources
GET  /v1/workspaces/{workspace_id}/capacity
POST /v1/workspaces/{workspace_id}/capacity/estimate
GET  /v1/resource-policy
PUT  /v1/resource-policy
POST /v1/resources/pause-background
POST /v1/resources/resume-background
```

## 6.19 Modelle und Benchmarks

```http
GET  /v1/models/catalog
GET  /v1/models/installed
POST /v1/models/recommend
POST /v1/models/pull
POST /v1/models/benchmark
POST /v1/models/select
POST /v1/models/preload
POST /v1/models/unload
```

Die freie Modellauswahl bleibt erhalten. OmaRag blockiert nur nachweislich technisch inkompatible Kombinationen.

## 6.20 Abhängigkeiten und Updates

```http
GET  /v1/system/dependencies
POST /v1/system/dependencies/check-updates
POST /v1/system/dependencies/plan-install
POST /v1/system/dependencies/apply
POST /v1/system/dependencies/rollback
GET  /v1/system/update-policy
PUT  /v1/system/update-policy
```

Plan und Anwendung sind getrennte Schritte.

## 6.21 Backendprofile

```http
GET    /v1/client/backends
POST   /v1/client/backends
PATCH  /v1/client/backends/{backend_id}
DELETE /v1/client/backends/{backend_id}
POST   /v1/client/backends/{backend_id}/test
```

Diese Endpunkte können lokal vom Launcher bereitgestellt werden oder als Frontendkonfiguration ohne Daemonaufruf umgesetzt werden. Geheimnisse gehören in einen geeigneten lokalen Secret Store.

## 6.22 Diagnose

```http
POST /v1/diagnostics
GET  /v1/diagnostics/{diagnostic_id}
GET  /v1/diagnostics/{diagnostic_id}/download
```

Vor Erstellung liefert ein Preview die enthaltenen und ausgeschlossenen Daten.

## 6.23 Themes und UI-Ressourcen

```http
GET /v1/ui/themes
GET /v1/ui/omarchy-themes
GET /v1/ui/keymap
```

Layoutwahl, Bedienebene und aktives Theme bleiben grundsätzlich clientlokal.

## 6.24 MCP-Fassade

Die MCP-Fassade ist ein optionaler Adapter und kein zweiter RAG-Kern. Sie bildet nur freigegebene OmaRag-Operationen auf MCP-Werkzeuge und Ressourcen ab.

## 6.25 API-Versionierung

- additive Felder sind innerhalb `v1` erlaubt
- unbekannte Eventtypen und JSON-Felder müssen Clients ignorieren
- entfernte oder semantisch geänderte Felder erfordern `v2`
- OpenAPI- und Event-Schema werden als Contract-Snapshots getestet
- jeder Server meldet minimale und maximale Clientprotokollversion
- deprecations werden strukturiert in `/v1/meta` angegeben

---
# 7. Einheitliches Ereignismodell

Unterschiede zwischen Haiku-Versionen, Pydantic-AI-Streams, Jobausführungen und Providerereignissen werden im Adapter normalisiert.

## 7.1 Persistenter Ereignisumschlag

```json
{
  "event_id": 12875,
  "sequence": 318,
  "timestamp": "2026-07-25T14:28:12.551Z",
  "type": "job.progress",
  "workspace_id": "ws-baustoffkunde",
  "job_id": "job-42",
  "run_id": null,
  "correlation_id": "corr-b7e1",
  "schema_version": 1,
  "payload": {}
}
```

Bedeutung:

- `event_id`: global monoton steigende Persistenz-ID
- `sequence`: monoton innerhalb eines Jobs oder Runs
- `workspace_id`: fachlicher Geltungsbereich
- `correlation_id`: verbindet Request, Job, Log und Fehler
- `schema_version`: Version des konkreten Payloadschemas

## 7.2 Kernereignisse

```text
connection.changed
backend.changed
workspace.opened
workspace.changed

run.started
run.completed
run.cancelled
run.failed

assistant.started
assistant.delta
assistant.completed

tool.started
tool.progress
tool.completed
tool.failed

retrieval.started
retrieval.hit
retrieval.completed
evidence.updated

citation.added
media.candidate
media.available
media.selection.completed

job.queued
job.started
job.progress
job.pause.requested
job.paused
job.resumed
job.checkpoint.saved
job.completed
job.cancelled
job.failed

config.changed
database.changed
health.changed
quality.changed
```

## 7.3 Betriebs- und Erweiterungsereignisse

```text
dependency.scan.started
dependency.status
dependency.update.available
dependency.download.progress
dependency.install.completed
dependency.rollback.completed

hardware.scan.completed
resource.policy.applied
resource.throttled
resource.background.paused
resource.background.resumed

model.recommendations.updated
model.pull.progress
model.loaded
model.unloaded
model.benchmark.progress

corpus.profile.progress
corpus.profile.completed
ingest.preview.progress
ingest.preview.completed
capacity.estimate.completed

source.sync.previewed
source.sync.started
source.sync.completed
source.sync.failed

quality.scan.progress
quality.issue.detected
quality.scan.completed
evaluation.run.progress
evaluation.run.completed
experiment.progress
experiment.completed

snapshot.created
restore.started
restore.completed
backup.progress
backup.completed
backup.verified

daemon.idle
daemon.shutdown.scheduled
```

## 7.4 Ereignisregeln

- `event_id` ist global monoton und wird im SQLite-Eventstore persistiert.
- `sequence` ist pro Job oder Run monoton.
- Unbekannte Ereignistypen werden ignoriert, nicht als Protokollfehler behandelt.
- Clients dürfen dasselbe Ereignis mehrfach erhalten und müssen deduplizieren.
- `run.completed`, `run.cancelled` oder `run.failed` schließen einen Run.
- `job.completed`, `job.cancelled` oder `job.failed` schließen einen Job.
- Quellen dürfen vor oder nach dem finalen Antworttext eintreffen.
- Toolnamen werden in verständliche Labels übersetzt.
- Interne Chain-of-Thought-Inhalte werden weder persistiert noch übertragen.
- Geheimnisse und vollständige Dokumenttexte gehören nicht in Betriebsereignisse.
- Ereignisse besitzen eine Aufbewahrungsrichtlinie; Snapshots sichern den aktuellen Zustand nach Bereinigung alter Events.

## 7.5 Replay

Client:

```http
GET /v1/workspaces/ws-baustoffkunde/events
Last-Event-ID: 12875
```

Server:

1. liefert alle noch vorhandenen Ereignisse nach `12875`,
2. sendet anschließend Liveereignisse,
3. oder signalisiert `replay_reset`, wenn die Historie bereits kompaktiert wurde.

Bei `replay_reset` lädt der Client:

```http
GET /v1/jobs/job-42/snapshot
```

und setzt danach ab dem im Snapshot enthaltenen `last_event_id` fort.

## 7.6 Beispiel für Indexfortschritt

```json
{
  "event_id": 13142,
  "sequence": 1842,
  "timestamp": "2026-07-25T14:42:02.001Z",
  "type": "job.progress",
  "workspace_id": "ws-baustoffkunde",
  "job_id": "job-8f3",
  "correlation_id": "corr-991a",
  "schema_version": 1,
  "payload": {
    "phase": "embedding",
    "phase_label": "Embeddings berechnen",
    "phase_progress": 0.735,
    "overall_progress": 0.618,
    "units": {
      "kind": "chunks",
      "done": 1482,
      "total": 2016
    },
    "counters": {
      "files_done": 5,
      "files_total": 12,
      "pages_done": 1274,
      "pages_total": 2068,
      "chunks_done": 1482,
      "chunks_total": 2016,
      "embedding_batches_done": 46,
      "embedding_batches_total": 63
    },
    "rate": {
      "value": 18.4,
      "unit": "chunks/s"
    },
    "eta": {
      "seconds_p50": 870,
      "seconds_low": 720,
      "seconds_high": 1080,
      "confidence": "medium",
      "status": "estimated"
    },
    "checkpoint": "embedding-batch-46",
    "resource_state": "normal"
  }
}
```

## 7.7 Beispiel für Belegstatus

```json
{
  "event_id": 13180,
  "sequence": 52,
  "type": "evidence.updated",
  "workspace_id": "ws-baustoffkunde",
  "run_id": "run-193",
  "schema_version": 1,
  "payload": {
    "status": "strong",
    "claims_total": 6,
    "claims_supported": 5,
    "distinct_documents": 3,
    "page_grounding_available": true,
    "warnings": [
      "Eine ergänzende Aussage wird nur durch eine Quelle gestützt."
    ]
  }
}
```

## 7.8 Idempotenz

Startende Operationen verwenden:

```http
Idempotency-Key: ingest-betontechnologie-2026-07-25
```

Der Server speichert Key, Requesthash und Ergebnis. Derselbe Key mit identischem Request liefert das vorhandene Ergebnis. Derselbe Key mit verändertem Request ergibt `IDEMPOTENCY_CONFLICT`.

---
# 8. Vollständige Haiku-Konfiguration im TUI

Haiku RAG besitzt eine umfangreiche YAML-Konfiguration für:

- Umgebung
- Speicher
- LanceDB
- Embeddings
- Reranking
- QA
- Analyse
- Suche
- Dokumentverarbeitung
- OCR
- Tabellen
- Bilder
- Provider
- Prompts
- Doctor
- Ingester

Die Felder dürfen **nicht manuell ein zweites Mal in Rust nachgebaut** werden. Sonst fehlen nach jedem Haiku-Update Einstellungen.


### 8.0 Konfigurationshierarchie

OmaRag trennt vier Ebenen:

| Ebene | Beispiele | Speicherort |
|---|---|---|
| globaler Betrieb | Runtime, Updatekanal, Modellkatalog, Daemon | globale OmaRag-Konfiguration |
| Backendprofil | URL, SSH-Tunnel, Authentifizierung | lokaler Client-Secret-Store |
| Workspace | Haiku-Konfiguration, Datenbank, Quellen, Modelle, Qualitätsregeln | Workspace-Verzeichnis |
| Client-UI | Layout, Theme, Bedienebene, Spaltenbreiten | lokales Frontendprofil |

Die effektive Workspacekonfiguration ergibt sich aus:

```text
Haiku-Defaults
  ↓
OmaRag-Verarbeitungsprofil
  ↓
Workspace haiku.rag.yaml
  ↓
zulässige Laufzeit-Overrides
```

Geheimnisse werden nicht in portable Workspace-Manifeste geschrieben. Das Manifest referenziert nur Secret-IDs oder Umgebungsvariablen.

Ein Workspace speichert zusätzlich die Identität des Embeddingmodells und die Vektordimension. Dadurch kann OmaRag bereits vor dem Öffnen erkennen, ob die aktuelle Konfiguration zum Index passt.


## 8.1 Schema-getriebene Formulare

Der Python-Adapter exportiert das JSON-Schema der tatsächlichen Haiku-Pydantic-Konfiguration.

Konzeptuell:

```python
schema = AppConfig.model_json_schema()
```

OmaRag ergänzt ausschließlich UI-Metadaten:

```yaml
/embeddings/model:
  group: "Modelle"
  order: 10
  label: "Embedding-Modell"
  help: "Erzeugt die Vektoren für Dokumente und Suchanfragen."
  impact: "rebuild"

/qa/model:
  group: "Modelle"
  order: 20
  label: "Antwortmodell"

/processing/ocr:
  group: "Verarbeitung"
  order: 40
  advanced: false
```

Diese UI-Metadaten enthalten keine Standardwerte und keine Validierungslogik. Sie steuern nur:

- deutsche Bezeichnung
- Hilfetext
- Reihenfolge
- Gruppierung
- erweiterte Ansicht
- Sicherheitswarnungen
- Auswirkungsanzeige

Unbekannte neue Felder erscheinen automatisch unter:

```text
Einstellungen → Erweitert → Neue Haiku-Einstellungen
```

---

## 8.2 Einstellungsbereiche

### Allgemein

- Profilname
- Datenverzeichnis
- Umgebungsvariablen
- Logstufe
- lokale oder externe Verbindung
- Sprache
- Standarddatenbank

### Datenbank und Speicher

- LanceDB-URI
- lokale und entfernte Speicheroptionen
- API-Key und Region
- automatisches Vacuum
- Aufbewahrungsdauer
- Indexmetrik
- Refine-Faktor
- Read-only-Modus

### Embeddings

- Provider
- Modellname
- Vektordimension
- Basis-URL
- Batchgröße
- multimodaler Modus
- providerspezifische Parameter

### Reranking

- Provider
- Modell
- multimodales Reranking
- zusätzliche Modellparameter
- Kandidatenzahl
- Ergebnislimit

### Antwortmodell

- Provider
- Modell
- Temperatur
- Tokenlimit
- Thinking-Modus
- Vision-Unterstützung
- Basis-URL
- maximale Suchvorgänge
- zusätzliche Request-Felder

### Suche

- Trefferlimit
- maximale Kontextgröße
- Filter
- Suchmodus
- Volltext-/Vektorgewichtung
- Retrieval-Diagnose
- Reranking aktiv oder inaktiv

### Dokumentverarbeitung

- lokales Docling oder Docling Serve
- Converter
- Chunker
- hybrides oder hierarchisches Chunking
- Chunkgröße
- Tokenizer
- Zusammenführung benachbarter Chunks
- Markdown-Tabellen
- PDF-Anhänge
- automatische Titel
- Titelmodell
- PDF-Seitensplitting

### OCR, Tabellen und Bilder

- OCR-Aktivierung
- OCR-Sprachen und Optionen
- Tabellenextraktion
- Bildskalierung
- Seitendarstellungen
- Bildbeschreibungsmodell
- Bildmodus `none`, `description` oder `image`
- multimodale Einbettung

### Analyse

- Analysemodell
- Sandboxoptionen
- Ausführungsgrenzen
- aktivierte Analysefähigkeiten
- maximale Toolausführungen

### Prompts

- Domain-Preamble
- QA-Prompt
- Prompt für Bildbeschreibungen
- weitere von Haiku angebotene Prompts

### Ingester

- Queue-Datenbank
- Quellen
- Worker
- Parallelität
- Lease und Heartbeat
- Wiederholungen
- Dead-Letter Queue
- API-Bindung
- Authentifizierung
- Dateigrößenlimits
- Pollingintervalle

### Doctor und Wartung

- Duplikatschwelle
- Mindestanzahl Chunks
- Migration
- Rebuild
- Vacuum
- Datenbankprüfung
- Embedder-Abgleich

### Rohkonfiguration

- vollständige YAML-Ansicht
- Suche nach Schlüsseln
- Validierung
- Diff
- Rücksetzen
- Export
- Import

---

## 8.3 Sicheres Speichern

Ablauf einer Änderung:

```text
Wert im TUI ändern
       ↓
lokaler ConfigDraft
       ↓
POST /v1/config/validate
       ↓
Pydantic-Validierung durch Haiku
       ↓
Auswirkungsanalyse
       ↓
Diff anzeigen
       ↓
PUT /v1/config mit ETag
       ↓
temporäre Datei schreiben
       ↓
fsync + atomarer Rename
       ↓
Konfiguration neu laden
```

Der vorherige Stand wird als Sicherung erhalten.

## 8.4 Auswirkungen einer Änderung

Das Backend klassifiziert Änderungen:

| Klasse | Beispiel | Verhalten |
|---|---|---|
| `live` | Suchlimit, Kontextgröße | sofort anwenden |
| `client_reload` | Antwortmodell, Reranker | Haiku-Client neu öffnen |
| `daemon_restart` | Ingester-API, Worker | Dienst neu starten |
| `rebuild_required` | Chunkgröße, Chunker, Embeddingmodell | Index neu aufbauen |
| `migration_required` | inkompatible Haiku-Version | Migration bestätigen |
| `dangerous` | Datenbank-URI, Löschen | explizite Bestätigung |

Bei einer Änderung der Vektordimension zeigt OmaRag eine blockierende Meldung. Eine unpassende Dimension ist inkompatibel zum bestehenden Index.

---

# 9. TUI-Design: drei umschaltbare Layouts

OmaRag besitzt eine gemeinsame Zustands- und Navigationslogik, aber drei unterschiedliche Darstellungen. Der Nutzer kann jederzeit mit `F2`, `Alt+1`, `Alt+2` oder `Alt+3` umschalten. Die Wahl wird pro Profil gespeichert; ein laufender Chat oder Indexjob bleibt beim Wechsel unverändert.

## 9.1 Layout A – **Fokus**

Das Standardlayout für Lesen, Fragen und Quellenkontrolle. Es kombiniert eine große Hauptfläche mit einer ein- und ausblendbaren Evidenzleiste.

```text
┌ OmaRag · LOCAL · Bauwissen ─ qwen3.5:4b ─ 428 Dokumente ────●┐
│ [c] Chat  [b] Bibliothek  [i] Index  [s] Suche  [e] Einst.   │
├──────────────────────────────────────────────┬────────────────┤
│                                              │ QUELLEN        │
│  Wie unterscheiden sich XC3 und XC4?        │                │
│                                              │ 1  Beton…      │
│  XC3 beschreibt …                            │    S. 112      │
│                                              │ 2  DIN…        │
│  [1] Betontechnologie, S. 112                │    S. 18       │
│  [2] DIN-Auszug, S. 18                       │                │
│                                              │ BILDER  2/3    │
│                                              │ ┌────┐ ┌────┐ │
│                                              │ │    │ │    │ │
│                                              │ └────┘ └────┘ │
├──────────────────────────────────────────────┴────────────────┤
│ TEXT │ Frage eingeben …                         Enter senden   │
│ ↑↓ wählen · ←→ Bereich · F2 Layout · F3 Theme · ? Hilfe      │
└───────────────────────────────────────────────────────────────┘
```

**Geeignet für:** normalen RAG-Chat, Unterrichtsvorbereitung, Quellenprüfung und Terminals ab etwa 110 Spalten.

**Verhalten:**

- Evidenzleiste mit `→` fokussieren oder mit `v` ein- und ausblenden.
- Quellen und Bilder teilen sich die rechte Leiste dynamisch.
- Bei unter 110 Spalten wird die Evidenzleiste zu einem Overlay.
- Chat bleibt stets der größte Bereich.

## 9.2 Layout B – **Werkbank**

Das informationsreiche Layout für Indexierung, Pipeline-Tuning und Retrieval-Fehlersuche.

```text
┌ OmaRag · WERKBANK ─ Profil: Deutsches Fachbuch ─ LOCAL ─────●┐
├──────────────┬────────────────────────────────┬───────────────┤
│ NAVIGATION   │ ARBEITSFLÄCHE                  │ INSPEKTOR     │
│              │                                │               │
│ [c] Chat     │ Indexierung                    │ Aktive Datei  │
│ [b] Biblioth.│ ████████████░░░  61,8 %        │ DIN-Beton.pdf │
│ [i] Index    │ 5/12 Dateien · 1.274/2.068 S.  │ S. 418/662    │
│ [s] Suche    │ ETA 12–18 min · mittel         │               │
│ [e] Einst.   │                                │ OCR 93 %      │
│ [y] System   │ Embeddings                     │ 4 Tabellen    │
│              │ 1.482/2.016 · 18,4 Chunks/s    │ 7 Bilder      │
│ JOBS         │                                │               │
│ ▶ job-8f3    │ [Pause] [Details] [Protokoll]  │ Ereignisse    │
│ ‖ job-91a    │                                │ 10:42 …       │
├──────────────┴────────────────────────────────┴───────────────┤
│ NAV │ ↑↓ Element · ←→ Spalte · Enter öffnen · Space Aktion   │
└───────────────────────────────────────────────────────────────┘
```

**Geeignet für:** große Fachbuchsammlungen, Fehlerdiagnose, Modellvergleich und breite Terminals ab etwa 145 Spalten.

**Verhalten:**

- Linke Spalte enthält Navigation und Jobliste.
- Mitte zeigt die aktive Aufgabe oder den Chat.
- Rechts stehen technische Details, Treffer, Chunkkontext, Medien und Logs.
- Spaltenbreiten sind per `Shift+←/→` veränderbar und werden gespeichert.
- Unter 145 Spalten fällt der Inspektor als Overlay aus der Hauptfläche heraus.

## 9.3 Layout C – **Zen**

Eine ruhige Einspaltenansicht für kleine Terminals, konzentriertes Lesen und Notebookbetrieb.

```text
┌ OmaRag ─ LOCAL ─ ZEN ────────────────────────────────────────┐
│ [c] Chat   [b] Bibliothek   [i] Index   [s] Suche            │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│             Wie unterscheiden sich XC3 und XC4?              │
│                                                              │
│  XC3 beschreibt mäßige Feuchte …                             │
│                                                              │
│  1 · Betontechnologie · Seite 112                            │
│  2 · DIN-Auszug · Seite 18                                   │
│                                                              │
│  [2 passende Bilder öffnen]                                  │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ Frage eingeben …                                             │
├──────────────────────────────────────────────────────────────┤
│ NAV · Ctrl+P Befehle · F2 Layout · F3 Theme · ? Hilfe       │
└──────────────────────────────────────────────────────────────┘
```

**Geeignet für:** 80×24-Terminals, SSH, ablenkungsarmes Lesen und geringe Bildschirmbreite.

**Verhalten:**

- Quellen, Bilder, Jobs und Einstellungen öffnen sich als Vollbild-Overlay.
- Keine dauerhaft sichtbare Seitenleiste.
- Der aktuelle Kontext bleibt in einer kurzen Kopfzeile erhalten.
- Bildkarten werden nacheinander oder als schmale Galerie dargestellt.

## 9.4 Gemeinsames Navigationsmodell

OmaRag ist vollständig mit Pfeiltasten bedienbar. Buchstabenkürzel ergänzen die Navigation, ersetzen sie aber nicht.

| Taste | Funktion im Navigationsmodus |
|---|---|
| `↑` / `↓` | vorheriges oder nächstes Element |
| `←` / `→` | Bereich, Spalte oder Tab wechseln |
| `Enter` | öffnen, fokussieren oder bestätigen |
| `Space` | markieren, pausieren oder umschalten |
| `Tab` / `Shift+Tab` | Fokus zyklisch wechseln |
| `Esc` | Overlay schließen oder eine Ebene zurück |
| `c` | Chat |
| `b` | Bibliothek |
| `i` | Indexierung |
| `s` | Suche und Retrieval-Inspector |
| `e` | Einstellungen |
| `y` | Systemstatus |
| `F2` | Layout wechseln |
| `F3` | Theme wechseln |
| `Ctrl+P` | Befehlspalette |
| `?` | kontextbezogene Hilfe |
| `q` | TUI schließen; laufende Hintergrundjobs bleiben erhalten |

### NAV- und TEXT-Modus

Damit `[c]`, `[i]` und andere Merktasten nicht beim Schreiben stören, gibt es zwei klar sichtbare Modi:

- **NAV:** Buchstaben sind globale Bereichskürzel.
- **TEXT:** Buchstaben werden in das aktive Eingabefeld geschrieben.

`Enter` auf einem Eingabefeld oder `/` aktiviert `TEXT`; `Esc` kehrt zu `NAV` zurück. `Alt+c`, `Alt+i` und die übrigen `Alt`-Kürzel funktionieren optional aus beiden Modi.

## 9.5 Responsive Regeln

| Breite | Standarddarstellung |
|---|---|
| `< 90` | Zen, einspaltig, Overlays |
| `90–119` | Fokus ohne permanente Evidenzleiste |
| `120–144` | Fokus mit Evidenzleiste |
| `≥ 145` | Werkbank möglich, drei Spalten |

Der Nutzer darf die automatische Auswahl deaktivieren. OmaRag wechselt niemals während einer aktiven Texteingabe eigenmächtig das Layout.

## 9.6 Barrierearmut und Zustände

- Farbe ist nie der einzige Bedeutungsträger.
- Status erscheint als Text und Symbol: `AKTIV ▶`, `PAUSIERT ‖`, `FEHLER !`.
- Ein monochromer Fallback ist vorhanden.
- Rahmen und Fokusmarkierung bleiben auch bei deaktivierten Farben sichtbar.
- Lange Operationen zeigen laufend sichtbare Aktivität, aber keine flackernden Vollbild-Neuzeichnungen.
- Die Renderfrequenz wird bei Inaktivität reduziert; Backendereignisse lösen gezielte Redraws aus.

---


## 9.7 Zwei Bedienebenen

Layout und Bedienebene sind unabhängig:

```text
Layout:        Fokus | Werkbank | Zen
Bedienebene:   Einfach | Werkstatt
```

### Einfach

Sichtbar sind nur:

- Frage stellen
- Dokumente hinzufügen
- Indexierungsstatus
- Workspace wechseln
- Modellprofil wählen
- Quellen öffnen
- Wissensbestand prüfen
- Hilfe und Systembereitschaft

Beispiel:

```text
┌ OmaRag · Baustoffkunde · EINFACH ──────────────────────┐
│                                                        │
│  [1] Frage stellen                                     │
│  [2] Dokumente hinzufügen                              │
│  [3] Indexierung ansehen                               │
│  [4] Wissensbestand prüfen                             │
│                                                        │
│  ● Bereit · 428 Dokumente · Belegmodus: Streng         │
└────────────────────────────────────────────────────────┘
```

### Werkstatt

Zusätzlich sichtbar:

- Retrieval-Inspector
- Chunk- und Kontextansicht
- Rerankingscores
- Rohkonfiguration
- Corpus-Profiler
- Ressourcenregeln
- Doctor und Reparaturen
- Evaluations- und A/B-Labor
- Snapshots, Backups und Ereignisdetails
- API- und Diagnoseinformationen

Die einfache Ebene versteckt Funktionen, entfernt sie aber nicht. Ein laufender Job oder eine Warnung bleibt in beiden Ebenen sichtbar.

## 9.8 Workspace- und Backendanzeige

Die Kopfzeile nennt immer:

```text
OmaRag · Baustoffkunde · SERVER ALSFELD · LOCAL AI · STRIKT
```

Damit ist jederzeit erkennbar:

- welcher Workspace aktiv ist,
- auf welchem Backend gearbeitet wird,
- ob lokale oder Cloudmodelle beteiligt sind,
- welcher Belegmodus gilt.

`w` öffnet den Workspace-Umschalter:

```text
● Baustoffkunde         428 Dokumente   bereit
○ DIN-Normen            112 Dokumente   1 Warnung
○ Testlabor               8 Dokumente   Indexierung 63 %
+ Neuer Workspace
```

`Alt+B` öffnet den Backend-Umschalter.

## 9.9 Befehlspalette

`Ctrl+P` durchsucht alle zulässigen Aktionen. Ergebnisse berücksichtigen Bedienebene, Capability, Schreibrechte und aktuellen Zustand.

Beispiele:

```text
Workspace wechseln
Testindexierung starten
Qualitätsprüfung ausführen
Snapshot erstellen
Hintergrundjobs pausieren
Modell entladen
Diagnosepaket erstellen
```

Gefährliche Aktionen öffnen immer einen Bestätigungsdialog und werden nicht unmittelbar aus der Palette ausgeführt.

---

# 10. Themes und Omarchy-Integration

## 10.1 Semantische Theme-Engine

Komponenten verwenden ausschließlich semantische Rollen:

```rust
pub struct Theme {
    pub mode: ThemeMode,
    pub background: Style,
    pub surface: Style,
    pub surface_alt: Style,
    pub text: Style,
    pub text_muted: Style,
    pub accent: Style,
    pub selection: Style,
    pub success: Style,
    pub warning: Style,
    pub error: Style,
    pub info: Style,
    pub border: Style,
    pub border_focused: Style,
    pub progress_done: Style,
    pub progress_remaining: Style,
}
```

Keine View darf Hexfarben direkt enthalten. Dadurch lassen sich Omarchy-Paletten, ein monochromer Modus und spätere externe Themes ohne Komponentenänderung verwenden.

## 10.2 Modus **Omarchy folgen**

OmaRag sucht in dieser Reihenfolge:

1. aktive Palette unter `~/.local/state/omarchy/current/theme/colors.toml`
2. Nutzer-Themes unter `~/.config/omarchy/themes/*/colors.toml`
3. System-Themes unter `/usr/share/omarchy/themes/*/colors.toml`
4. eingebaute OmaRag-Fallbackthemes

Bei aktivem **Omarchy folgen** beobachtet OmaRag die aktive `colors.toml`. Ein Themewechsel in Omarchy wird ohne Neustart übernommen. Es werden nur Farbdaten importiert, keine Wallpapers, Fonts, Shellskripte oder ausführbaren Themebestandteile.

## 10.3 Mitgelieferte Omarchy-Paletten

Als sofort nutzbare Fallbacks werden drei Paletten aus dem offiziellen Omarchy-Themesystem abgeleitet und mit Herkunftshinweis ausgeliefert:

| Theme | Charakter | Hintergrund | Vordergrund | Akzent |
|---|---|---:|---:|---:|
| **Tokyo Night** | ruhiges dunkles Standardtheme | `#1a1b26` | `#a9b1d6` | `#7aa2f7` |
| **Matte Black** | OLED-freundlich, hoher Kontrast | `#121212` | `#bebebe` | `#e68e0d` |
| **Catppuccin Latte** | helles, dokumentfreundliches Theme | `#eff1f5` | `#4c4f69` | `#1e66f5` |

Optional wird **Gruvbox** als warmer vierter Fallback eingebaut. Die Palette wird anhand der Omarchy-Lizenz und nicht durch Kopieren sonstiger Desktopbestandteile übernommen.

## 10.4 Abbildung von Omarchy auf OmaRag

```text
Omarchy accent              → accent, border_focused
Omarchy selection           → selection
Omarchy muted               → text_muted, progress_remaining
Omarchy background          → background
Omarchy lighter_background  → surface
Omarchy foreground          → text
Omarchy green               → success
Omarchy yellow              → warning
Omarchy red                 → error
Omarchy blue/cyan           → info
```

Fehlende Schlüssel werden aus Vordergrund, Hintergrund und Akzent mit kontrastgeprüften Ableitungen ergänzt.

## 10.5 Theme-Bedienung

- `F3`: nächstes Theme
- `Shift+F3`: vorheriges Theme
- `Ctrl+P → Theme`: Theme-Palette öffnen
- Vorschau ohne Speichern
- `Enter`: übernehmen
- `r`: Omarchy-Palette neu laden
- separate Auswahl je OmaRag-Profil möglich

---

# 11. Modulare RAG-Pipeline

## 11.1 Indexierung

```text
Quelle
  ↓
Source Adapter
  ↓
Job Queue
  ↓
Docling Converter
  ↓
optionaler Preprocessor
  ↓
OCR / Tabellen / Bilder
  ↓
Chunker
  ↓
optionaler Chunk Filter
  ↓
optionaler Metadata Enricher
  ↓
Embedding Provider
  ↓
atomarer Haiku-Import
  ↓
LanceDB
  ↓
Index / Vacuum
  ↓
Job-Ereignisse an das TUI
```

## 11.2 Abfrage

```text
Frage
  ↓
Session- und Profilregeln
  ↓
Metadatenfilter
  ↓
Query Embedding
  ↓
Hybrid Retrieval
  ├── Vektorsuche
  └── Volltextsuche
  ↓
Fusion
  ↓
optionales Reranking
  ↓
Kontextausweitung
  ↓
RAG Capability
  ↓
Antwortmodell
  ↓
Quellen- und Medienzuordnung
  ↓
normalisierter Ereignisstream
  ↓
TUI
```

OmaRag macht die Stufen sichtbar und konfigurierbar, dupliziert sie aber nicht. Für genaue Fortschrittsdaten ruft der Adapter die öffentlichen Haiku-Pipelinebausteine schrittweise auf und umschließt sie mit Messpunkten, Checkpoints und Ereignissen.

---

## 11.3 Erweiterungspunkte

Konzeptionelle Schnittstellen:

```text
SourceProvider
Preprocessor
ChunkEnricher
MetadataProvider
EmbeddingProvider
RerankingProvider
AnswerCapability
CitationEnricher
MediaRenderer
FrontendTransport
```

Im Kern existieren zunächst nur:

```text
HaikuSourceProvider
HaikuProcessingPipeline
HaikuEmbeddingProvider
HaikuRerankingProvider
HaikuAnswerCapability
```

Die Schnittstellen dienen zunächst der Entkopplung und noch nicht als öffentliches Plugin-SDK.

### Grundregel

Plugins verändern nicht direkt interne Haiku-Objekte. Sie erhalten und liefern klar definierte OmaRag-Datentypen.

Beispiel:

```python
class MetadataProvider(Protocol):
    async def enrich(
        self,
        document: DocumentDescriptor,
        metadata: dict[str, object],
    ) -> dict[str, object]:
        ...
```

Vorhandene Erweiterungspunkte von Haiku werden bevorzugt genutzt, statt eine zweite parallele Pluginwelt aufzubauen.

---


## 11.4 Preflight- und Testindexierungspipeline

Vor einer großen Indexierung kann ein repräsentativer Testlauf erfolgen:

```text
Quelle
  ↓
Datei- und Sicherheitsprüfung
  ↓
Dokumenttyp und Textschicht erkennen
  ↓
repräsentative Seiten auswählen
  ├── Titel/Inhaltsverzeichnis
  ├── normaler Fließtext
  ├── Tabellen
  ├── Abbildungen
  └── gescannte Seiten
  ↓
Haiku/Docling-Konvertierung im Previewmodus
  ↓
OCR-, Tabellen-, Bild- und Chunkanalyse
  ↓
Speicher- und Zeitabschätzung
  ↓
visueller Prüfbericht
  ↓
Profilvorschlag
  ↓
Nutzer übernimmt, verändert oder verwirft
```

Der Preview erzeugt keine produktiven Datenbankeinträge. Optional dürfen Artefakte in einem temporären, automatisch bereinigten Cache gespeichert werden.

## 11.5 Corpus-Profiler

Der Corpus-Profiler arbeitet zweistufig:

### schneller Scan

- Dateitypen und Größen
- PDF-Seitenzahlen
- vorhandene Textschicht
- Stichproben zur Sprache
- Bild- und Tabellendichte
- Hashes und mögliche Duplikate

### tiefer Scan

- OCR-Bedarf
- Layoutkomplexität
- Tabellenqualität
- Bildrelevanz
- Dokumentstruktur
- geschätzte Chunkverteilung
- Speicher- und Laufzeitmodell

Das Ergebnis beeinflusst ausschließlich Empfehlungen. Der Nutzer kann jede Empfehlung übersteuern.

## 11.6 Abfrage mit Beleg- und Gültigkeitsregeln

```text
Frage
  ↓
Workspace und Session
  ↓
Gültigkeitsrichtlinie
  ├── nur aktuell
  ├── aktuell bevorzugen
  ├── alle Versionen
  └── historischer Stichtag
  ↓
Dokumentfilter und angeheftete Quellen
  ↓
Retrieval und Reranking
  ↓
Kontextausweitung
  ↓
Antwort im Modus Streng / Normal / Erkunden
  ↓
Aussagen und Quellen zuordnen
  ↓
Belegbericht und Medienauswahl
  ↓
Antwort, Warnungen und visuelle Verankerung
```

Der Belegprüfer darf eine Antwort nicht als „sicher“ klassifizieren. Er berichtet stattdessen nachvollziehbare Eigenschaften wie Quellenanzahl, Seitenverankerung, Widersprüche und nicht ausreichend gestützte Aussagen.

---

# 12. Lokaler Betrieb und Cloudwarnungen

## 12.1 Provider-Klassifizierung

Jeder Provider erhält Metadaten:

```json
{
  "id": "ollama",
  "execution": "local",
  "sends_document_content": false
}
```

oder:

```json
{
  "id": "openai",
  "execution": "cloud",
  "sends_document_content": true
}
```

## 12.2 Sichtbare Betriebsanzeige

```text
LOCAL
```

oder:

```text
CLOUD: Antwortmodell
```

Bei Cloud-Embeddings:

```text
CLOUD: Dokumentinhalte werden für Embeddings übertragen
```

## 12.3 Technische Sperre

Eine Cloudkonfiguration wird erst aktiv, wenn im OmaRag-Profil explizit gespeichert wurde:

```toml
[privacy]
cloud_acknowledged = true
cloud_acknowledged_at = "2026-07-25T10:00:00+02:00"
```

Der Adapter lehnt eine Cloudausführung andernfalls ab. Eine bloße Warnung im TUI genügt nicht, weil ein anderes Frontend sie umgehen könnte.

## 12.4 Datenschutzprinzipien

- lokal als Standard
- keine Telemetrie
- keine automatische Fehlerübertragung
- Geheimnisse nicht in Logs
- Cloudstatus jederzeit sichtbar
- getrennte Freigabe für Embedding, Reranking, Vision und Antwortmodell
- Möglichkeit, Cloudprovider vollständig per Policy zu sperren

---

# 13. Persistenter Hintergrundbetrieb

## 13.1 Entscheidung: kein zusätzlicher Dienst

Ein Hintergrundprozess ist für fortlaufende Indexierung zwingend nötig, aber OmaRag besitzt bereits `omaragd`. Daher lautet die verbindliche Entscheidung:

> **`omaragd` ist API-Brücke, Jobdienst und Scheduler in einem. Es gibt keinen separaten `omarag-worker`.**

Das hält Installation, Speicherbedarf und Fehlerdiagnose klein.

## 13.2 Adaptive Lebensdauer

```text
TUI startet
   ↓
Socket/Healthcheck vorhanden? ── ja ──► verbinden
   │ nein
   ▼
omaragd starten
   ↓
TUI wird geschlossen
   ↓
aktive Jobs? ── ja ──► weiterarbeiten
   │ nein
   ▼
weitere Clients? ── ja ──► weiterlaufen
   │ nein
   ▼
nach Idle-Timeout sauber beenden
```

Konfigurierbare Modi:

| Modus | Verhalten |
|---|---|
| `adaptive` | automatisch starten; bei Jobs bleiben; nach Leerlauf beenden |
| `always-on` | als User-Service dauerhaft verfügbar |
| `session` | mit TUI starten und nur ohne aktive Jobs beenden |
| `external` | TUI verbindet sich mit Docker- oder Serverdaemon |

## 13.3 Persistente Queue

Der Daemon speichert Job- und Ereigniszustand in SQLite mit WAL-Modus:

```sql
jobs(
  id, kind, state, priority, created_at, started_at, finished_at,
  profile_id, source_manifest, current_phase, progress, error_code
)

tasks(
  id, job_id, sequence, kind, state, work_done, work_total,
  checkpoint_key, attempt, lease_owner, lease_expires_at
)

job_events(
  seq, job_id, timestamp, type, payload_json
)

throughput_samples(
  hardware_fingerprint, profile_hash, file_kind, phase,
  units, duration_ms, recorded_at
)
```

Die Datenbank liegt standardmäßig unter:

```text
$XDG_STATE_HOME/omarag/jobs.sqlite3
```

## 13.4 Checkpoints

Sichere Wiederaufnahmepunkte:

1. Quelle erkannt und gehasht
2. Datei lokal verfügbar
3. Docling-Konvertat und Seitenmetadaten im Cache
4. Chunks serialisiert
5. jeweils abgeschlossener Embedding-Batch
6. atomarer Haiku-Import abgeschlossen
7. Indexoptimierung abgeschlossen

Ein Job beginnt nach einem Absturz am letzten sicheren Punkt. Nicht atomar unterbrechbare Haiku-Schritte werden entweder zu Ende geführt oder beim nächsten Start vollständig wiederholt; ein halbfertiges Dokument wird nicht als erfolgreich markiert.

## 13.5 Pause, Fortsetzen und Abbruch

- **Weiche Pause:** keine neue Task wird begonnen; der aktuelle atomare Schritt darf sauber enden.
- **Schnelle Pause:** die aktuelle asynchrone Arbeit wird am nächsten sicheren Checkpoint angehalten.
- **Fortsetzen:** Queue übernimmt den nächsten offenen Task.
- **Abbruch:** temporäre Artefakte ohne Referenz werden bereinigt; bestehende erfolgreiche Dokumente bleiben erhalten.

Die Taste `Space` pausiert oder setzt den markierten Job fort. `p` pausiert alle Jobs. Das Schließen des TUI ist **kein** Abbruchsignal.

## 13.6 Haiku-Ingester-Adapter

Der Haiku-Ingester wird dort verwendet, wo seine vorhandenen Stärken passen:

- beobachtete Verzeichnisse und andere Quellen
- persistente Queue
- Wiederholungen
- Dead-Letter Queue
- Lease- und Heartbeat-Verhalten
- Single-Writer-Betrieb

Für interaktive Einzel- und Stapelimporte bevorzugt OmaRag die instrumentierte öffentliche Haiku-Pipeline. Beide Wege werden in dasselbe OmaRag-Jobmodell normalisiert.

## 13.7 Systemd-User-Service

```ini
[Unit]
Description=OmaRag background service
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/lib/omarag/omarag service run
Restart=on-failure
RestartSec=3
Environment=OMARAG_MODE=always-on

[Install]
WantedBy=default.target
```

Der Dienst wird nur nach Zustimmung installiert. Portable AppImage-Nutzung funktioniert auch ohne systemd.

## 13.8 Ressourcenverhalten

- nur ein LanceDB-Writer
- Workerzahl standardmäßig hardwareabhängig, konservativ `1`
- CPU- und I/O-Priorität optional reduzierbar
- Modelle nach konfigurierbarer Leerlaufzeit entladen
- kein Polling im Sekundentakt; Ereignisse und Dateiwächter bevorzugen
- Queue bleibt auch ohne geladenes Modell verfügbar
- Status- und Fortschrittsabfragen dürfen die Indexierung nicht blockieren

---

# 14. Fehlerbehandlung

Alle API-Fehler verwenden ein einheitliches Format:

```json
{
  "code": "EMBEDDING_DIMENSION_MISMATCH",
  "message": "Das aktive Embedding-Modell erzeugt 1024 statt 768 Dimensionen.",
  "user_action": "Neuen Index anlegen oder vollständigen Rebuild starten.",
  "retryable": false,
  "correlation_id": "err-c91d",
  "details": {
    "database_dimension": 768,
    "model_dimension": 1024
  }
}
```

Wichtige Fehlercodes:

```text
BACKEND_UNAVAILABLE
HAIKU_VERSION_UNSUPPORTED
CONFIG_INVALID
CONFIG_CONFLICT
MIGRATION_REQUIRED
REBUILD_REQUIRED
EMBEDDING_DIMENSION_MISMATCH
DATABASE_BUSY
DATABASE_NOT_OPEN
PROVIDER_UNAVAILABLE
PROVIDER_AUTH_FAILED
CLOUD_CONSENT_REQUIRED
DOCUMENT_UNSUPPORTED
DOCUMENT_IMPORT_FAILED
RUN_CANCELLED
RUN_LIMIT_REACHED
MEDIA_UNAVAILABLE
DEPENDENCY_MISSING
DEPENDENCY_UPDATE_FAILED
DEPENDENCY_ROLLBACK_FAILED
MODEL_INCOMPATIBLE
MODEL_PULL_FAILED
HARDWARE_SCAN_FAILED
JOB_PAUSE_PENDING
CHECKPOINT_INVALID
APPIMAGE_UPDATE_FAILED
```

Das TUI zeigt zuerst die verständliche Meldung. Technische Details sind über die Detail- oder Systemansicht erreichbar.

---

# 15. Sicherheit

Mindestanforderungen für Version 0.3:

- Bindung ausschließlich an Loopback
- zufälliges Bearer-Token pro eingebetteter Sitzung
- kein CORS im lokalen Standardmodus
- Authentifizierung zwingend bei Nicht-Loopback-Bindung
- Geheimnisse niemals in Logs oder SSE-Ereignissen
- API-Schlüssel im TUI nur maskiert
- bevorzugt `${UMGEBUNGSVARIABLE}` statt Klartext
- Pfad-Whitelist für lokale Importe
- URL-Schema-Whitelist für entfernte Quellen
- keine Shellausführung aus Dateinamen oder Providerparametern
- Größenlimits für Uploads
- Zeit- und Ressourcenlimits
- keine Telemetrie
- Cloudanbieter standardmäßig deaktiviert
- Installationsquellen, Versionen und Prüfsummen vor Ausführung sichtbar
- keine Aktualisierung während eines mutierenden Datenbankjobs
- atomarer Runtimewechsel mit Rollback auf die vorherige Haiku-Umgebung
- Docker-Port standardmäßig nur an Loopback binden
- Remotezugriff bevorzugt über SSH-Tunnel oder VPN
- TLS und langlebige Tokens nur bei bewusstem Serverbetrieb
- Workspace-Export ohne Secrets
- Backups vor Restore per Prüfsumme und Manifest validieren
- Idempotency-Key und CSRF-unabhängige Bearer-Authentifizierung für mutierende Requests
- Pluginrechte vor Installation sichtbar machen
- Plugins standardmäßig nicht im Hauptprozess ausführen
- MCP-Schreibwerkzeuge standardmäßig deaktivieren
- Diagnosepakete vor Export redigieren und anzeigen
- Auditereignisse für Restore, Massenlöschung, Cloudfreigabe und MCP-Schreibzugriffe

---

# 16. Bilder und Tabellen im TUI

OmaRag soll pro fachlicher Antwort nach Möglichkeit **zwei bis drei wirklich passende Quellenbilder** zeigen. Die Zahl ist ein Ziel, keine Pflicht: Eine Qualitätsgrenze verhindert, dass irrelevante Bilder nur zum Auffüllen erscheinen.

## 16.1 Herkunft der Bilder

Bevorzugte Reihenfolge:

1. direkt mit einem verwendeten Chunk verknüpfte Abbildung
2. Tabelle, Diagramm oder Foto derselben Seite
3. relevanter Seitenausschnitt um die zitierte Textstelle
4. Abbildung einer unmittelbar benachbarten Seite im gleichen Abschnitt
5. gespeichertes Dokumentbild, das über multimodale Suche gefunden wurde

OmaRag generiert an dieser Stelle keine Bilder. Jede Darstellung bleibt mit Dokument, Seite, Abschnitt und Chunk verknüpft.

## 16.2 Medienauswahl

Der Daemon sammelt Kandidaten und berechnet einen nachvollziehbaren Rang:

```text
35 % Retrieval- oder Reranking-Score
25 % direkte Chunk-, Seiten- oder Referenznähe
20 % semantische Ähnlichkeit von Bildunterschrift und Frage
10 % Quellenvertrauen und Zitierstatus
10 % visuelle Qualität und Nutzbarkeit
```

Danach gelten:

- Perceptual-Hash gegen Dubletten
- MMR beziehungsweise Diversitätsregel gegen drei fast gleiche Bilder
- höchstens ein Bild pro Seite, außer eine Tabelle und eine Abbildung ergänzen sich
- Mindestscore, standardmäßig `0,62`
- Ziel `3`, wenn drei starke Kandidaten vorhanden sind
- sonst `2`, `1` oder kein Bild

Die Gewichte und Schwelle sind konfigurierbar, aber im einfachen Einstellungsmodus verborgen.

## 16.3 Medienvertrag

```json
{
  "answer_id": "run-193",
  "target_count": 3,
  "selected": [
    {
      "media_id": "media-9",
      "kind": "table",
      "document_id": "doc-42",
      "title": "Konsistenzklassen F1 bis F6",
      "page": 113,
      "chunk_ids": ["chunk-123"],
      "score": 0.91,
      "mime_type": "image/png",
      "width": 1280,
      "height": 746,
      "thumbnail_url": "/v1/media/media-9/thumbnail"
    }
  ]
}
```

## 16.4 Terminalprotokolle

`ratatui-image` übernimmt Erkennung und Darstellung in dieser Reihenfolge:

- Kitty Graphics Protocol
- Sixel
- iTerm2 Inline Images
- Unicode-Halfblock-Fallback
- reine Textkarte, falls Farbe oder Grafik nicht möglich ist

Bilddekodierung, Verkleinerung, Protokollvorbereitung und Cachezugriff laufen in Hintergrundtasks. Der Immediate-Mode-Renderpfad erhält nur fertige Renderobjekte.

## 16.5 Darstellung je Layout

### Fokus

- zwei Miniaturen nebeneinander in der Evidenzleiste
- drittes Bild darunter oder per Galerieumschaltung
- `Enter` öffnet die große Ansicht

### Werkbank

- Medien-Tab im Inspektor
- Bild, Metadaten, Score, Seite und zugehöriger Chunk gleichzeitig sichtbar
- `[` und `]` wechseln Kandidaten

### Zen

- kompakter Hinweis wie `[2 passende Bilder öffnen]`
- Galerie als Vollbild-Overlay

## 16.6 Bildviewer

Funktionen:

- einpassen, Originalgröße und Zoom
- Quelle und Seite einblenden
- zwischen zwei bis drei Bildern wechseln
- Originalseite oder externen Viewer öffnen
- Bildpfad kopieren
- alternative Kandidaten anzeigen
- Bild für diese Antwort ausblenden

Fallback:

```text
┌ Tabelle · Betontechnologie · Seite 113 ─────────────────────┐
│ Konsistenzklassen F1 bis F6                                  │
│ 1280 × 746 · PNG · Relevanz 0,91                             │
│ Enter: öffnen · o: extern · q: schließen                     │
└───────────────────────────────────────────────────────────────┘
```

## 16.7 Cache

```text
$XDG_CACHE_HOME/omarag/media/
├── originals/
├── thumbnails/
└── terminal/
```

Der Cache ist in der Größe begrenzt, content-addressed und jederzeit löschbar. Quellenbilder in der Haiku-Datenbank werden nicht durch Cachebereinigung gelöscht.

## 16.8 Tabellen

Darstellungsreihenfolge:

1. strukturierte Tabelle
2. Markdown-Tabelle
3. narrative Tabellenbeschreibung
4. Bildvorschau
5. Originalseite

Bei sehr breiten Tabellen kann der Nutzer horizontal scrollen oder in einen Vollbild-Tabellenmodus wechseln.

---

# 17. Zusätzliche OmaRag-Subsysteme

## 17.1 Verarbeitungsprofile

Beispiele:

```text
Schneller PDF-Import
Deutsches Fachbuch
Gescanntes Dokument
Normensammlung
Tabellenlastiges Dokument
Bildlastiges Handbuch
```

Ein Profil setzt mehrere Haiku-Werte gemeinsam, bleibt aber vollständig als normale Haiku-Konfiguration sichtbar.

### Beispiel „Deutsches Fachbuch“

- deutsche OCR-Sprache
- hierarchisches Chunking
- moderate Chunkgröße
- Tabellen als Markdown
- Seitennummern erhalten
- automatische Titelerkennung
- Bildbeschreibungen aktiv
- lokales Embedding
- Reranker aktiv

## 17.2 Abhängigkeits- und Updateverwaltung

### 17.2.1 Komponentenstatus

Jede Abhängigkeit besitzt einen eindeutigen Zustand:

```text
Missing
InstalledStopped
Ready
UpdateAvailable
Unsupported
Broken
Remote
```

Das Dashboard zeigt mindestens:

| Komponente | Gefunden | Aktiv | Verfügbar | Quelle | Aktion |
|---|---:|---:|---:|---|---|
| Haiku RAG | 0.70.0 | ja | dynamisch geprüft | OmaRag Runtime | prüfen |
| Ollama | API-Version | ja | dynamisch geprüft | lokaler Dienst | prüfen |
| Python | 3.12.x | ja | kompatibel | uv managed | Details |
| Docling-Modelle | vollständig | – | – | Cache | verwalten |

### 17.2.2 Haiku-Erkennung

Reihenfolge:

1. von OmaRag verwaltete Runtime
2. im Profil festgelegte Python-Umgebung
3. auf `PATH` vorhandenes `haiku-rag`
4. Python-Metadaten über `importlib.metadata`

Systeminstallationen werden standardmäßig nur gelesen. OmaRag empfiehlt eine isolierte, verwaltete Umgebung unter:

```text
$XDG_DATA_HOME/omarag/runtime/haiku/<runtime-id>/
```

`uv` verwaltet Python, virtuelle Umgebung und den gesperrten Haiku-Abhängigkeitsstand. Fehlt ein passendes Python, darf `uv` nach sichtbarer Zustimmung eine verwaltete Version installieren.

### 17.2.3 Ollama-Erkennung

Reihenfolge:

1. konfigurierte API-URL über `GET /api/version`
2. lokale Standard-URL `http://127.0.0.1:11434/api/version`
3. `ollama --version` auf `PATH`
4. erkannter User- oder Systemdienst
5. nicht vorhanden

Installierte Modelle kommen aus `/api/tags`, Details aus `/api/show`, aktive Speicherbelegung aus `/api/ps`. Downloads laufen über den streamenden Pull-Endpunkt, sodass der Fortschritt im TUI sichtbar bleibt.

### 17.2.4 Installationsplan

OmaRag lädt nichts still herunter. Vorher erscheint:

```text
Installationsplan
────────────────────────────────────────────────────────────
Komponente: Haiku RAG
Version:    0.70.x, von OmaRag freigegebener Kanal
Quelle:     Python Package Index über uv
Ziel:       ~/.local/share/omarag/runtime/haiku/...
Download:   wird vorab aus Paket- und Modellplan ermittelt
Rechte:     keine Administratorrechte
Folgen:     neuer Runtime-Slot, bestehender Slot bleibt erhalten

[Installieren] [Details] [Abbrechen]
```

Für Ollama gelten plattformspezifische Installer. Benötigt die Systeminstallation erhöhte Rechte, zeigt OmaRag den exakten Vorgang und verwendet nur nach Bestätigung einen grafischen beziehungsweise terminalbasierten Privilegienhelfer. AppImage und TUI führen niemals unbemerkt `sudo` aus.

### 17.2.5 Updatekanäle

```text
Aus          keine Netzprüfung
Manuell      nur auf ausdrücklichen Befehl
Hinweisen    beim Start aus Cache, Netzprüfung höchstens täglich
Stabil       kompatible stabile Updates anbieten
Vorschau     freigegebene Pre-Releases anbieten
```

Standard: **Hinweisen + Stabil**.

Lokaler Status wird bei jedem Start geprüft. Eine Netzwerkprüfung geschieht nur, wenn der Cache älter als 24 Stunden ist oder der Nutzer sie manuell auslöst. Der Start des TUI wird dadurch nicht blockiert.

### 17.2.6 Sichere Haiku-Aktualisierung

```text
Jobs anhalten oder auslaufen lassen
  ↓
neuen Runtime-Slot neben dem alten erzeugen
  ↓
Haiku und Abhängigkeiten installieren
  ↓
Adaptervertrag + Doctor + Testdatenbank prüfen
  ↓
Migration/Rebuild-Auswirkung anzeigen
  ↓
aktiven Symlink atomar umschalten
  ↓
Healthcheck
  ├─ erfolgreich → alten Slot vorerst behalten
  └─ fehlerhaft  → automatischer Rollback
```

Eine Haiku-Aktualisierung darf nie ungefragt eine Datenbankmigration starten.

### 17.2.7 Ollama- und Modellupdates

Ollama-Binärversion und Modellinhalt sind getrennte Updates:

- Runtimeupdate: neue Ollama-Version installieren und Dienst kontrolliert neu starten
- Modellupdate: denselben Tag erneut pullen und Digest vergleichen
- Embeddingmodell: Digest und Dimension werden Teil der Datenbankidentität; Änderung kann Rebuild verlangen
- Chat- oder Visionmodell: kann nach Testlauf ohne Re-Embedding gewechselt werden
- Updates werden nicht mitten in einer laufenden Indexierung angewendet

## 17.3 Hardwareerkennung und Modellberater

### 17.3.1 Erfasste Daten

```text
Betriebssystem, Architektur und Kernel
CPU-Modell, Kerne, Threads und relevante Instruktionssätze
Gesamt- und verfügbarer RAM, Swap
GPU-Hersteller, Modell, VRAM oder Unified Memory
sichtbare GPU-Geräte in Docker/cgroups
freier Speicherplatz und Dateisystem
Ollama-Backend und tatsächlich geladene CPU-/GPU-Anteile
optional: gemessener Datenträger- und Modelldurchsatz
```

Unter Linux werden bevorzugt `/proc`, `/sys/class/drm`, PCI-Informationen und cgroup-Grenzen verwendet. Externe Werkzeuge wie `nvidia-smi`, `rocm-smi` oder `vulkaninfo` sind optionale Ergänzungen, keine zwingenden Abhängigkeiten.

### 17.3.2 Keine starre 1–10-Magie

OmaRag darf zusätzlich eine leicht verständliche Systemklasse `1–10` anzeigen. Die Entscheidung beruht aber intern auf einem transparenten Capability-Vektor:

```json
{
  "cpu_score": 0.58,
  "memory_gib_effective": 15.2,
  "gpu_vram_gib_effective": 2.0,
  "unified_memory": true,
  "disk_free_gib": 412,
  "container_memory_limit_gib": null,
  "ollama_acceleration": "vulkan-partial"
}
```

In Containern zählt die cgroup-Grenze, nicht der möglicherweise größere Host-RAM.

### 17.3.3 Modellkatalog

Das TUI enthält keine fest codierte Topliste. OmaRag verwendet einen signierten, versionierten Katalog mit Offline-Fallback:

```json
{
  "catalog_version": 1,
  "verified_at": "2026-07-25T00:00:00Z",
  "models": [
    {
      "id": "qwen3.5:4b",
      "runtime": "ollama",
      "roles": ["chat", "vision"],
      "modalities": ["text", "image"],
      "languages": ["de", "en", "multilingual"],
      "download_bytes": 3400000000,
      "minimum_ollama": "catalog-defined",
      "context_options": [8192, 16384, 32768],
      "stability": "stable",
      "source": "ollama-library"
    }
  ]
}
```

Der Katalog wird als Release-Artefakt von OmaRag aktualisiert, lokal signaturgeprüft und höchstens wöchentlich automatisch neu geladen. Ein Nutzer kann einen eigenen Katalog hinzufügen.

### 17.3.4 Getrennte Modellrollen

Empfehlungen erfolgen separat für:

- **Chat/QA**
- **Embedding**
- **Reranking**
- **Vision/Bildbeschreibung**

Ein Modell wird nicht nur deshalb für alle Rollen empfohlen, weil es auf der Hardware startet.

### 17.3.5 Fit- und Rankingmodell

Zuerst gilt ein harter Eignungstest:

```text
Modellgewichte
+ KV-/Kontextspeicher
+ Laufzeit-Overhead
+ konkurrierende OmaRag- und Docling-Prozesse
+ Sicherheitsreserve von typischerweise 15–25 %
≤ effektiv verfügbarer RAM/VRAM
```

Danach kann ein Score verwendet werden:

```text
33 % erwartete Qualität für die Rolle
25 % erwartete Geschwindigkeit
17 % Speicherreserve
10 % Deutsch-/Mehrsprachigkeit
 8 % benötigte Fähigkeiten wie Vision oder Tools
 7 % Aktualität und Stabilität
```

Die Gewichte sind profilabhängig.

### 17.3.6 Nutzerprofile

- **Sparsam:** geringe Last, kleine Modelle, gute Akkulaufzeit
- **Ausgewogen:** Standardempfehlung
- **Qualität:** größere Modelle und längere Antwortzeit akzeptiert
- **Manuell:** keine automatische Vorauswahl

Jede Empfehlung zeigt eine Begründung:

```text
Empfohlen: qwen3.5:4b
✓ passt mit Reserve in den effektiven Arbeitsspeicher
✓ Text und Bild in einem Modell
✓ mehrsprachig
~ voraussichtlich mittlere CPU-Latenz
! 32K statt maximalem Kontext empfohlen
```

### 17.3.7 Freie Auswahl

Der Nutzer kann:

- jedes installierte Modell auswählen
- beliebige Ollama-Tags eintragen
- ein nicht empfohlenes Modell trotzdem pullen
- Kontext, Quantisierung und Parallelität abweichend einstellen
- Empfehlungen dauerhaft ignorieren

OmaRag zeigt Warnungen, aber sperrt nicht aus Bequemlichkeit. Blockiert werden nur fehlende Providerfähigkeit, inkompatible Embeddingdimension oder nachweislich unmögliche Runtimevoraussetzungen.

### 17.3.8 Optionaler Kurzbenchmark

Nach der statischen Erkennung kann der Nutzer einen kurzen Benchmark starten:

- Zeit bis zum ersten Token
- Ausgabe-Token pro Sekunde
- Embedding-Chunks pro Sekunde
- Spitzen-RAM und VRAM
- CPU-/GPU-Offload laut Ollama
- Vision-Latenz mit kleinem Testbild

Die Messwerte überschreiben keine Auswahl automatisch, verbessern aber die lokale Rangfolge und ETA-Berechnung.

### 17.3.9 Referenzprofile des Projekts

Diese Systeme werden als feste Akzeptanzprofile getestet:

| Profil | Hardware | Erwartete Kandidaten, endgültig per Benchmark |
|---|---|---|
| Yoga | Ryzen 5 8640HS, 16 GiB RAM, Radeon 760M mit 2 GiB nutzbarem VRAM | Chat `2b–4b`, Embedding bevorzugt kleine Variante, Vision vorsichtig `4b`, Worker `1` |
| Server | 10 vCPU, 24 GiB RAM, keine GPU | Chat `4b–9b` je Latenzziel, Embedding klein bis mittel, Worker konservativ `1–2` |

Aktuelle Modellnamen dienen nur als Katalogbeispiele. Tatsächliche Quantisierung, Kontextgröße, übrige Docker-Dienste und gemessene Leistung entscheiden.

## 17.4 Genaue Fortschrittsanzeige und ETA

### 17.4.1 Messbare Phasen

| Phase | anfänglicher Prior | konkrete Einheiten |
|---|---:|---|
| Entdecken und Hashen | 3 % | Dateien, Bytes |
| Kopieren oder Laden | 5 % | Bytes |
| Docling, OCR, Tabellen, Bilder | 40 % | Seiten, OCR-Seiten, Tabellen, Bilder |
| Chunking | 7 % | Seiten, Chunks |
| Bildbeschreibungen | 10 % optional | Bilder |
| Embeddings | 27 % | Chunks, Batches, Tokens |
| atomarer Import | 5 % | Dokumente, Datenbankzeilen |
| Indexoptimierung | 3 % | Indexsegmente |

Diese Prozente sind nur Startwerte. Nicht verwendete Phasen werden entfernt und die Gewichte neu normalisiert.

### 17.4.2 Dynamischer Arbeitsplan

Vor dem Import ermittelt ein schneller Preflight soweit möglich:

- Zahl und Größe der Dateien
- PDF-Seitenzahlen
- Dateityp und Scanwahrscheinlichkeit
- vorhandene Cacheartefakte
- voraussichtliche OCR- und Bildarbeit

Während der Verarbeitung werden unbekannte Größen ergänzt. Die Gesamtanzeige wird geglättet neu gewichtet und springt niemals rückwärts.

### 17.4.3 ETA

Die ETA basiert nicht nur auf Prozenten, sondern auf gemessenen Work Units:

```text
Restzeit pro Phase = verbleibende Einheiten / geglätteter Durchsatz
Gesamt-ETA = Summe offener Phasen + Queuewartezeit
```

Verwendet werden:

- exponentiell gewichteter Durchsatz
- robuste Medianwerte der letzten Batches
- Ausreißerbegrenzung
- historische Werte für Hardwarefingerprint, Profil, Dateityp und Modell
- Aufschlag für noch nicht vermessene Phasen

Vor ausreichenden Messwerten steht **„wird kalibriert“**. Danach zeigt die UI eine Spanne, beispielsweise `12–18 min`, plus Konfidenz `niedrig`, `mittel` oder `hoch`. Falsche Sekundengenauigkeit wird vermieden.

### 17.4.4 Anzeige

```text
Gesamt      ████████████░░░░░░░  61,8 %
Dateien     5 / 12
Seiten      1.274 / 2.068
Aktuell     Embeddings · DIN-Beton.pdf
Chunks      1.482 / 2.016 · 18,4/s
ETA         12–18 min · Konfidenz mittel
Checkpoint  Embedding-Batch 46 gespeichert
```

Detailansicht:

```text
✓ Dateien erfassen             12/12       00:03
✓ Laden und hashen             8,4 GiB     01:17
✓ PDF/OCR                      2.068 S.    18:42
✓ Chunking                     2.016       00:19
▶ Embeddings                   1.482/2.016 01:20
· Datenbankimport              ausstehend
· Indexoptimierung             ausstehend
```

### 17.4.5 Genauigkeitsgrenzen

- OCR-Seiten können stark unterschiedlich lange dauern.
- Bildbeschreibungsmodelle variieren mit Bildinhalt und Runtime.
- Netzwerkmodelle besitzen schwankende Latenz.
- Die UI nennt daher eine grobe Spanne und zeigt ihre Konfidenz.
- Nach einem Modell- oder Profilwechsel werden alte Messwerte nicht blind weiterverwendet.

## 17.5 AppImage-Distribution

### 17.5.1 Artefakt

```text
OmaRag-<version>-x86_64.AppImage
```

Später kann ein getrenntes `aarch64`-Artefakt folgen. Das AppImage enthält:

- Rust-TUI und Launcher
- Startlogik für `omaragd`
- OmaRag-Python-Bridge als Wheel oder eingebettete Quelle
- `uv` als Runtime-Bootstrap
- Fallback-Modellkatalog und Kompatibilitätsmatrix
- Desktopdatei, Icon, Metainfo und Lizenzen

Nicht enthalten:

- Ollama-Modellgewichte
- vollständige Fachbuchdatenbank
- zwingend eine riesige vorinstallierte Python-/Docling-Umgebung

### 17.5.2 Erster Start

```text
AppImage starten
  ↓
Abhängigkeiten erkennen
  ↓
verwaltete Python-Runtime und Haiku nur bei Bedarf installieren
  ↓
Ollama finden oder Installationsplan anbieten
  ↓
Hardwareprofil erstellen
  ↓
Modelle vorschlagen und nach Auswahl pullen
  ↓
OmaRag öffnen
```

Damit bleibt das AppImage handhabbar, obwohl Haiku, Docling und Modelle groß sein können.

### 17.5.3 Portable und integrierte Nutzung

```bash
./OmaRag.AppImage
./OmaRag.AppImage --install
./OmaRag.AppImage service install
./OmaRag.AppImage service status
```

`--install` kopiert das AppImage in einen stabilen Nutzerpfad, integriert Desktopdatei und Icon und erlaubt die optionale User-Service-Installation. Reiner Portable-Modus bleibt möglich.

### 17.5.4 Updates

- AppImage wird mit `linuxdeploy --output appimage` gebaut.
- Updateinformation und `.zsync` werden eingebettet.
- Updateprüfung ist opt-in oder Hinweis-basiert.
- Download erfolgt neben die laufende Datei.
- Signatur und Prüfsumme werden vor Umschalten geprüft.
- Das laufende AppImage ersetzt sich nicht mitten in einem Indexjob.

### 17.5.5 Releasequalität

Jedes Release enthält:

- SHA-256-Prüfsummen
- Signatur
- SBOM
- Lizenzübersicht
- reproduzierbare Versionsmetadaten
- Changelog und Migrationshinweise

## 17.6 Docker-Distribution

### 17.6.1 Bilder

```text
ghcr.io/<org>/omarag-daemon:<version>
ghcr.io/<org>/omarag-tui:<version>      # optional
```

Der Daemon-Container enthält Haiku, Bridge und Verarbeitungskomponenten. Ollama bleibt ein eigener offizieller Container oder ein externer Dienst.

### 17.6.2 Compose-Profile

- `cpu`: OmaRag + Ollama CPU
- `nvidia`: GPU über NVIDIA Container Toolkit
- `amd`: Zugriff auf `/dev/kfd` und `/dev/dri`
- `external-ollama`: nur OmaRag-Daemon, externe `OLLAMA_BASE_URL`
- optional `docling-serve`: separater Konverter für größere Installationen

### 17.6.3 Beispiel

```yaml
services:
  ollama:
    image: ollama/ollama
    restart: unless-stopped
    volumes:
      - ollama-models:/root/.ollama
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 10s
      timeout: 5s
      retries: 12

  omaragd:
    image: ghcr.io/example/omarag-daemon:${OMARAG_VERSION:-latest}
    restart: unless-stopped
    depends_on:
      ollama:
        condition: service_healthy
    environment:
      OLLAMA_BASE_URL: http://ollama:11434
      OMARAG_BIND: 0.0.0.0:8766
    ports:
      - "127.0.0.1:8766:8766"
    volumes:
      - omarag-data:/data
      - omarag-config:/config
      - omarag-state:/state
      - omarag-cache:/cache

volumes:
  ollama-models:
  omarag-data:
  omarag-config:
  omarag-state:
  omarag-cache:
```

### 17.6.4 Empfohlene Bedienung

Die beste Kombination ist häufig:

```text
AppImage-TUI auf dem Host
          ↓ localhost:8766
Docker-Backend mit omaragd + Ollama
```

So erhält das TUI vollen Zugriff auf Terminalgrafikprotokolle und der RAG-Stack bleibt sauber containerisiert. Das optionale TUI-Image erhält `stdin_open: true` und `tty: true`, ist aber nicht der bevorzugte Bildmodus.

### 17.6.5 Docker-Regeln

- Modelle nicht in Images einbacken, sondern in Volumes speichern
- Healthchecks und `depends_on: condition: service_healthy`
- API-Port standardmäßig nur an `127.0.0.1` veröffentlichen
- Queue, Konfiguration und LanceDB persistent mounten
- cgroup-Limits in Hardwareempfehlungen berücksichtigen
- CPU-, NVIDIA- und AMD-Konfiguration getrennt dokumentieren
- Containerupdates erst nach Queue-Drain oder Pause durchführen

## 17.7 Evaluationsgrundlage

OmaRag verwendet Haikus vorhandene Evaluationsfähigkeiten als technische Grundlage und ergänzt workspacebezogene Testfälle, Historie, Profilvergleiche und eine benutzerfreundliche Darstellung. Die ausführliche Spezifikation folgt in den Abschnitten 17.17, 17.18 und 17.24.

## 17.8 Atlas-Anbindung

Atlas verwendet dieselbe workspacebezogene API:

```text
atlas.search          → POST /v1/workspaces/{workspace}/search
atlas.ask             → POST /v1/workspaces/{workspace}/runs
atlas.openCitation    → GET  /v1/workspaces/{workspace}/citations/{id}
atlas.importDocument  → POST /v1/workspaces/{workspace}/documents/ingest
atlas.watchJob        → GET  /v1/jobs/{id}/events
```

Atlas erhält stabile Quellenreferenzen statt bloß kopiertem Text.

## 17.9 Pluginvorbereitung

Mögliche Pluginarten:

- Quellenadapter
- Metadatenextraktoren
- Chunk-Nachverarbeitung
- Promptprofile
- Modellkatalogquellen
- Medienrenderer
- Frontendintegrationen

Ein öffentliches Plugin-SDK folgt erst nach Stabilisierung von API, Workspaceformat, Berechtigungsmodell und Isolation. Abschnitt 17.29 legt die Sicherheitsgrundsätze fest.

---
## 17.10 Workspaces

Workspaces sind die primäre Organisationseinheit.

### 17.10.1 Verzeichnislayout

```text
Baustoffkunde.omarag/
├── workspace.toml
├── haiku.rag.yaml
├── sources.yaml
├── metadata-overlays/
│   └── documents.jsonl
├── database/
├── evaluations/
│   ├── core.yaml
│   └── history/
├── annotations/
├── reports/
├── snapshots/
├── backup-manifests/
└── .omarag/
    ├── locks/
    ├── queue-links.json
    └── runtime-state.json
```

`.omarag/runtime-state.json` ist nicht für portable Exporte bestimmt.

### 17.10.2 Workspace-Manifest

```toml
schema_version = 1
id = "ws-baustoffkunde"
name = "Baustoffkunde"
created_at = "2026-07-25T12:00:00Z"

[haiku]
compatible_range = ">=0.70,<0.71"
database_schema_version = "detected"

[embedding]
provider = "ollama"
model = "example-embedding-model"
vector_dimension = 1024

[defaults]
processing_profile = "deutsches-fachbuch"
evidence_mode = "strict"
document_policy = "prefer-current"

[privacy]
mode = "local"
cloud_acknowledged = false
```

Konkrete Modellnamen im Beispiel sind Platzhalter; reale Empfehlungen kommen aus dem Modellkatalog.

### 17.10.3 Workspace-Operationen

- erstellen
- öffnen und schließen
- klonen
- read-only öffnen
- archivieren
- exportieren und importieren
- auf ein anderes Backend übertragen
- gegen ein Backup verifizieren
- reparieren
- löschen

### 17.10.4 Identitätsregeln

Ein Workspace gilt als technisch identisch nur, wenn mindestens übereinstimmen:

- Workspace-ID
- Datenbankidentität
- Embeddingprovider und -modell
- Vektordimension
- relevante Chunkingparameter
- Dokumentmanifest

Abweichungen werden sichtbar gemacht, nicht still ignoriert.

## 17.11 Zwei Bedienebenen

Die einfache Ebene ist kein separater Codepfad. Beide Ebenen verwenden denselben AppState und dieselben Actions.

```text
Einfach:
  Fragen
  Dokumente
  Fortschritt
  Modellprofil
  Qualität
  Workspace
  Hilfe

Werkstatt:
  alles aus Einfach
  plus Inspector
  Konfiguration
  Corpus-Profiler
  Ressourcen
  Backups
  Evaluation
  A/B-Labor
  Diagnose
```

Capability- oder Berechtigungsgrenzen gelten unabhängig von der Bedienebene.

## 17.12 Indexierungs-Testlauf

### 17.12.1 Ziele

- fehlerhafte OCR- oder Tabellenkonfiguration früh erkennen
- erwartete Laufzeit und Speicherbedarf schätzen
- Chunkgrenzen sichtbar prüfen
- ungeeignete Bildverarbeitung vermeiden
- Verarbeitungsprofil begründet vorschlagen

### 17.12.2 Repräsentative Seitenauswahl

Automatische Auswahl bevorzugt:

1. Titelseite
2. Inhaltsverzeichnis
3. mehrere normale Textseiten
4. Seite mit hoher Tabellendichte
5. Seite mit Abbildungen
6. Seite ohne Textschicht
7. Seite mit komplexem Mehrspaltenlayout
8. letzte Inhaltsseite

Der Nutzer kann Seiten ersetzen oder ergänzen.

### 17.12.3 Previewbericht

```text
Testindexierung · Betontechnologie.pdf

Textschicht                 gut
OCR erforderlich            18 % der Stichprobe
Sprache                     Deutsch, einzelne englische Begriffe
Überschriftenstruktur       gut
Tabellen                    7/8 strukturiert erkannt
Abbildungen                 12 erkannt, 8 fachlich relevant
Chunkgrenzen                2 Warnungen
Seitennummern               erhalten

Vorschlag:
„Deutsches Fachbuch mit Tabellen“

Geschätzte Vollverarbeitung:
Dauer                       24–36 min
dauerhafter Speicher        1,8–2,4 GB
temporäre Spitze            3,1–3,8 GB
```

### 17.12.4 Übernahme

Ein übernommener Preview speichert:

- gewählte Konfiguration
- erkannte Dokumentmerkmale
- geschätzte Work Units
- Previewartefakte mit kurzer Lebensdauer
- Nutzungsentscheidung

Er erzeugt keinen separaten Schattenindex.

## 17.13 Corpus Profiler

### 17.13.1 Bericht

```text
Dokumentprofil · Baustoffkunde

Dokumente                    428
PDF-Seiten                48.231
digitaler Text                71 %
gescannte Seiten              29 %
Tabellen                       8 %
Seiten mit Abbildungen        34 %
Deutsch                       92 %
Englisch                       8 %
nahe Duplikatgruppen          14
geschätzte Bilddaten         9,4 GB
```

### 17.13.2 Ableitungen

Der Profiler kann vorschlagen:

- OCR nur für Seiten ohne brauchbare Textschicht
- bestimmte OCR-Sprache oder Engine
- hierarchisches statt rein tokenbasiertem Chunking
- Bildbeschreibungen nur für fachlich relevante Bilder
- Tabellen als strukturierte Daten oder Markdown
- multimodale Embeddings an- oder abschalten
- passenden Reranker
- Batchgrößen und Parallelität
- Speicherbereinigung vor dem Start

### 17.13.3 Grenzen

- Stichproben werden als Schätzungen gekennzeichnet.
- Dokumentinhalte werden nicht an externe Dienste geschickt.
- Empfehlungen verändern die aktive Konfiguration erst nach Zustimmung.
- Ein schneller Scan darf große Dateien nicht vollständig konvertieren.

## 17.14 Ressourcenwächter

### 17.14.1 Richtlinien

```yaml
resource_policy:
  max_cpu_percent: 70
  min_free_ram_gib: 4
  max_temperature_c: 85
  pause_on_battery: true
  resume_on_ac: true
  chat_priority: highest
  quiet_hours:
    start: "07:30"
    end: "15:30"
  downloads_during_chat: pause
  maintenance_priority: low
```

### 17.14.2 Prioritäten

```text
1. aktive Nutzerfrage
2. Quellen- und Medienbereitstellung
3. manueller Import
4. automatische Quellensynchronisation
5. Rebuild
6. Evaluation und A/B-Labor
7. Vacuum und Wartung
8. Modell- und Runtimeupdates
```

### 17.14.3 Kooperative Drosselung

Der Scheduler versucht zunächst:

- keine neuen Batches zu starten
- Batchgröße zu reduzieren
- Parallelität zu verringern
- einen sicheren Checkpoint abzuwarten
- Modellresidenz anzupassen

Erst danach wird ein Job vollständig pausiert.

### 17.14.4 Modellresidenz

OmaRag kann über Providerfunktionen:

- Chatmodell vorladen
- Embeddingmodell nach einem Batch entladen
- `keep_alive` sinnvoll setzen
- bei knapper Hardware immer nur ein großes Modell aktiv halten
- Hintergrundindexierung für eine Chatfrage kurz freigeben
- nach der Frage kontrolliert fortsetzen

### 17.14.5 Laptopregeln

Auf Akkubetrieb kann OmaRag standardmäßig:

- neue OCR-/Embeddingjobs pausieren
- aktive Batches sauber beenden
- Bildbeschreibungen verschieben
- Benachrichtigung statt Fehlermeldung zeigen

## 17.15 Ereignis-Replay, Snapshots und Idempotenz

### 17.15.1 Eventstore

SQLite-WAL-Tabellen:

```text
events
event_streams
stream_snapshots
idempotency_keys
client_cursors
```

### 17.15.2 Aufbewahrung

- aktive Jobs: vollständige Ereignisfolge
- kürzlich abgeschlossene Jobs: komprimierte Folge
- ältere Jobs: Abschlussbericht plus Snapshot
- Debugdetails: getrennte, konfigurierbare Frist
- Dokumenttexte: niemals im Eventstore

### 17.15.3 Idempotency-Key

Gespeichert werden:

- Key
- Requesthash
- Route
- Benutzer-/Clientkontext
- Ergebnis-ID
- Status
- Ablaufdatum

Damit erzeugt ein wiederholter Netzwerkrequest keinen doppelten Import oder Restore.

## 17.16 Snapshots, Rollback und Backups

### 17.16.1 Automatische Schutzpunkte

Vor folgenden Aktionen wird ein Snapshot empfohlen oder erzwungen:

- Haiku-Update mit Migration
- Datenbankmigration
- Änderung des Chunkings
- Wechsel des Embeddingmodells
- großer Synchronisationslauf
- Massenlöschung
- Rebuild
- Restore eines älteren Zustands

### 17.16.2 Datenbank-Tags

Tags dienen schnellen logischen Wiederherstellungspunkten:

```text
vor-haiku-update-0.71
vor-rebuild-embeddingwechsel
stabil-baustoffkunde-2026-07
```

Ein Tag ersetzt kein externes Backup.

### 17.16.3 Knowledge Pack

```text
OmaRag-Knowledge-Pack.tar.zst
├── manifest.json
├── checksums.sha256
├── workspace.toml
├── haiku.rag.yaml
├── sources.yaml
├── metadata-overlays/
├── evaluations/
├── annotations/
├── reports/
└── database/
```

Standardmäßig nicht enthalten:

- Modellgewichte
- API-Schlüssel
- Clienttokens
- lokale SSH-Konfiguration
- temporäre Caches

### 17.16.4 Restore

Restoreablauf:

```text
Backup prüfen
  ↓
Versions- und Kapazitätscheck
  ↓
aktuellen Zustand sichern
  ↓
Ziel in temporäres Verzeichnis entpacken
  ↓
Prüfsummen und Manifest validieren
  ↓
Datenbank read-only öffnen und Doctor ausführen
  ↓
atomar umschalten
  ↓
Workspace neu laden
```

## 17.17 Qualitätscockpit

### 17.17.1 Übersicht

```text
Qualität · Baustoffkunde

Datenbankintegrität       ✓
Vektordimension           ✓ 1024
Vektorindex               ⚠ 8.421 Chunks nicht indexiert
fehlende Bilder           ✓
verwaiste Chunks          ✓
nahe Duplikate            ⚠ 14 Gruppen
Provider erreichbar       ✓
Regressionstests          47 / 50 bestanden
mittlere Suchzeit         184 ms
mittlere Antwortzeit      18,2 s
```

### 17.17.2 Quellen der Qualitätsdaten

- Haiku Doctor
- Datenbankinfo und Indexabdeckung
- eigene Quellen- und Manifestchecks
- letzte Evaluationsläufe
- Citation- und Visual-Grounding-Prüfungen
- Jobfehler
- Providerstatus
- Speicher- und Backupzustand

### 17.17.3 Reparaturaktionen

Zulässige direkte Aktionen:

- Vektorindex aktualisieren
- Migration planen
- fehlende Embeddings neu erzeugen
- Dokument neu indexieren
- Duplikatgruppe öffnen
- Backup erstellen
- Regression erneut ausführen

Massenlöschungen bleiben manuell und explizit.

## 17.18 Workspacebezogene Regressionstests

### 17.18.1 Testfall

```yaml
id: concrete-class-c25-30
question: "Was bedeutet die Betonklasse C25/30?"
mode: strict

expect:
  documents:
    - "Betontechnologie.pdf"
  pages:
    any_of: [42, 43]
  required_terms:
    - "Zylinderdruckfestigkeit"
    - "Würfeldruckfestigkeit"
  citation_required: true
  max_first_relevant_rank: 3
```

### 17.18.2 Bewertungsdimensionen

- korrekte Dokumentquelle
- korrekte Seite
- Trefferposition
- MRR/MAP oder geeignete Retrievalmetrik
- geforderte Begriffe
- Zitatermittlung
- Antworttreue
- Widersprüche
- Latenz
- RAM-/VRAM-Spitze
- erzeugte Tokens
- Speicherbedarf

### 17.18.3 Auslöser

Regressionen können laufen:

- manuell
- vor Modellprofilübernahme
- nach Haiku-Update
- nach Chunkingänderung
- nach Rebuild
- nach Rerankerwechsel
- geplant in Ruhezeiten

Ein fehlgeschlagener Test blockiert nicht automatisch den Betrieb, kann aber die Beförderung eines neuen Profils verhindern.

## 17.19 Belegmodi und Antwortintegrität

### Streng

- nur ausreichend belegbare Aussagen
- keine brauchbare Quelle führt zu einer klaren Nichtbelegbarkeitsmeldung
- aktuelle Dokumente gemäß Richtlinie
- Zitate möglichst aussagenah
- Standard für Normen und Fachwissen

### Normal

- quellengebundene Antwort
- erklärende Übergänge erlaubt
- unbelegte Ergänzungen werden sichtbar markiert

### Erkunden

- weitergehende Interpretation möglich
- Ableitungen und Vermutungen klar kennzeichnen
- nicht als Standard für Prüfungs- oder Normenaussagen

### Belegbericht

```text
Belegstatus: STARK

✓ 5 von 6 Kernaussagen mit Quellen gestützt
✓ 3 unterschiedliche Dokumente
✓ Seitenangaben und visuelle Verankerung vorhanden
✓ keine widersprüchlichen aktuellen Quellen
⚠ eine ergänzende Aussage nur einfach belegt
```

Keine Prozentzahl wird als objektive Wahrheit ausgegeben.

## 17.20 Speicher- und Kapazitätsplaner

### 17.20.1 Vorhersage

```text
Originaldokumente                 12,4 GB
Dokumentstruktur                   4,8 GB
Seitenbilder                       9,1 GB
Chunks und Metadaten               2,3 GB
Embeddings                         6,7 GB
temporärer Spitzenbedarf          11,2 GB
────────────────────────────────────────
dauerhaft ungefähr                35,3 GB
während Verarbeitung bis          46,5 GB

Freier Speicher                   61,8 GB
Bewertung                         ausreichend
```

### 17.20.2 Einflussanalyse

```text
Seitenbilder behalten      +9,1 GB
Bildbeschreibungen         +0,8 GB
multimodale Embeddings     +3,4 GB
Datenbankhistorie          variabel
Previewcache               temporär
```

### 17.20.3 Aufräumassistent

- alte Modellversionen
- abgelaufene Thumbnails
- abgeschlossene Detailereignisse
- verwaiste temporäre Dateien
- alte Backups
- historische Datenbankversionen, soweit keine Tags sie schützen

OmaRag zeigt Abhängigkeiten und Wiederherstellungsauswirkungen vor dem Löschen.

## 17.21 Quellenmanager

### 17.21.1 Quellenarten

- lokale Datei
- lokaler Ordner
- beobachteter Ordner
- HTTP/HTTPS
- WebDAV
- S3 und kompatible Dienste
- später Plugins

### 17.21.2 Quellendefinition

```yaml
id: fachbuecher-local
type: filesystem
root: /home/stephan/Fachbuecher
mode: watch

include:
  - "**/*.pdf"
  - "**/*.docx"

exclude:
  - "**/~*"
  - "**/.trash/**"

processing_profile: deutsches-fachbuch

metadata:
  collection: Fachbücher
  subject: Bautechnik
```

### 17.21.3 Synchronisationsvorschau

Vor dem Lauf:

```text
Hinzufügen       4
Aktualisieren    2
Unverändert    138
Entfernen        1
Konflikte        1
```

Löschen aus der Quelle darf je nach Policy bedeuten:

- im Index behalten
- als veraltet markieren
- nach Bestätigung entfernen
- automatisch entfernen, nur bei ausdrücklich aktivierter Regel

## 17.22 Dokumentversionen und Gültigkeit

### 17.22.1 Metadatenoverlay

```yaml
document_type: norm
edition: "2023-08"
valid_from: "2023-08-01"
valid_until: null
status: current
supersedes:
  - "DIN XYZ:2018"
superseded_by: null
jurisdiction: DE
subject: concrete
```

### 17.22.2 Retrievalrichtlinien

```text
nur aktuelle Dokumente
aktuelle bevorzugen
alle Versionen
historischer Stand zum Datum
```

### 17.22.3 Konflikte

Bei mehreren Fassungen zeigt OmaRag:

- verwendete Ausgabe
- alternative ältere oder neuere Ausgabe
- Gültigkeitswarnung
- widersprüchliche Fundstellen

Die Originaldatei bleibt unverändert; OmaRag speichert Metadaten als Overlay.

## 17.23 Backendprofile

```text
● Server Alsfeld
  10 vCPU · 24 GiB RAM
  verbunden über SSH-Tunnel
  LOCAL AI · Schreibzugriff

○ Lokal auf diesem Gerät
  Ryzen 5 8640HS · 16 GiB RAM
  LOCAL AI · 37 Dokumente

○ Docker lokal
  CPU-Profil
```

### Sicherheitsregeln

- Remotezugriff bevorzugt über SSH-Tunnel, Tailscale oder anderes VPN
- TLS und Token bei direkter Netzwerkfreigabe
- kein ungeschützter Standardport
- Profile können read-only sein
- Backendname und Datenschutzstatus bleiben ständig sichtbar
- lokale und entfernte Dateipfade werden eindeutig gekennzeichnet

## 17.24 A/B-Labor

### 17.24.1 Experimentdefinition

```yaml
name: "kleines Embedding plus Reranker gegen großes Embedding"
workspace: ws-baustoffkunde
suite: core

variant_a:
  embedding_profile: small-german
  reranker: enabled

variant_b:
  embedding_profile: larger-multilingual
  reranker: disabled

runs: 3
warmup: true
statistics: median
```

### 17.24.2 Fairnessregeln

- identischer Dokumentbestand
- identische Testfragen
- gleiche Filter
- dokumentierte Seeds, soweit anwendbar
- Warm-up
- mehrere Läufe
- Median und Streuung
- getrennte Retrieval- und Antwortwertung
- keine automatische Standardübernahme

### 17.24.3 Ergebnis

```text
Variante A

✓ 2,2 × schneller
✓ bessere Zitatermittlung
✓ 4,1 GiB weniger Speicher
≈ gleiche Antwortqualität

Empfehlung: Variante A
```

## 17.25 Schlanke CLI

Beispiele:

```bash
omarag status
omarag backends
omarag workspace list
omarag workspace open baustoffkunde
omarag ingest ./Betontechnologie.pdf
omarag ingest preview ./Betontechnologie.pdf
omarag corpus profile
omarag jobs
omarag job pause job-42
omarag ask "Was bedeutet C25/30?" --evidence strict
omarag search "Expositionsklasse XC4" --json
omarag quality scan
omarag evaluation run core
omarag backup create
omarag diagnose
```

Grundregeln:

- standardmäßig menschenlesbare Ausgabe
- `--json` mit stabilem Schema
- eindeutige Exitcodes
- keine interaktiven Rückfragen bei `--non-interactive`
- gefährliche Befehle benötigen `--confirm` oder vorbereiteten Plan
- dieselbe API und dieselben Berechtigungen wie das TUI

## 17.26 Diagnosepaket

### 17.26.1 Vorschau

```text
Enthalten:
✓ System- und Hardwaredaten
✓ Paket- und Modellversionen
✓ maskierte Konfiguration
✓ Doctor-Bericht
✓ Jobzustände
✓ letzte technische Fehler
✓ Terminalfähigkeiten

Nicht enthalten:
✗ Dokumentinhalte
✗ Chatverläufe
✗ API-Schlüssel
✗ private SSH-Schlüssel
```

### 17.26.2 Paket

```text
omarag-diagnostics-20260725.tar.zst
├── system.json
├── hardware.json
├── versions.json
├── config.redacted.yaml
├── doctor.json
├── jobs.json
├── last-errors.log
├── docker-status.txt
├── ollama-models.json
└── terminal-capabilities.json
```

Nutzer können einzelne Bestandteile vor dem Export abwählen.

## 17.27 MCP-Fassade

Haikus vorhandene MCP-Funktion wird bevorzugt wiederverwendet. OmaRag ergänzt eine Policy- und Workspaceebene.

Mögliche Werkzeuge:

```text
omarag_list_workspaces
omarag_select_workspace
omarag_search
omarag_ask
omarag_get_citation
omarag_ingest
omarag_job_status
omarag_pause_job
omarag_run_evaluation
```

Regeln:

- read-only ist Standard
- Schreibwerkzeuge einzeln freigeben
- Workspace und Backend explizit
- Cloudpolicy gilt auch für MCP
- Auditereignisse für mutierende Aufrufe
- keine direkte Umgehung der OmaRag-Queue

## 17.28 Komfortfunktionen

- Antwort als Markdown mit Quellen exportieren
- reinen Text oder JSON exportieren
- Belegpaket mit Bildausschnitten erzeugen
- Frage als Regressionstest speichern
- Quellen für die nächste Frage anheften
- Suchverlauf mit reproduzierbaren Parametern
- Desktopbenachrichtigungen bei Jobabschluss
- Zwischenablage für Antwort, Zitat, Pfad und Seite
- Terminal-Selbsttest
- zuletzt genutzten Workspace wieder öffnen
- Jobpriorität schnell ändern
- Lesemodus für lange Antworten
- Quellen nebeneinander vergleichen

## 17.29 Plugin-Sicherheit

Ein öffentliches Plugin-SDK folgt erst nach stabiler API. Die Berechtigungsstruktur wird dennoch vorab festgelegt.

```toml
id = "de.omarag.webdav-extra"
version = "1.0.0"

[permissions]
network = ["https://cloud.example.de"]
filesystem_read = ["/home/user/Documents"]
filesystem_write = []
secrets = ["WEBDAV_TOKEN"]
process_spawn = false

[capabilities]
source_provider = true
metadata_provider = true
tui_panel = false
```

Installationsdialog:

```text
Dieses Plugin möchte:

✓ Dateien aus /home/user/Documents lesen
✓ cloud.example.de erreichen
✓ auf WEBDAV_TOKEN zugreifen
✗ keine Dateien verändern
✗ keine Befehle ausführen
```

Langfristig laufen Plugins bevorzugt in getrennten Prozessen oder einer eingeschränkten WASI-/RPC-Umgebung. Beliebiger Python-Code wird nicht automatisch in den `omaragd`-Hauptprozess geladen.

---

# 18. Implementierungsroadmap v0.3

Die Reihenfolge folgt dem technischen Risiko. Zuerst werden Verträge, Persistenz und Wiederaufnahme stabilisiert. Komfort, Bilder und Verpackung bauen erst danach darauf auf.

## Phase 0 – Versions-, Capability- und Vertragsspitze

Umfang:

- Haiku `0.70.x` in isolierter `uv`-Runtime
- Adapter `haiku-v070`
- Probeadapter `haiku-v069`
- Capability-Handshake
- OpenAPI-, Event- und Workspace-Schema
- öffentliche Haiku-Pipeline einmal vollständig ausführen
- Bildanhang für `ask` testen
- Ollama-Version, Tags, Pullstream und Modellstatus testen
- kleiner Rust-Testclient

Abnahme:

```text
Test-PDF importieren
  ↓
Rust-Client erhält Fortschritt
  ↓
Frage mit und ohne Bild stellen
  ↓
Tokens, Quellen und Medienreferenzen empfangen
  ↓
Contract-Snapshots reproduzierbar
```

## Phase 1 – Persistenter Daemon, Workspaces und Eventstore

Umfang:

- Workspace-Manifest
- Workspace erstellen, öffnen, klonen und read-only öffnen
- SQLite-WAL für Jobs, Events, Snapshots und Idempotenz
- Single-Writer-Scheduler
- adaptive Daemon-Lebensdauer
- SSE-Replay
- Job- und Run-Snapshots
- Pause, Resume, Cancel
- ETags und Idempotency-Key

Abnahme:

- TUI-Testclient kann während eines Jobs beendet werden.
- Der Job läuft weiter oder pausiert kontrolliert.
- Nach Reconnect ist der Zustand ohne Doppelereignisse vollständig.
- Derselbe Idempotency-Key erzeugt keinen zweiten Job.

## Phase 2 – Abhängigkeiten und sichere Updates

Umfang:

- Python-, `uv`-, Haiku- und Ollama-Erkennung
- Komponentenstatus
- Installationsplan
- isolierte Runtime-Slots
- Haiku-Updateprüfung
- Ollama-Updatehinweise
- Modellpulls
- Signatur- und Prüfsummenprüfung
- Rollback
- Snapshot-Hook vor Migration

Abnahme:

- sauberes System kann ohne Änderung von System-Python bootstrappen,
- bestehende Installationen werden erkannt,
- Updates werden nur nach Planbestätigung ausgeführt,
- fehlgeschlagenes Haiku-Update kann auf den vorherigen Slot zurückfallen.

## Phase 3 – Corpus-Profiler, Testindexierung und Kapazität

Umfang:

- schneller Corpus-Scan
- repräsentative Seitenauswahl
- Previewkonvertierung
- OCR-, Tabellen-, Bild- und Chunkbericht
- Pipelinevorschlag
- Speicher- und Laufzeitschätzung
- Aufräumvorschläge
- Previewübernahme

Abnahme:

- ein 500-seitiges Fachbuch kann mit höchstens zwölf Seiten getestet werden,
- der Nutzer sieht extrahierten Text, Tabellen, Bilder und Chunkgrenzen,
- die Vollverarbeitung wird erst nach ausdrücklicher Übernahme gestartet.

## Phase 4 – Instrumentierte Haiku-Pipeline und präziser Fortschritt

Umfang:

- Preflight
- Datei-, Seiten-, OCR-, Tabellen-, Bild-, Chunk- und Batchzählung
- Checkpoints
- Work-Unit-Modell
- historische Durchsatzsamples
- ETA-Spanne mit Konfidenz
- atomarer Import
- Indexstatus
- Fehlerphase und Retry

Abnahme:

- Gesamtfortschritt springt nicht rückwärts,
- aktuelle Phase und konkrete Einheiten sind sichtbar,
- ein Kill während Embeddings lässt den Job am letzten sicheren Punkt fortsetzen,
- ETA zeigt zunächst Kalibrierung und später eine Spanne.

## Phase 5 – Hardware, Modelle und Ressourcenwächter

Umfang:

- CPU, RAM, Swap, GPU, VRAM, Speicher, Temperatur und cgroups
- lokale und Remote-Backendmessung
- signierter Modellkatalog
- getrennte Empfehlungen für Chat, Embedding, Reranking und Vision
- Profile Sparsam, Ausgewogen und Qualität
- freie Modellauswahl
- Kurzbenchmark
- Resource Governor
- Modellresidenz und `keep_alive`

Abnahme:

- Yoga und CPU-Server werden korrekt unterschieden,
- Empfehlungen beziehen sich auf das aktive Backend,
- Chat kann Hintergrundindexierung kooperativ verdrängen,
- bei knapper Hardware wird gewarnt, nicht willkürlich gesperrt.

## Phase 6 – Rust-Anwendungsunterbau und TUI-MVP

Umfang:

- Domainmodelle
- HTTP/SSE-/Mockclient
- AppState, Actions, Reducer, Effects
- Reconnect und Replay
- Workspace- und Backendwechsel
- NAV-/TEXT-Modus
- Fokus-, Werkbank- und Zenlayout
- Einfach-/Werkstatt-Ebene
- Command Palette
- responsive Darstellung

Abnahme:

- gesamte App-Logik ist ohne Ratatui testbar,
- alle Hauptbereiche sind per Pfeiltasten erreichbar,
- Layout und Bedienebene wechseln ohne Neustart,
- aktive Jobs bleiben in jeder Ansicht sichtbar.

## Phase 7 – vollständige Konfiguration, Quellen und Metadaten

Umfang:

- schema-getriebener Haiku-Editor
- Roh-YAML und Diff
- Config-Impact
- Verarbeitungsprofile
- Quellenmanager
- Synchronisationsvorschau
- Include-/Exclude-Regeln
- Gültigkeits- und Versionsoverlays
- Dokumentfilter und angeheftete Quellen

Abnahme:

- jedes Haiku-Konfigurationsfeld ist erreichbar,
- Rebuild- und Migrationsfolgen werden vor dem Speichern gezeigt,
- eine Quelle kann getestet und ihre Änderungen vorab angesehen werden,
- aktuelle und veraltete Dokumentfassungen sind unterscheidbar.

## Phase 8 – Qualität, Belegmodi und Regression

Umfang:

- Doctor-Adapter
- Qualitätscockpit
- strukturierte Issues
- sichere Reparaturjobs
- Belegmodi Streng, Normal und Erkunden
- Aussage-Quellen-Zuordnung
- Regressionstestdefinition
- Retrieval-, Zitat- und Antwortmetriken
- geplante Läufe

Abnahme:

- ein fehlerhafter oder unvollständiger Index wird verständlich gemeldet,
- streng beantwortete Fragen geben bei fehlender Evidenz keine erfundene Fachantwort,
- ein Modell- oder Chunkingwechsel kann gegen eine Testsuite geprüft werden.

## Phase 9 – Snapshots, Backups und Restore

Umfang:

- Haiku-/LanceDB-Tags
- automatische Schutzpunkte
- Knowledge-Pack-Export
- Prüfsummen
- Backupprüfung
- atomarer Restore
- Rollbackbericht
- Backupaufbewahrung

Abnahme:

- vor einem riskanten Rebuild wird ein Schutzpunkt erstellt,
- ein externes Backup kann auf einem Testsystem verifiziert und wiederhergestellt werden,
- Geheimnisse und Modellgewichte sind standardmäßig nicht im Export.

## Phase 10 – Bilder, Tabellen und visuelle Verankerung

Umfang:

- Medienkandidaten aus Quellen
- Ranking, Deduplizierung und Diversität
- zwei bis drei relevante Medien als Ziel
- Thumbnailservice
- Kitty, Sixel, iTerm2 und Halfblock
- Vollbildviewer
- strukturierte Tabellen
- Chunkvisualisierung mit Bounding Boxes

Abnahme:

- irrelevante Auffüllbilder werden verhindert,
- jedes Medium nennt Dokument und Seite,
- Bilddarstellung blockiert den Renderloop nicht,
- Textfallback funktioniert über SSH und in unbekannten Terminals.

## Phase 11 – A/B-Labor, CLI, Remoteprofile und Diagnose

Umfang:

- reproduzierbare Experimente
- Profilvergleich
- native CLI
- JSON-Ausgabe
- Backendprofile
- SSH-Tunnel
- read-only Remotezugriff
- Diagnosepreview und -paket
- Desktopbenachrichtigungen

Abnahme:

- zwei Pipelineprofile können fair verglichen werden,
- zentrale Funktionen sind skriptbar,
- ein Notebook kann sicher auf das Serverbackend zugreifen,
- ein Diagnosepaket enthält keine Dokumenttexte oder Geheimnisse.

## Phase 12 – MCP- und Atlas-Anbindung

Umfang:

- OmaRag-MCP-Fassade
- read-only Standard
- Workspaceauswahl
- Policy- und Auditschicht
- Atlas-API-Client
- Quellen- und Medienübergabe
- Jobbeobachtung

Abnahme:

- Atlas kann suchen, fragen und Zitate öffnen,
- schreibende MCP-Werkzeuge sind standardmäßig deaktiviert,
- keine MCP-Aktion umgeht Queue, Cloudpolicy oder Workspacegrenzen.

## Phase 13 – AppImage und Docker

Umfang:

- dünnes AppImage
- `uv`-Bootstrap
- Desktopintegration
- `systemd --user`
- Signatur und zsync
- `omaragd`-Container
- Compose-Profile CPU, NVIDIA, AMD und External Ollama
- Volumes und Healthchecks
- Host-TUI-Dokumentation

Abnahme:

- AppImage startet auf einem sauberen unterstützten Linuxsystem,
- Dockerneubau verliert keine Daten,
- Host-TUI kann das Dockerbackend verwenden,
- Release enthält Prüfsummen, SBOM und Migrationshinweise.

## Phase 14 – Härtung und Veröffentlichung

Umfang:

- Kill- und Recoverymatrix
- Migrationsmatrix
- große Dokumentbestände
- lange Indexierungsjobs
- Backpressure
- Speicherlecks
- Security Review
- Barrierearmut
- Dokumentation
- Beispielworkspaces
- Beta-Migrationspfad

Abnahme:

- alle verbindlichen Kriterien aus Abschnitt 20 sind erfüllt,
- keine bekannte Datenverlustklasse bleibt ohne Schutz oder dokumentierte Warnung,
- Contract-, E2E- und Recoverytests laufen in CI.

---

# 19. Teststrategie

## 19.1 Rust-Unit- und Reducer-Tests

Testfälle:

- Workspacewechsel
- Backendwechsel
- NAV-/TEXT-Modus
- Layoutwechsel
- Runstream
- Eventdeduplizierung
- Replayreset
- Jobpause
- Konfigurationsdiff
- Qualitätsissue
- Backupdialog
- Fehler- und Reconnectzustände

Propertytests eignen sich für:

- monotone Fortschrittsanzeige
- Eventdeduplizierung
- Navigation ohne Fokusverlust
- Idempotenzdarstellung
- Layoutberechnung bei beliebigen Terminalgrößen

## 19.2 Rendering-Golden-Tests

Größen:

```text
80 × 24
90 × 28
100 × 30
120 × 36
145 × 40
200 × 60
```

Varianten:

- Fokus, Werkbank und Zen
- Einfach und Werkstatt
- dunkel, hell und monochrom
- deutscher Langtext
- laufender Job
- schwacher Belegstatus
- drei Bilder und Textfallback
- Remote- und Cloudwarnung

## 19.3 Python- und Daemon-Tests

- Workspace-Lebenszyklus
- Config-Roundtrip
- Haiku-Adapter `0.70`
- best-effort `0.69`
- Datenbank öffnen, read-only und Migration
- Previewpipeline
- Corpusprofil
- atomarer Import
- Eventstore
- Idempotency-Key
- Queue und Scheduler
- Pause, Resume und Cancel
- Backup und Restore
- Doctor und Regression

## 19.4 Contract-Tests

- OpenAPI-Snapshot
- JSON-Schema für Events
- unbekannte Felder
- unbekannte Eventtypen
- Client-Min-/Max-Version
- Fehlerobjekte
- ETag-Konflikt
- Idempotency-Konflikt
- Replayreset
- Capability-Degradation

## 19.5 Kill- und Recoverymatrix

Pro Phase:

```text
preflight
download
conversion
ocr
table extraction
picture extraction
chunking
embedding
database import
index creation
vacuum
backup
restore
evaluation
```

Jeweils testen:

- TUI beendet
- Daemon SIGTERM
- Daemon SIGKILL
- Ollama beendet
- Rechnerneustart simuliert
- Dateisystem voll
- Quelle verschwindet
- Netzwerk fällt aus
- Eventclient trennt sich

## 19.6 Qualitäts- und Regressionstests

- erwartetes Dokument
- erwartete Seite
- MRR/MAP
- Zitatermittlung
- Widerspruchserkennung
- streng ohne Quelle
- veraltete gegen aktuelle Dokumentversion
- Bild- und Tabellenquelle
- Antwort mit angehefteten Dokumenten
- A/B-Median und Warm-up

## 19.7 Ressourcenwächter

- CPU-Limit
- RAM-Untergrenze
- cgroup-Limit
- Temperaturwarnung
- Akkubetrieb
- Ruhezeiten
- Chat verdrängt Indexierung
- automatische Fortsetzung
- Modell entladen
- Provider überlastet

## 19.8 Hardware-Akzeptanzprofile

Mindestens:

1. Lenovo Yoga 7 14AHP9, Ryzen 5 8640HS, 16 GiB RAM, Radeon 760M, 2 GiB nutzbarer VRAM
2. Server, 10 vCPU, 24 GiB RAM, keine GPU
3. Docker mit 8 GiB cgroup-Limit
4. System ohne Ollama
5. Remote-Ollama
6. NVIDIA-Testsystem, soweit CI oder Hardware verfügbar
7. AMD-Containerprofil, soweit Runtimeunterstützung vorhanden

## 19.9 AppImage-E2E

- sauberer Benutzer ohne Haiku
- vorhandenes Ollama
- fehlendes Ollama
- portable Nutzung
- Desktopintegration
- User-Service
- Updateprüfung
- Signaturfehler
- Pfade mit Leerzeichen und Umlauten
- read-only AppImage-Verzeichnis

## 19.10 Docker-E2E

- CPU-Profil
- External Ollama
- persistente Volumes
- Healthcheck
- Upgrade und Rollback
- Containerlimit
- Host-TUI
- Backup in gemountetes Ziel
- Restart während Indexierung
- Port nur auf Loopback

## 19.11 Sicherheitstests

- Token fehlt oder falsch
- Nicht-Loopback ohne Auth
- Path Traversal
- URL-Schema-Missbrauch
- Secret-Leak in Logs
- Secret-Leak in Events
- bösartige Dateinamen
- manipulierte Katalogsignatur
- manipuliertes Backup
- Plugin mit zu vielen Rechten
- MCP-Schreibtool ohne Freigabe
- Cloudprovider ohne Zustimmung

---

# 20. Verbindliche Abnahmekriterien für OmaRag 0.3

OmaRag `0.3` gilt als fertig, wenn alle folgenden Punkte erfüllt sind.

## 20.1 Architektur und Haiku

- Haiku RAG wird nicht geforkt oder gepatcht.
- Das Rust-TUI kennt keine internen Haiku-Pythonobjekte.
- LanceDB wird ausschließlich über Haiku angesprochen.
- Adapter und Capability-Handshake funktionieren mit der freigegebenen Haiku-Version.
- TUI, CLI und Mockclient verwenden denselben API-Vertrag.
- Suche, Chat, Analyse, Quellen und Medien besitzen stabile OmaRag-Typen.

## 20.2 Workspaces

- mehrere Workspaces lassen sich erstellen und getrennt betreiben,
- jeder Workspace besitzt Manifest, Konfiguration und eigene Datenbank,
- Embeddingidentität und Vektordimension werden vor dem Öffnen geprüft,
- Export, Import, Klonen und read-only Öffnen funktionieren,
- UI-Präferenzen verunreinigen das portable Manifest nicht.

## 20.3 Abhängigkeiten und Updates

- vorhandenes Haiku, Python, `uv` und Ollama werden erkannt,
- fehlende Komponenten sind nach sichtbarer Zustimmung installierbar,
- Updateprüfung blockiert den TUI-Start nicht,
- Haiku kann in isolierten Slots aktualisiert und zurückgerollt werden,
- Modellupdates sind von Runtimeupdates getrennt,
- riskante Updates lösen einen Schutzpunkt aus.

## 20.4 Corpus, Preview und Kapazität

- ein Corpus kann ohne Vollindexierung profiliert werden,
- eine Testindexierung zeigt Text, Tabellen, Bilder und Chunks,
- ein Pipelinevorschlag wird begründet,
- Speicher- und Zeitbedarf werden als Schätzung gekennzeichnet,
- der Nutzer kann sämtliche Empfehlungen ändern.

## 20.5 Hardware, Modelle und Ressourcen

- CPU, effektiver RAM, GPU/VRAM, Speicher und Containerlimits werden erfasst,
- Empfehlungen werden je Modellrolle getrennt,
- jede Empfehlung nennt Gründe und erwartete Engpässe,
- freie Auswahl bleibt möglich,
- Resource Governor kann Hintergrundarbeit pausieren und fortsetzen,
- aktive Chatfragen erhalten konfigurierbaren Vorrang.

## 20.6 TUI

- Fokus, Werkbank und Zen wechseln ohne Neustart,
- Einfach und Werkstatt sind unabhängig vom Layout,
- alle Hauptfunktionen sind vollständig mit Pfeiltasten erreichbar,
- Merktasten kollidieren nicht mit Texteingabe,
- Workspace, Backend, Local-/Cloudstatus und Belegmodus sind sichtbar,
- Farbe ist nicht der einzige Zustandsträger.

## 20.7 Hintergrundbetrieb und Ereignisse

- `omaragd` ist der einzige Pflicht-Hintergrundprozess,
- laufende Jobs überleben das Schließen des TUI,
- Pause, Resume und Abbruch funktionieren,
- Jobs können nach Prozessneustart fortgesetzt werden,
- SSE-Replay und Snapshots rekonstruieren den Zustand,
- Idempotency-Key verhindert doppelte mutierende Jobs,
- nur ein schreibender LanceDB-Vorgang läuft gleichzeitig.

## 20.8 Fortschritt

- Gesamtfortschritt, aktuelle Phase, Dateien, Seiten, Chunks und Batches sind sichtbar,
- ETA erscheint zuerst kalibrierend und später als Spanne,
- Prozentwerte springen nicht rückwärts,
- Ressourcenpausen sind von Fehlern unterscheidbar,
- Checkpoints sind im Detailbericht sichtbar.

## 20.9 Qualität und Belege

- Doctor-Ergebnisse werden strukturiert dargestellt,
- Regressionstests speichern erwartete Quellen und Seiten,
- streng beantwortete Fragen verweigern unbelegte Fachbehauptungen,
- Belegstatus ist erklärbar und keine künstliche Prozentzahl,
- aktuelle und veraltete Dokumente können getrennt behandelt werden,
- A/B-Läufe sind reproduzierbar.

## 20.10 Snapshots und Backups

- Schutzpunkte können erstellt, gelistet und restauriert werden,
- externe Knowledge Packs besitzen Manifest und Prüfsummen,
- Restore prüft Daten vor dem atomaren Umschalten,
- Geheimnisse und Modelle sind standardmäßig ausgeschlossen,
- ein Restore erzeugt einen Sicherheitszustand des vorherigen Workspace.

## 20.11 Bilder und Tabellen

- bis zu drei relevante Quellenmedien werden ausgewählt,
- Mindestqualität verhindert Auffüllbilder,
- jedes Medium nennt Dokument und Seite,
- Terminalprotokolle und Textfallback funktionieren,
- Tabellen werden möglichst strukturiert dargestellt,
- Medienarbeit blockiert das TUI nicht.

## 20.12 Quellen und Remotezugriff

- Quellen können getestet, geplant und synchronisiert werden,
- Änderungen sind vorab sichtbar,
- Remote-Backends können read-only oder schreibbar konfiguriert werden,
- Hardwareempfehlungen beziehen sich auf das Backend,
- direkte Netzwerkfreigabe ist nicht ungeschützt voreingestellt.

## 20.13 CLI, Diagnose und MCP

- zentrale Funktionen besitzen CLI-Befehle und JSON-Ausgabe,
- Diagnosepakete zeigen vorab ihren Inhalt,
- Dokumenttexte und Geheimnisse fehlen im Standarddiagnosepaket,
- MCP ist read-only voreingestellt,
- MCP- und Atlasaktionen umgehen keine Richtlinie.

## 20.14 Distribution und Sicherheit

- signiertes AppImage und Docker-Compose-Stack werden erzeugt,
- Modelle sind nicht in Standardartefakte eingebaut,
- Daten überleben Containerneubauten,
- Cloudprovider benötigen technische Zustimmung,
- Geheimnisse erscheinen nicht in Logs oder Events,
- Release enthält Prüfsummen, SBOM und Migrationshinweise.

---

# 21. Empfohlene technische Festlegungen

```yaml
OmaRag:
  version: 0.3
  architecture: API-first
  central_entity: workspace

  tui:
    language: Rust
    framework: Ratatui
    runtime: Tokio
    pattern: State + Action + Reducer + Effect
    layouts: [focus, workbench, zen]
    interaction_levels: [simple, workshop]
    navigation: arrows-plus-mnemonics
    input_modes: [nav, text]

  themes:
    semantic_tokens: true
    follow_omarchy: true
    builtins: [tokyo-night, matte-black, catppuccin-latte]
    live_reload: true

  backend:
    executable: omaragd
    language: Python
    persistent_when_jobs_active: true
    extra_worker_daemon: false
    api: HTTP-JSON-plus-resumable-SSE
    schema: OpenAPI-3.1
    queue: SQLite-WAL
    event_store: SQLite-WAL
    event_replay: true
    idempotency: true

  workspaces:
    portable_manifest: true
    own_database: true
    own_config: true
    own_sources: true
    own_evaluations: true
    secret_references_only: true

  rag:
    implementation: Haiku-RAG
    development_target: 0.70.x
    modification_of_haiku: false
    pipeline: public-instrumented-primitives
    ingester_adapter: optional
    strict_evidence_mode: true

  inference:
    primary_local_runtime: Ollama
    additional_providers: optional
    version_detection: API
    model_catalog: signed-and-updateable
    user_override: allowed-when-technically-valid
    residency_manager: true

  dependencies:
    python_manager: uv
    isolated_runtime: true
    update_default: notify-stable
    atomic_runtime_switch: true
    rollback: true

  progress:
    work_units: true
    checkpoints: true
    eta: range-plus-confidence
    historical_throughput: true

  quality:
    doctor: true
    regression_suites: true
    ab_lab: true
    citation_checks: true
    fake_confidence_score: false

  recovery:
    database_tags: true
    external_backups: true
    checksums: true
    atomic_restore: true

  resources:
    governor: true
    battery_awareness: true
    thermal_awareness: true
    chat_priority: configurable

  media:
    target_images: 3
    minimum_useful_images: 0
    relevance_threshold: configurable
    renderer: ratatui-image
    fallbacks: [halfblocks, text-card]

  database:
    implementation: LanceDB-through-Haiku
    direct_rust_access: false
    writers: 1

  packaging:
    appimage: true
    docker_compose: true
    models_bundled: false
    sbom: true
    signatures: true

  privacy:
    default: local
    telemetry: false
    cloud_requires_consent: true
    remote_requires_auth: true
```

---

# 22. Empfohlener Technologie-Stack

## 22.1 Rust

| Aufgabe | Bibliothek oder Technik |
|---|---|
| TUI | `ratatui` |
| Terminal | `crossterm` |
| Async Runtime | `tokio` |
| HTTP | `reqwest` |
| SSE | `reqwest-eventsource` oder eigener robuster Adapter |
| Serialisierung | `serde`, `serde_json` |
| CLI | `clap` |
| Fehler | `thiserror`, `anyhow` nur an Binärgrenzen |
| Logging | `tracing`, `tracing-subscriber` |
| Hardware | `/proc`, `/sys`, PCI, `sysinfo` ergänzend |
| Theme-TOML | `toml` |
| Dateiwächter | `notify` |
| Bilder | `ratatui-image`, `image` |
| IDs | `uuid` |
| Zeit | `time` |
| Secret Store | Secret-Service/libsecret-Adapter, Fallbackdatei nur verschlüsselt |
| SSH-Tunnel | kontrollierter `ssh`-Subprozess oder Rust-SSH-Bibliothek nach Spike |
| Tests | `insta`, `wiremock`, `proptest` |

## 22.2 Python

| Aufgabe | Bibliothek oder Technik |
|---|---|
| API | `FastAPI` |
| Server | `uvicorn` |
| Schema | `pydantic` |
| Haiku | `haiku.rag` |
| HTTP intern | `httpx` |
| Queue/Eventstore | stdlib `sqlite3` oder schlanke Async-Schicht, WAL |
| Versionen | `packaging.version`, `importlib.metadata` |
| Systemmetriken | `psutil` ergänzend, plattformspezifisch abstrahiert |
| Backups | `tarfile`/zstd-Bibliothek, Streaming |
| Tests | `pytest`, `pytest-asyncio` |
| Runtime | `uv` mit Lockfile |

## 22.3 Externe Schnittstellen

| System | Verwendung |
|---|---|
| Haiku Python API | RAG, Pipeline, Quellen, Datenbank |
| Haiku Doctor/Tags/Evaluation | Qualität, Restore, Benchmarks |
| Haiku Ingester | kontinuierliche Quellen |
| Ollama API | Version, Modelle, Pull, Inferenz, Residency |
| Omarchy `colors.toml` | Themeimport |
| AppImage/linuxdeploy | AppImage-Build und Updateinformation |
| Docker Compose | reproduzierbarer Dienstbetrieb |
| MCP | optionale Agenten- und Atlasintegration |

## 22.4 Release-Automatisierung

- CI-Matrix für Rust und Python
- reproduzierbarer Python-Lock
- Container-Multistagebuild
- AppImage in kompatibler Linux-Buildumgebung
- Signaturen und SHA-256
- SBOM
- OpenAPI-/Event-Contractprüfung
- Migrations- und Restoretest
- sauberer Installations-E2E
- Release Notes mit Haiku-Kompatibilitätsbereich

---

# 23. Priorisierte Epics und erste Tickets

## Epic A – Verträge und Adapter

1. OpenAPI-Grundvertrag
2. Event-Schema
3. Workspace-Schema
4. Haiku-v070-Adapter
5. Capability Map
6. Contract-Snapshots
7. Bildanhang für Fragen

## Epic B – Persistenter Kern

1. SQLite-Schema
2. Jobzustandsautomat
3. Eventstore
4. Idempotency Store
5. Single-Writer-Scheduler
6. Checkpoints
7. SSE-Replay
8. Snapshots
9. adaptive Lebensdauer

## Epic C – Workspaces

1. Manifestparser
2. Create/Open/Close
3. Locks
4. Clone
5. Read-only
6. Export/Import
7. Backendübertragung
8. Integritätsprüfung

## Epic D – Abhängigkeiten und Updates

1. Runtimeerkennung
2. Statusmodell
3. `uv`-Bootstrap
4. Installationsplan
5. Runtime-Slots
6. Updatecache
7. Rollback
8. Modellpull

## Epic E – Corpus und Preview

1. Dateiinventar
2. Stichprobenauswahl
3. Textschichterkennung
4. Previewkonvertierung
5. Tabellen-/Bildbericht
6. Chunkviewer
7. Pipelinevorschlag
8. Kapazitätsschätzung

## Epic F – Instrumentierte Pipeline

1. Preflight
2. Conversionadapter
3. OCR-Fortschritt
4. Tabellen und Bilder
5. Chunkadapter
6. Embedding-Batches
7. atomarer Import
8. Indexstatus
9. Retry/Rollback

## Epic G – Hardware, Modelle und Ressourcen

1. Hardwareinventar
2. cgroups
3. Remoteinventar
4. Katalogschema
5. Signaturprüfung
6. Fit-Gate
7. Empfehlungen
8. Benchmark
9. Resource Governor
10. Residency Manager

## Epic H – Rust-App und TUI

1. Domainmodelle
2. Client und Mockclient
3. AppState/Reducer/Effects
4. Replay
5. drei Layouts
6. zwei Bedienebenen
7. Workspace-Switcher
8. Backend-Switcher
9. Command Palette
10. responsive Regeln

## Epic I – Konfiguration und Quellen

1. Schemaformulare
2. Config-Impact
3. Raw YAML
4. Quellenmanager
5. Syncpreview
6. Watch/Poll
7. Metadatenoverlays
8. Gültigkeitsfilter

## Epic J – Qualität und Evaluation

1. Doctoradapter
2. Quality Report
3. Issues
4. Evidence Report
5. Strict Mode
6. Testfallschema
7. Regression Runner
8. Metriken
9. A/B-Labor
10. Profilbeförderung

## Epic K – Recovery

1. Tags
2. Schutzpunkthooks
3. Knowledge Pack
4. Prüfsummen
5. Verify
6. Restore
7. Retention
8. Recovery-E2E

## Epic L – Medien

1. Kandidaten
2. Ranking
3. Deduplizierung
4. Thumbnails
5. Terminalprotokolle
6. Bildkarten
7. Viewer
8. Tabellen
9. Visual Grounding

## Epic M – CLI, Remote, Diagnose und MCP

1. CLI-Grundgerüst
2. JSON-Schema
3. Backendprofile
4. SSH-Tunnel
5. Diagnosepreview
6. Diagnoseexport
7. MCP read-only
8. Atlas-Client
9. Auditereignisse

## Epic N – Distribution

1. AppDir
2. Bootstrap
3. Desktopintegration
4. User-Service
5. AppImage-Update
6. Daemon-Dockerfile
7. Compose-Profile
8. Volumes
9. Healthchecks
10. SBOM und Signatur

---

# 24. Kritischer Pfad und Releases

## 24.1 Technischer MVP

Enthält:

- Haiku-v070-Adapter
- einen Workspace
- persistente Queue
- SSE-Replay
- einfacher Import
- Suche und Chat
- Quellen
- Fokuslayout
- einfache Abhängigkeitsprüfung

Noch nicht enthalten:

- Corpus-Profiler
- Backups
- Regression
- Bilder
- Dockerprofile jenseits CPU

## 24.2 Alpha

Zusätzlich:

- mehrere Workspaces
- Testindexierung
- präziser Fortschritt
- Hardware- und Modellberater
- drei Layouts
- Omarchy-Themes
- AppImage

## 24.3 Beta

Zusätzlich:

- Ressourcenwächter
- Quellenmanager
- Quality Cockpit
- Belegmodi
- Regression
- Snapshots und externe Backups
- Bilder und Tabellen
- Docker

## 24.4 v1.0-Kandidat

Zusätzlich:

- A/B-Labor
- Remoteprofile
- CLI
- Diagnose
- Atlas/MCP
- dokumentierter Migrationspfad
- vollständige Recovery- und Securitytests

---

# 25. Nicht-Ziele

OmaRag soll im Kern nicht:

- Haiku RAG ersetzen
- LanceDB direkt aus Rust verwalten
- eine eigene Modellruntime schreiben
- ein eigenes GraphRAG als Pflichtschicht einführen
- ein allgemeines Agentenframework werden
- standardmäßig Websuche aktivieren
- Modelle ohne Zustimmung herunterladen
- Systempakete still überschreiben
- immer drei Bilder erzwingen
- eine ETA als exakte Zusage darstellen
- einen einzelnen künstlichen KI-Konfidenzwert ausgeben
- eine öffentliche, ungeschützte Internet-API bereitstellen
- frühzeitig eine komplexe Mehrbenutzer- und Mandantenplattform bauen
- einen Plugin-Marktplatz vor stabiler API eröffnen
- beliebige Plugins im Hauptprozess ausführen
- Modellgewichte in das Standard-AppImage einbauen
- Omarchy-Wallpaper, Fonts oder ausführbare Themebestandteile kopieren
- Haikus MCP, Doctor, Tags oder Evaluation unnötig neu implementieren

---

# 26. Architekturentscheidungen als ADR-Kurzfassung

## ADR-001: API-first

Das TUI, die CLI und Atlas kommunizieren ausschließlich über die OmaRag API.

## ADR-002: Python-Sidecar statt PyO3

Haiku läuft in einem separaten Pythonprozess.

## ADR-003: Kein direkter LanceDB-Zugriff aus Rust

Alle Datenbankoperationen laufen über Haiku.

## ADR-004: Workspace als zentrale Einheit

Konfiguration, Datenbank, Quellen, Tests und Backups werden gemeinsam versioniert und verwaltet.

## ADR-005: Schema-getriebene Konfiguration

Formulare werden aus Haikus Pydantic-Schema erzeugt.

## ADR-006: Wiederaufnehmbare SSE

HTTP-Befehle werden mit persistenten, replayfähigen SSE-Streams kombiniert.

## ADR-007: Idempotente mutierende Befehle

Start- und Restoreoperationen benötigen Idempotency-Keys.

## ADR-008: Local-first mit technischer Cloudsperre

Cloudprovider benötigen gespeicherte Zustimmung.

## ADR-009: Ein Writer

Mutierende LanceDB-Operationen werden serialisiert.

## ADR-010: `omaragd` ist der einzige Pflichtdaemon

API, Queue, Scheduler, Events und Hintergrundarbeit laufen in einem Prozess.

## ADR-011: Isolierte Haiku-Runtime über `uv`

System-Python wird nicht ungefragt verändert.

## ADR-012: Dynamischer Modellkatalog

Empfehlungen kommen aus einem signierten, aktualisierbaren Katalog.

## ADR-013: Empfehlung ist beratend

Nutzer dürfen nicht empfohlene Modelle wählen, sofern sie technisch nutzbar sind.

## ADR-014: Corpusprofil ergänzt Hardwareprofil

Pipelineempfehlungen basieren auf Hardware und Dokumentbestand.

## ADR-015: Testindexierung vor Großjob

Repräsentative Seiten können ohne Produktivimport geprüft werden.

## ADR-016: Resource Governor statt maximaler Auslastung

Interaktive Nutzung hat kontrollierbaren Vorrang vor Hintergrundjobs.

## ADR-017: Work Units und ETA-Spanne

Fortschritt basiert auf messbaren Einheiten und historischen Durchsätzen.

## ADR-018: Relevanz vor Bildanzahl

Zwei bis drei Bilder sind Ziel, nicht Zwang.

## ADR-019: Belegbericht statt Vertrauenswert

OmaRag zeigt überprüfbare Belegeigenschaften statt einer erfundenen Sicherheitsskala.

## ADR-020: Tags plus externe Backups

Schnelle logische Restorepunkte und physische Sicherungen erfüllen unterschiedliche Zwecke.

## ADR-021: Doctor und Evaluation wiederverwenden

Vorhandene Haiku-Funktionen werden strukturiert eingebunden, nicht dupliziert.

## ADR-022: Dünnes AppImage

Runtimekomponenten können gebootstrapt werden; Modelle bleiben getrennt.

## ADR-023: Host-TUI bei Docker bevorzugt

Das Backend läuft containerisiert, das TUI behält direkten Terminalzugriff.

## ADR-024: Remote über sichere Profile

SSH-Tunnel oder VPN werden gegenüber offenem Port bevorzugt.

## ADR-025: Pluginrechte vor Pluginmarkt

Berechtigungen und Isolation werden spezifiziert, bevor Drittplugins öffentlich unterstützt werden.

---

# 27. Daten- und Persistenzmodell

## 27.1 SQLite-Betriebsdatenbank

Kernentitäten:

```text
workspaces
jobs
job_tasks
job_checkpoints
events
stream_snapshots
idempotency_keys
throughput_samples
resource_samples
dependency_states
model_catalog_cache
backend_profiles_public
quality_runs
evaluation_runs
backup_records
notifications
```

Geheimnisse werden nicht in dieser Datenbank gespeichert, sofern ein Secret Store verfügbar ist.

## 27.2 Aufbewahrung

| Daten | Standard |
|---|---|
| aktive Jobevents | vollständig |
| abgeschlossene Jobevents | 30 Tage, danach kompakt |
| Durchsatzsamples | aggregiert, 90 Tage |
| Ressourcenmetriken | kurzzeitig und aggregiert |
| Chatverläufe | workspacebezogen, nutzersteuerbar |
| Diagnosepakete | bis manuell gelöscht oder kurze Frist |
| Previewcache | automatisch, wenige Tage |
| Backups | nach expliziter Retentionpolicy |

## 27.3 Locks

- ein Daemonlock pro Betriebsdatenbank
- ein Writerlock pro Workspace
- read-only Clients sind parallel möglich
- verwaiste Locks werden nur nach Prozess- und Zeitprüfung gelöst
- Remote-/Netzwerkdateisysteme erhalten gesonderte Warnungen

---

# 28. UX-Grundsätze

1. **Zeigen, nicht verstecken:** aktive Modelle, Backend, Workspace und Datenschutzmodus bleiben sichtbar.
2. **Keine falsche Präzision:** ETA und Qualität werden als Spanne, Status und Begründung dargestellt.
3. **Gefährliche Aktionen haben Vorschau:** Updates, Rebuilds, Restore und Massenlöschungen.
4. **Einfach ohne Sackgasse:** die einfache Ebene kann jederzeit zur Werkstatt erweitert werden.
5. **Tastatur zuerst:** alle Kernfunktionen ohne Maus.
6. **Farbe ergänzt, ersetzt aber nicht Text und Symbole.**
7. **Keine dekorativen Quellen:** Bilder und Zitate müssen fachlich relevant sein.
8. **Fehler nennen die nächste sinnvolle Handlung.**
9. **Hintergrundarbeit bleibt kontrollierbar.**
10. **Reproduzierbarkeit:** Runs, Tests und Experimente speichern ihre relevanten Parameter.

---

# 29. Kerngedanke

**OmaRag soll nicht einfach das schönste TUI für Haiku RAG sein. Es soll die verlässlichste lokale RAG-Werkstatt für große, fachliche Dokumentbestände werden.**

Die Schlankheit entsteht durch klare Zuständigkeiten:

- Rust und Ratatui für Bedienung
- ein einziger kleiner `omaragd` für API und Betrieb
- Haiku RAG unverändert für die eigentliche RAG-Pipeline
- Ollama oder kompatible Provider für Modelle
- SQLite für Queue und Ereignisse
- LanceDB nur über Haiku
- Workspaces für reproduzierbare Wissensbestände
- Tests, Belege und Backups statt blindem Vertrauen
- AppImage für komfortable lokale Nutzung
- Docker für reproduzierbaren Dienstbetrieb

Der zentrale Ablauf:

```text
OmaRag starten
  ↓
Backend und Workspace auswählen
  ↓
Abhängigkeiten und Updates prüfen
  ↓
Hardware und Corpus analysieren
  ↓
Pipeline und Modelle vorschlagen
  ↓
Testindexierung prüfen
  ↓
Vollindexierung mit Fortschritt und Checkpoints
  ↓
TUI darf schließen
  ↓
omaragd arbeitet ressourcenschonend weiter
  ↓
Fragen im passenden Belegmodus
  ↓
Antwort, Zitate, Seiten und relevante Bilder
  ↓
Regression und Quality Cockpit sichern die Qualität
  ↓
Snapshots und Backups sichern den Bestand
```

Das Alleinstellungsmerkmal lautet:

> **OmaRag berät vor der Indexierung, arbeitet währenddessen transparent und weist nach Änderungen nach, dass Retrieval, Zitate und Antworten weiterhin stimmen.**

---

# 30. Technische Referenzen

## Haiku RAG

- Dokumentation: <https://ggozad.github.io/haiku.rag/>
- Changelog: <https://ggozad.github.io/haiku.rag/changelog/>
- Installation: <https://ggozad.github.io/haiku.rag/installation/>
- Python API: <https://ggozad.github.io/haiku.rag/python/>
- Custom Pipelines: <https://ggozad.github.io/haiku.rag/custom-pipelines/>
- Ingester: <https://ggozad.github.io/haiku.rag/ingester/>
- CLI, Doctor, Tags und Visualisierung: <https://ggozad.github.io/haiku.rag/cli/>
- Benchmarks und Evaluation: <https://ggozad.github.io/haiku.rag/benchmarks/>
- MCP: <https://ggozad.github.io/haiku.rag/mcp/>

## Ratatui und Bilder

- Ratatui: <https://ratatui.rs/>
- Ratatui Image: <https://github.com/benjajaja/ratatui-image>

## Ollama

- Dokumentation: <https://docs.ollama.com/>
- API: <https://docs.ollama.com/api>
- FAQ und `keep_alive`: <https://docs.ollama.com/faq>
- Docker: <https://docs.ollama.com/docker>
- Modellbibliothek: <https://ollama.com/library>

## Runtime und Distribution

- uv: <https://docs.astral.sh/uv/>
- AppImage native binaries: <https://docs.appimage.org/packaging-guide/from-source/native-binaries.html>
- AppImage Updates: <https://docs.appimage.org/packaging-guide/optional/updates.html>
- Docker Compose: <https://docs.docker.com/compose/>
- Compose Startreihenfolge: <https://docs.docker.com/compose/how-tos/startup-order/>

## Omarchy

- Repository: <https://github.com/basecamp/omarchy>
- Theming: <https://github.com/basecamp/omarchy/blob/quattro/docs/theming.md>

## MCP

- Spezifikation: <https://modelcontextprotocol.io/specification/>

---

## Gespeicherter Projektmerksatz

> **OmaRag** ist eine lokale, hardwarebewusste und überprüfbare RAG-Werkstatt auf Basis von **Rust, Ratatui und Haiku RAG**. Haiku bleibt unverändert und wird über den einzigen kleinen Hintergrundprozess `omaragd` per HTTP/JSON und wiederaufnehmbarer SSE-API angebunden. Zentrale Einheit ist ein Workspace mit eigener Datenbank, Konfiguration, Quellen, Qualitätsprüfungen und Backups. OmaRag erkennt Abhängigkeiten und Hardware, profiliert zusätzlich den Dokumentbestand, schlägt passende Modelle und Verarbeitungsprofile vor und erlaubt dennoch freie Entscheidungen. Vor großen Importen kann eine repräsentative Testindexierung erfolgen. Jobs laufen mit Checkpoints, präzisem Fortschritt, vorsichtiger ETA und Ressourcenregeln im Hintergrund weiter. Das TUI bietet Fokus, Werkbank und Zen sowie die Bedienebenen Einfach und Werkstatt, vollständige Pfeiltastennavigation, Merktasten und Omarchy-Themes. Antworten besitzen nachvollziehbare Belegmodi, Seitenzitate und bis zu drei relevante Quellenbilder. Doctor, Regressionstests, A/B-Labor, Snapshots und externe Knowledge Packs sichern Qualität und Wiederherstellbarkeit. Distribution: AppImage, Docker und CLI; Atlas und MCP verwenden dieselbe stabile API. Local-first, keine Telemetrie und Cloudnutzung nur nach ausdrücklicher Freigabe.
