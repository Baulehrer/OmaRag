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
  → Docling: Layout, Lesereihenfolge, Überschriften, Tabellen, Formeln, Bilder
  → strukturiertes Dokument mit Seiten- und Elementprovenienz
  → kleine, strukturorientierte Such-Chunks plus Abschnittskontext
  → BM25/FTS + Dense Retrieval
  → Reciprocal Rank Fusion
  → dedizierter Cross-Encoder-Reranker
  → wenige relevante Evidenzeinheiten
  → Antwort aus Quellen mit Buch, Auflage und Seite
```

Oracle hält Haiku RAG dabei unverändert. Die Anwendung nutzt ausschließlich dessen öffentliche
APIs und ergänzt die Betriebs-, Metadaten-, Vertrauens- und Evaluationsschicht außerhalb Haikus.

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

## Chunking

Strukturgrenzen haben Vorrang vor Tokenlängen. Der Startpunkt für neue Workspaces ist:

| Einstellung | Startwert |
|---|---:|
| Such-Chunk | 384 Tokens |
| sinnvoller Testbereich | 250 / 400 / 600 Tokens |
| Abschnittskontext | vollständiger Parent, bedarfsweise |
| Überlappung | gering; Segmentgrenze eine Seite |
| Tabellen | Kopfzeilen und Einheiten erhalten |
| Überschriften | im Chunk-Kontext erhalten |

Doclings `HybridChunker` führt kleine, zusammengehörige Peers zusammen und teilt übergroße
Strukturelemente tokenbewusst. Tabellen, Formeln und Abbildungen behalten ihre Elementreferenzen.
Chunks, die ausschließlich Seitenkopf oder Seitenfuß enthalten, werden verworfen.

## Retrieval

Die Standardsuche ist Haikus native Hybridsuche:

- FTS/BM25 für Normen, Fachzeichen, Zahlen, Formeln und exakte Bezeichnungen;
- Dense Retrieval für Synonyme und bedeutungsgleiche Schülerfragen;
- RRF zur score-unabhängigen Fusion;
- Cross-Encoder-Reranking für die endgültige Reihenfolge;
- Kontextrekonstruktion aus benachbarten Chunks innerhalb des Zeichenbudgets.

Oracle ersetzt keinen dieser Haiku-Bausteine. Dokument- und Auflagenfilter werden vor der Suche in
konkrete Haiku-Dokument-IDs aufgelöst und als öffentlicher Suchfilter übergeben.

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

Jeder Beleg erhält eine unveränderliche ID (`E1`, `E2`, …). Titel, Auflage und Seite stammen aus
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

## CPU-Profil

Oracle passt Batchgröße, Trefferzahl und Kontextbudget an den Arbeitsspeicher an. Auf einem
CPU-Rechner unter 18 GiB startet es mit Embedding-Batch 16, sechs Ergebnissen und 4.000 Zeichen
Antwortkontext. Zwischen 18 und 32 GiB gelten 32, acht und 5.000. Größere Systeme verwenden 64,
acht und 6.000. Der Cross-Encoder bleibt klein und mehrsprachig.

Optionen wie GraphRAG, visuelle Seitensuche oder Late Chunking werden erst aktiviert, wenn der
eigene Evaluationssatz dafür einen messbaren Vorteil zeigt.

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
