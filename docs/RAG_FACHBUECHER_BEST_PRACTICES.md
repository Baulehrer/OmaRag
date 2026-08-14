# RAG für Fach- und Lehrbücher

## Leitprinzip

Die Antwortqualität entsteht entlang der gesamten Kette:

> strukturtreue Extraktion → hierarchisches Chunking → hybride Suche → Reranking →
> gezielter Kontext → quellgebundene Antwort → Evaluation

Ein größeres Sprachmodell kann eine falsch gelesene Tabelle oder eine fehlende Fundstelle nicht
zuverlässig reparieren. Deshalb wird zuerst die richtige, vollständige Originalstelle gefunden und
erst danach eine Antwort erzeugt.

## Zielarchitektur

```text
PDF / Scan
  → PDF-Preflight: Fingerprint, Seitenlabels, Bookmarks, Text-/Scan-Seiten
  → Docling-Pass 1: Layout, Lesereihenfolge, Überschriften, Tabellen, Formeln, Bilder
  → globale Buchstruktur aus Bookmarks, Inhaltsverzeichnis und Body-Headings
  → Chunk-/Import-Pass 2: strukturorientierte Raw-Chunks mit Seiten-/Elementprovenienz
  → BookRAG-lite: abgeleitete Baum-, Register- und Glossarrouten zu Raw-Chunks
  → parallele hybride Facettensuche + gewichtete Reciprocal Rank Fusion
  → dedizierter Cross-Encoder-Reranker + adaptive Evidenzauswahl
  → claimweise Antwort aus Originalbelegen mit Buch, Auflage und Seite
```

Oracle hält Haiku RAG dabei unverändert. Die Anwendung nutzt ausschließlich dessen öffentliche
APIs und ergänzt die Betriebs-, Metadaten-, Vertrauens- und Evaluationsschicht außerhalb Haikus.
Der geprüfte Book-v2-Vertrag ist auf Haiku RAG Slim `0.74.0` und Docling `2.119.0` festgesetzt;
die Pipelinekennung lautet `book-index-v2`. Ein Upgrade dieser beiden Versionen ist kein
transparentes Patch-Update, sondern benötigt einen erneuten Kompatibilitätslauf und gegebenenfalls
einen vollständigen Rebuild.

## Dokumentaufbereitung

Erhalten werden müssen:

- Überschriftenhierarchie und Lesereihenfolge;
- echte PDF-Seiten und Bounding Boxes;
- Absätze, Listen, Definitionen und Merksätze;
- Tabellenstruktur einschließlich Überschriften und Einheiten;
- Formeln samt Variablenerklärung;
- Bildunterschriften, Abbildungsverweise und Seitenbilder;
- der unveränderte Originaltext für Zitate.

Eingebetteter Text hat Vorrang. OCR wird nur für Seiten ohne brauchbare Textebene aktiviert und
mit Deutsch und Englisch konfiguriert. Kopf- und Fußzeilen werden nicht als eigenständige
Wissens-Chunks indexiert. Jede Originaldatei wird vor der Verarbeitung SHA-256-adressiert und
unveränderlich im Workspace archiviert.

Book-v2 übergibt Docling immer die unveränderte Originaldatei und einen absoluten, 1-basierten,
inklusiven Seitenbereich. Es erzeugt weder PDF-Slices noch zusammengefügte Zwischen-PDFs und nutzt
pro Buch genau einen Converter. Der erste Pass sammelt globale Signale und cached das Konvertat;
erst nach der Reconciliation der vollständigen Buchstruktur werden im zweiten Pass Chunks erzeugt,
kontextualisiert, eingebettet und importiert.

Die Strukturreihenfolge ist:

1. PDF-Bookmarks und ein erkanntes gedrucktes Inhaltsverzeichnis werden miteinander und mit den
   Überschriften im Buchkörper abgeglichen;
2. ist nur eines dieser Signale vorhanden, wird es mit den Body-Headings ergänzt;
3. ohne Bookmarks oder Inhaltsverzeichnis dienen ausreichend belastbare Body-Headings als Struktur;
4. fehlt auch dieses Signal, decken deterministische Fenster das gesamte Buch ab (acht Textseiten
   beziehungsweise vier Scan-Seiten je Fenster); ein Vorspann vor dem ersten Kapitel erhält ein
   eigenes Fenster.

Sachregister, Glossar, Abkürzungs- und Symbolverzeichnis liefern Begriffe, Aliase und Seitenziele.
Abbildungs-, Tabellen- und Formelverzeichnisse liefern Caption-/Seitenrouten, werden aber nicht als
Kapitelanker missverstanden. Unsichere Navigationsregionen werden derzeit deterministisch verworfen.
`llm_fallback=auto` schaltet noch keinen lokalen Modellparser ein; Qualitätsbericht und Statistiken
weisen deshalb ehrlich `llm_fallback_used=false` aus. Gleiches gilt für angeforderte VLM-Anreicherung.

## Buchidentität und Auflagen

Vor dem Indexieren findet ein Preflight statt. Erkannte Daten werden als Vorschläge mit Quelle und
Konfidenz geliefert und müssen bestätigt werden. Pro Buch werden mindestens gespeichert:

- Werk-ID, Titel und Autoren;
- Auflage, Ausgabejahr und ISBN;
- Sprache, Curriculum und Tags;
- Status `active`, `superseded` oder `reference`;
- Gültigkeitszeitraum;
- Original- und verwalteter Quellpfad;
- Fingerprint, Pipeline-Version und Extraktionsqualität.

Alle Segmente eines Buches tragen dieselbe Identität. Standardmäßig durchsucht Oracle nur die
höchste aktive Auflage eines Werks. Explizite Filter können Werk, Titel, Autor, Auflage, Jahr,
ISBN, Sprache, Tags und Status wählen.

## Chunking und Evidenz

Strukturgrenzen haben Vorrang vor Tokenlängen. Der Startpunkt für neue Workspaces ist:

| Einstellung | Startwert |
|---|---:|
| Such-Chunk | 384 Tokens |
| sinnvoller Testbereich | 250 / 400 / 600 Tokens |
| Abschnittskontext | kanonischer globaler Heading-Pfad plus begrenzte Aliase |
| Range-Überlappung | keine; absolute Kernbereiche decken jede Seite genau einmal ab |
| Tabellen | Kopfzeilen und Einheiten erhalten |
| Überschriften | getrennt für Zitat und Embedding-Kontext erhalten |

Doclings `HybridChunker` führt kleine, zusammengehörige Peers zusammen und teilt übergroße
Strukturelemente tokenbewusst. Tabellen, Formeln und Abbildungen behalten ihre Elementreferenzen.
Chunks, die ausschließlich Seitenkopf oder Seitenfuß enthalten, werden verworfen.

Der Text eines Raw-Chunks wird durch Struktur- oder Heading-Hooks nicht verändert. Für Dense/FTS
wird lediglich ein abgeleiteter Kontext aus kanonischem Heading-Pfad und höchstens 24 priorisierten
Register-/Glossaraliasen (ungefähr 96 Tokens) verwendet; alle Aliase bleiben unabhängig davon im
Snapshot erhalten. Der Hash wird über genau diesen begrenzten Embedding-Kontext gebildet.

Jeder Chunk erhält eine stabile Evidence-ID, absolute Seiten, Elementreferenzen beziehungsweise
einen explizit markierten Seiten-Fallback, einen kanonischen Abschnitt sowie Previous/Next-Links.
Diese EvidenceRecords bilden zusammen mit Baum, Register, Glossar und Caption-Zielen einen
validierten Book-Knowledge-Snapshot. BookRAG-lite erzeugt daraus nur provenancegebundene Struktur-,
Alias-, Ziel- und begrenzte Ko-Okkurrenzkanten; es generiert keine fachlichen Tatsachentripel.
Raw-Chunks in Haiku bleiben die alleinige Belegquelle.

## Retrieval

Query-v2 nutzt Haikus öffentliche Hybridsuche als Kandidatenquelle:

- FTS/BM25 für Normen, Fachzeichen, Zahlen, Formeln und exakte Bezeichnungen;
- Dense Retrieval für Synonyme und bedeutungsgleiche Schülerfragen;
- parallele Facetten für Vergleiche, Mehrfachfragen und buchweite Zusammenhänge;
- zusätzliche Tree-/Register-/BookRAG-lite-Routen, die immer zu Raw-Chunks zurückführen;
- gewichtete RRF zur score-unabhängigen Fusion;
- genau ein dediziertes Cross-Encoder-Reranking für die endgültige Reihenfolge;
- kalibrierte Schwellen, Score-Gap/Top-Delta, Deduplizierung und MMR-Diversität;
- tokenbegrenzte Evidenzfenster um die ausgewählten Raw-Chunks.

Dokument- und Auflagenfilter werden vor der Suche in konkrete Haiku-Dokument-IDs aufgelöst und als
öffentlicher Suchfilter übergeben. Haikus interner Reranker bleibt in dieser Kandidatenphase aus;
dadurch werden Facetten erst fusioniert und anschließend gemeinsam einmal gererankt.

Die Komplexität wird ohne LLM aus Signalen wie direktem Lookup, Vergleich, Mehrfachfrage,
Multi-Hop-, Global- und Berechnungsformulierung bestimmt. Die Basisbudgets sind verbindlich
begrenzt:

| Komplexität | Kandidaten | finale Evidenz | Facetten | Evidenztokens | Antworttokens | absolute RAG-Deadline |
|---|---:|---:|---:|---:|---:|---:|
| einfach | 24 | 1–5 | 1 | 320 | 256 | 15 s |
| standard | 40 | 2–8 | 2 | 1.200 | 384 | 25 s |
| komplex | 72 | 4–14 | 4 | 2.400 | 512 | 35 s |

`fast`, `balanced` und `deep` begrenzen beziehungsweise erweitern diese Budgets innerhalb der
harten Obergrenzen; `max_sources` kann die finale Auswahl zusätzlich bis maximal 14 begrenzen.
Die Deadline umfasst nicht nur das Modell, sondern Readiness, Cacheprüfung, Ressourcenzulassung,
Retrieval, Generierung und Persistenz. Mehr Kontext ist daher eine bewusste Qualitäts-/Latenzwahl.

Schlägt eine einzelne Facette oder der Buchrouter fehl, laufen verbleibende Retrievalpfade weiter
und die Degradation erscheint im Receipt. Ohne kalibrierten Reranker erzeugt Query-v2 keine
scheinbar belegte Antwort, sondern meldet unzureichende Evidenz. Nur der Retrieval-Inspector zeigt
in diesem Fall bis zu drei fusionierte Treffer ausdrücklich als unkalibriert an.

## Antwortmodi

### Strict

- ausschließlich aus bereitgestellten Quellen;
- jede wesentliche Aussage benötigt Evidenz;
- Zahlen, Einheiten, Normen und Formelzeichen müssen in den Belegen vorkommen;
- fehlen Belege, lautet die Antwort: „In den bereitgestellten Quellen nicht ausreichend belegt.“

### Normal

Quellen haben Vorrang. Nachvollziehbare Schlussfolgerungen werden ausdrücklich gekennzeichnet.

### Explore

Ergänzendes Modellwissen ist erlaubt, muss aber sichtbar von Quellen und Schlussfolgerungen
getrennt werden.

Jeder für eine Antwort ausgewählte Beleg erhält eine kompakte Prompt-/Anzeige-ID (`E1`, `E2`, …),
die nur innerhalb dieser Antwort gilt. Für Audit, Cachebindung und Joins trägt derselbe Beleg
zusätzlich seine stabile `ev-…`-Evidence-ID. Titel, Auflage und Seite stammen aus
Anwendungsmetadaten und nicht aus frei erzeugten Seitenangaben des Sprachmodells.

## Evaluation

Retriever und Generator werden getrennt beurteilt. Oracle erzeugt zunächst reproduzierbare
Silver-Fragen aus Überschriften, Seiten und Chunk-Provenienz. Dieselben Fälle laufen als A/B-Test
über `fts`, `vector` und `hybrid`.

Gemessen werden:

- Recall@5 und Recall@10;
- MRR und nDCG@10;
- Trefferquote der richtigen Seite.

Für belastbare fachliche Freigaben wird der Silver-Satz anschließend zu einem manuell geprüften
Goldstandard ausgebaut. Er sollte Definitionen, Zahlen, Formeln, Tabellen, Bilder, Vergleiche,
mehrstufige Fragen, Auflagenkonflikte sowie beantwortbare und unbeantwortbare Fälle enthalten.

## Ressourcen- und Latenzprofil

Indexierungsranges und Worker-Residency reagieren auf Arbeitsspeicher und Chat-Priorität. Die
Querybudgets richten sich dagegen nach Fragekomplexität und dem gewählten Profil; sie werden nicht
als unkontrollierter RAM-Prozentsatz skaliert. Der Cross-Encoder bleibt ein eigener, persistenter
Query-Worker-Baustein und wird nicht pro Facette neu geladen.

Die globale `/v1/readiness` bestätigt Prozess, SQLite und kompatiblen Adapter, aber keine warme
Antwortlatenz. Die Workspace-Readiness prüft zusätzlich eine abfragbare Indexgeneration sowie
Generator und Embeddingmodell. Nicht residente Modelle ergeben `latency_degraded` und benötigen
Warm-up; ein falscher residenter Modelldigest oder ein Embeddingdigest, der nicht zur READY-
Generation passt, ist dagegen ein harter Fehler. Rerankerfehler werden wie oben beschrieben nicht
als normale Relevanzbewertung kaschiert.

Ein Full-Rebuild beginnt nur nach gespeichertem Preflight und explizitem `REINDEX`. Geprüft werden
immutable Originale samt SHA-256, freier Speicher, schreibbarer Cache, die exakten Haiku-/Docling-
Versionen, Workspace-Konfigurationshash und Embeddingdigest. Diese Werte sowie der Katalogzustand
werden unmittelbar vor destruktiven Schritten und am exklusiven Rebuild-Gate erneut geprüft. Der
Rebuild ist in-place und besitzt keinen Live-Rollback, bleibt aber über Checkpoints fortsetzbar.
Während `maintenance` und nach `maintenance_failed` sind Fragen blockiert. Erst die Validierung von
Seitenabdeckung, Manifesten, Evidence-Kette, Struktur, Snapshot und Graph veröffentlicht `ready`.

Vollständiges generatives GraphRAG, flächendeckende visuelle Seitensuche und Late Chunking sind
nicht aktiv. Vorhanden ist ausschließlich das kleine deterministische BookRAG-lite-Routing; weitere
Verfahren benötigen einen messbaren Vorteil im eigenen Evaluationssatz.

## Typische Fehlkonfigurationen

| Fehlkonfiguration | Folge |
|---|---|
| PDF nur als Fließtext | Tabellen, Seiten und Lesereihenfolge gehen verloren |
| starre Zeichen-Chunks | fachliche Einheiten werden getrennt |
| große pauschale Überlappung | redundanter Index und mehrfacher Kontext |
| nur Vektorsuche | Normen, Kennwerte und Kurzzeichen werden schlechter gefunden |
| nur BM25 | sinngleiche Fragen werden übersehen |
| zu kleiner Kandidatenpool | relevante Quelle fehlt schon vor dem Reranking |
| allgemeines LLM als Reranker | instabile Reihenfolge und erfundene Fundstellen |
| möglichst viel Kontext | Rauschen verdrängt die Belege |
| Auflagen vermischen | widersprüchliche oder veraltete Antworten |
| keine unbeantwortbaren Tests | das System lernt nie, begründet nicht zu antworten |

## Prioritäten

1. korrekte strukturierte Dokumentextraktion;
2. repräsentativer Evaluationsdatensatz;
3. strukturorientiertes Chunking und Provenienz;
4. BM25 plus Dense Retrieval;
5. dedizierter Cross-Encoder-Reranker;
6. Tabellen- und Formelqualität;
7. gezielte Kontextrekonstruktion und Deduplizierung;
8. verbindliche Seiten- und Auflagenmetadaten;
9. Quellenbindung und Nichtantwort;
10. Spezialverfahren erst nach gemessenem Bedarf.
