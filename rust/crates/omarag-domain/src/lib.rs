//! Stable, frontend-independent OmaRag API types.
//!
//! This crate intentionally has no HTTP or terminal dependencies.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub type WorkspaceId = String;
pub type JobId = String;
pub type RunId = String;
pub type EventId = u64;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilitySet {
    #[serde(default)]
    pub streaming_chat: bool,
    #[serde(default)]
    pub question_images: bool,
    #[serde(default)]
    pub analysis_images: bool,
    #[serde(default)]
    pub multimodal_search: bool,
    #[serde(default)]
    pub multimodal_reranking: bool,
    #[serde(default)]
    pub visual_grounding: bool,
    #[serde(default)]
    pub database_tags: bool,
    #[serde(default)]
    pub native_ingester: bool,
    #[serde(default)]
    pub evaluation: bool,
    #[serde(default)]
    pub event_replay: bool,
    #[serde(default)]
    pub workspaces: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendMeta {
    pub api_version: String,
    pub min_client_version: String,
    pub max_client_version: String,
    pub omarag_version: String,
    pub haiku_version: Option<String>,
    pub adapter: Option<String>,
    pub backend_id: String,
    pub capabilities: CapabilitySet,
    #[serde(default)]
    pub deprecations: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HealthReport {
    pub status: String,
    pub ready: bool,
    #[serde(default)]
    pub checks: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceSummary {
    pub id: WorkspaceId,
    pub name: String,
    pub path: String,
    pub read_only: bool,
    pub updated_at: String,
    pub etag: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum PrivacyMode {
    Local,
    CloudAllowed,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EvidenceMode {
    #[default]
    Strict,
    Normal,
    Explore,
}

impl std::fmt::Display for EvidenceMode {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Strict => write!(f, "streng"),
            Self::Normal => write!(f, "normal"),
            Self::Explore => write!(f, "erkunden"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkspaceManifest {
    pub schema_version: u32,
    pub id: WorkspaceId,
    pub name: String,
    pub created_at: String,
    pub updated_at: String,
    pub path: String,
    pub read_only: bool,
    pub haiku_compatible_range: String,
    pub database_schema_version: String,
    pub embedding_provider: String,
    pub embedding_model: String,
    pub vector_dimension: Option<u32>,
    pub processing_profile: String,
    pub evidence_mode: EvidenceMode,
    pub document_policy: String,
    pub privacy_mode: PrivacyMode,
    pub cloud_acknowledged: bool,
    pub etag: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DocumentSummary {
    pub id: String,
    pub title: String,
    pub source: String,
    #[serde(default)]
    pub segment_document_ids: Vec<String>,
    #[serde(default)]
    pub page_count: Option<u32>,
    #[serde(default = "default_docling_parser")]
    pub parser_id: String,
    pub status: String,
    pub imported_at: String,
}

fn default_docling_parser() -> String {
    "docling".into()
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceDefinition {
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub source_type: String,
    pub location: String,
    pub enabled: bool,
    pub created_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CreateSource {
    pub name: String,
    #[serde(rename = "type")]
    pub source_type: String,
    pub location: String,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QualityReport {
    pub workspace_id: WorkspaceId,
    pub status: String,
    pub document_count: usize,
    pub completed_imports: usize,
    pub failed_jobs: usize,
    #[serde(default)]
    pub issues: Vec<String>,
    pub generated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackupSummary {
    pub id: String,
    pub workspace_id: WorkspaceId,
    pub created_at: String,
    pub path: String,
    pub size_bytes: u64,
    pub sha256: String,
    pub verified: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConfigDocument {
    pub content: String,
    pub etag: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdateConfig {
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Queued,
    Running,
    PauseRequested,
    Paused,
    Completed,
    Cancelled,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JobSnapshot {
    pub id: JobId,
    pub workspace_id: WorkspaceId,
    pub kind: String,
    pub status: JobStatus,
    pub progress: f64,
    pub phase: String,
    #[serde(default)]
    pub payload: Value,
    pub result: Option<Value>,
    pub error: Option<Value>,
    pub created_at: String,
    pub updated_at: String,
    pub last_event_id: Option<EventId>,
    pub checkpoint: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchRequest {
    pub query: String,
    #[serde(default = "default_search_limit")]
    pub limit: u32,
    #[serde(default)]
    pub filters: BTreeMap<String, Value>,
}

const fn default_search_limit() -> u32 {
    10
}

impl SearchRequest {
    pub fn new(query: impl Into<String>) -> Self {
        Self {
            query: query.into(),
            limit: default_search_limit(),
            filters: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchHit {
    pub chunk_id: String,
    pub content: String,
    pub score: Option<f64>,
    #[serde(default)]
    pub pages: Vec<u32>,
    pub document_id: Option<String>,
    pub document_title: Option<String>,
    #[serde(default)]
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunRequest {
    pub session_id: Option<String>,
    pub mode: String,
    pub question: String,
    #[serde(default)]
    pub images: Vec<String>,
    pub evidence_mode: EvidenceMode,
    pub document_policy: String,
    #[serde(default)]
    pub filters: BTreeMap<String, Value>,
}

impl RunRequest {
    pub fn question(question: impl Into<String>, evidence_mode: EvidenceMode) -> Self {
        Self {
            session_id: None,
            mode: "rag".into(),
            question: question.into(),
            images: Vec::new(),
            evidence_mode,
            document_policy: "current-only".into(),
            filters: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CitationAnchor {
    pub page: u32,
    pub doc_item_ref: String,
    pub element_type: Option<String>,
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Citation {
    pub chunk_id: String,
    #[serde(default)]
    pub chunk_ids: Vec<String>,
    pub document_id: Option<String>,
    #[serde(default)]
    pub logical_document_id: Option<String>,
    #[serde(default)]
    pub source_uri: Option<String>,
    pub document_title: Option<String>,
    #[serde(default)]
    pub pages: Vec<u32>,
    #[serde(default)]
    pub headings: Vec<String>,
    #[serde(default)]
    pub element_types: Vec<String>,
    #[serde(default)]
    pub doc_item_refs: Vec<String>,
    #[serde(default)]
    pub picture_refs: Vec<String>,
    #[serde(default)]
    pub primary_anchors: Vec<CitationAnchor>,
    #[serde(default)]
    pub context_anchors: Vec<CitationAnchor>,
    pub excerpt: String,
    pub retrieval_rank: Option<u32>,
    pub rerank_score: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunSnapshot {
    pub id: RunId,
    pub workspace_id: WorkspaceId,
    pub status: JobStatus,
    pub question: String,
    pub evidence_mode: EvidenceMode,
    pub answer: String,
    #[serde(default)]
    pub citations: Vec<Citation>,
    pub error: Option<Value>,
    pub created_at: String,
    pub updated_at: String,
    pub last_event_id: Option<EventId>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DomainEvent {
    pub event_id: EventId,
    pub sequence: u64,
    pub timestamp: String,
    #[serde(rename = "type")]
    pub event_type: String,
    pub workspace_id: Option<WorkspaceId>,
    pub job_id: Option<JobId>,
    pub run_id: Option<RunId>,
    pub correlation_id: String,
    pub schema_version: u32,
    #[serde(default)]
    pub payload: Value,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CreateWorkspace {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default)]
    pub read_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IdempotentResult {
    pub id: String,
    pub reused: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IngestSource {
    #[serde(rename = "type")]
    pub source_type: String,
    pub path: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IngestRequest {
    pub sources: Vec<IngestSource>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub metadata: BTreeMap<String, Value>,
    #[serde(default = "default_parser_id")]
    pub parser_id: String,
    pub processing_profile: String,
    pub duplicate_policy: String,
    pub validity_policy: String,
}

fn default_parser_id() -> String {
    "auto".into()
}

impl IngestRequest {
    pub fn file(path: impl Into<String>) -> Self {
        Self::files([path])
    }

    pub fn files(paths: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            sources: paths
                .into_iter()
                .map(|path| IngestSource {
                    source_type: "file".into(),
                    path: path.into(),
                })
                .collect(),
            tags: Vec::new(),
            metadata: BTreeMap::new(),
            parser_id: default_parser_id(),
            processing_profile: "default".into(),
            duplicate_policy: "review".into(),
            validity_policy: "prefer-current".into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventSubscription {
    pub workspace_id: Option<WorkspaceId>,
    pub job_id: Option<JobId>,
    pub run_id: Option<RunId>,
    pub last_event_id: Option<EventId>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApiErrorBody {
    pub code: String,
    pub message: String,
    #[serde(default)]
    pub details: Value,
    pub correlation_id: Option<String>,
    pub retryable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ApiErrorResponse {
    pub error: ApiErrorBody,
}

#[derive(Debug, thiserror::Error)]
pub enum OmaRagError {
    #[error("Backend nicht erreichbar: {0}")]
    Transport(String),
    #[error("Backendfehler {status} ({code}): {message}")]
    Api {
        status: u16,
        code: String,
        message: String,
        retryable: bool,
    },
    #[error("Ungueltige Backendantwort: {0}")]
    Protocol(String),
}

pub type OmaResult<T> = Result<T, OmaRagError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_event_payload_fields_are_accepted() {
        let raw = r#"{
            "event_id": 7, "sequence": 1, "timestamp": "2026-07-25T00:00:00Z",
            "type": "future.event", "workspace_id": null, "job_id": null,
            "run_id": null, "correlation_id": "c", "schema_version": 1,
            "payload": {"future": true}, "future_envelope_field": 12
        }"#;
        let event: DomainEvent = serde_json::from_str(raw).unwrap();
        assert_eq!(event.event_type, "future.event");
        assert_eq!(event.payload["future"], true);
    }

    #[test]
    fn run_request_has_stable_wire_values() {
        let request = RunRequest::question("Was ist Beton?", EvidenceMode::Strict);
        let json = serde_json::to_value(request).unwrap();
        assert_eq!(json["evidence_mode"], "strict");
        assert_eq!(json["mode"], "rag");
    }
}
