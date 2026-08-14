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

/// Coarse hardware class used to select a harmonious local model stack.
///
/// The wire representation deliberately stays numeric (`1..=10`) so future
/// catalogs can change their model choices without requiring a client update.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(transparent)]
pub struct HardwareTier(u8);

impl HardwareTier {
    pub const MIN: u8 = 1;
    pub const MAX: u8 = 10;

    pub const fn new(level: u8) -> Option<Self> {
        if level >= Self::MIN && level <= Self::MAX {
            Some(Self(level))
        } else {
            None
        }
    }

    pub const fn level(self) -> u8 {
        self.0
    }

    /// Conservative legacy-only fallback. The server-side scan remains the
    /// authority because accelerator backends and unified memory vary widely.
    pub fn for_capacity(system_memory_bytes: u64, dedicated_vram_bytes: u64) -> Self {
        const GIB: u64 = 1_073_741_824;
        let ram = system_memory_bytes / GIB;
        let vram = dedicated_vram_bytes / GIB;
        let level = if ram >= 64 && vram >= 24 {
            10
        } else if (ram >= 32 && vram >= 24)
            || (ram >= 48 && vram >= 20)
            || (ram >= 64 && vram >= 16)
        {
            9
        } else if (ram >= 32 && vram >= 16) || (ram >= 48 && vram >= 12) {
            8
        } else if ram >= 32 && vram >= 8 {
            7
        } else if (ram >= 24 && vram >= 8) || (ram >= 32 && vram == 0) {
            6
        } else if ram >= 16 && vram >= 8 {
            5
        } else if ram >= 16 && vram >= 4 {
            4
        } else if ram >= 16 {
            3
        } else if ram >= 12 {
            2
        } else {
            1
        };
        Self(level)
    }
}

impl Default for HardwareTier {
    fn default() -> Self {
        Self(Self::MIN)
    }
}

impl<'de> Deserialize<'de> for HardwareTier {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct TierVisitor;

        impl serde::de::Visitor<'_> for TierVisitor {
            type Value = HardwareTier;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a hardware tier from 1 through 10")
            }

            fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                u8::try_from(value)
                    .ok()
                    .and_then(HardwareTier::new)
                    .ok_or_else(|| E::custom(format!("hardware tier must be 1..=10, got {value}")))
            }

            fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                u64::try_from(value)
                    .map_err(|_| E::custom(format!("hardware tier must be positive, got {value}")))
                    .and_then(|value| self.visit_u64(value))
            }

            fn visit_str<E>(self, value: &str) -> Result<Self::Value, E>
            where
                E: serde::de::Error,
            {
                value
                    .parse::<u64>()
                    .map_err(|_| E::custom(format!("invalid hardware tier {value:?}")))
                    .and_then(|value| self.visit_u64(value))
            }
        }

        deserializer.deserialize_any(TierVisitor)
    }
}

impl std::fmt::Display for HardwareTier {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PerformanceProfile {
    Fast,
    #[default]
    #[serde(alias = "balanced", alias = "standard")]
    Normal,
    Quality,
}

impl PerformanceProfile {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Fast => "Fast",
            Self::Normal => "Normal",
            Self::Quality => "Quality",
        }
    }

    pub const fn api_label(self) -> &'static str {
        match self {
            Self::Fast => "fast",
            Self::Normal => "normal",
            Self::Quality => "quality",
        }
    }
}

impl std::fmt::Display for PerformanceProfile {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.label())
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelRecommendation {
    #[serde(default)]
    pub role: String,
    #[serde(default, alias = "model_id")]
    pub model: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default, alias = "memory_bytes")]
    pub required_bytes: Option<u64>,
    #[serde(default, alias = "context_length")]
    pub context_tokens: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HardwareRecommendationRequest {
    pub performance_profile: PerformanceProfile,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_id: Option<WorkspaceId>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HardwareProfileResponse {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    #[serde(default, alias = "hardware_tier")]
    pub tier: HardwareTier,
    #[serde(default, alias = "hardware_tier_label")]
    pub tier_label: String,
    #[serde(default, alias = "bottleneck")]
    pub limiting_factor: String,
    #[serde(default, alias = "model_catalog_version")]
    pub catalog_version: String,
    #[serde(default)]
    pub scanned_at: Option<String>,
    #[serde(default, alias = "subprofile", alias = "performance_profile")]
    pub profile: PerformanceProfile,
    #[serde(default)]
    pub expert_mode: bool,
    #[serde(default, alias = "models")]
    pub recommendations: Vec<ModelRecommendation>,
}

impl Default for HardwareProfileResponse {
    fn default() -> Self {
        Self {
            schema_version: default_schema_version(),
            tier: HardwareTier::default(),
            tier_label: String::new(),
            limiting_factor: String::new(),
            catalog_version: String::new(),
            scanned_at: None,
            profile: PerformanceProfile::default(),
            expert_mode: false,
            recommendations: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum CatalogRole {
    Chat,
    Vl,
    Embedding,
    Rerank,
    VisualEmbedding,
    #[serde(other)]
    Unknown,
}

impl CatalogRole {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Chat => "Chat",
            Self::Vl => "VL",
            Self::Embedding => "Embedding",
            Self::Rerank => "Reranker",
            Self::VisualEmbedding => "Visual embedding",
            Self::Unknown => "Other",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum CatalogProvider {
    Ollama,
    HuggingFace,
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ModelInstallState {
    Installed,
    NotInstalled,
    DigestMismatch,
    #[serde(other)]
    #[default]
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelAssignment {
    pub role: CatalogRole,
    pub artifact_id: String,
    pub provider: CatalogProvider,
    pub model: String,
    pub revision: String,
    pub digest: String,
    #[serde(default)]
    pub quantization: Option<String>,
    #[serde(default)]
    pub install_state: ModelInstallState,
    #[serde(default)]
    pub installed_digest: Option<String>,
    #[serde(default)]
    pub download_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelStackRecommendation {
    pub recommendation_id: String,
    #[serde(default)]
    pub catalog_id: String,
    #[serde(default)]
    pub catalog_release: String,
    pub profile: PerformanceProfile,
    pub stack_tier: HardwareTier,
    #[serde(default)]
    pub assignments: Vec<ModelAssignment>,
    #[serde(default)]
    pub context_tokens: u32,
    #[serde(default)]
    pub total_download_bytes: u64,
    #[serde(default)]
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelProfilePreflight {
    pub recommendation: ModelStackRecommendation,
    #[serde(default)]
    pub changes: BTreeMap<String, String>,
    #[serde(default)]
    pub downloads: Vec<ModelAssignment>,
    #[serde(default)]
    pub requires_reindex: bool,
    #[serde(default)]
    pub requires_visual_reindex: bool,
    #[serde(default)]
    pub can_apply: bool,
    #[serde(default)]
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelProfilePreflightRequest {
    pub performance_profile: PerformanceProfile,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ModelProfileApplyConfirmation {
    #[serde(rename = "APPLY")]
    Apply,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ModelDownloadConsent {
    #[serde(rename = "DOWNLOAD_MODELS")]
    DownloadModels,
}

/// Deliberately omits `reindex_consent`: embedding changes must go through the
/// full rebuild workflow and can never be partially applied by this client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelProfileApplyRequest {
    pub preflight_id: String,
    pub confirm: ModelProfileApplyConfirmation,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub download_consent: Option<ModelDownloadConsent>,
}

impl ModelProfileApplyRequest {
    pub fn new(preflight_id: impl Into<String>, allow_downloads: bool) -> Self {
        Self {
            preflight_id: preflight_id.into(),
            confirm: ModelProfileApplyConfirmation::Apply,
            download_consent: allow_downloads.then_some(ModelDownloadConsent::DownloadModels),
        }
    }
}

const fn default_schema_version() -> u32 {
    1
}

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
    #[serde(default)]
    pub book_index_v2: bool,
    #[serde(default)]
    pub adaptive_retrieval: bool,
    #[serde(default)]
    pub claim_streaming: bool,
    #[serde(default)]
    pub knowledge_snapshots: bool,
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
    #[serde(default = "default_haiku_update_policy")]
    pub haiku_update_policy: String,
    #[serde(default)]
    pub haiku_last_verified: Option<String>,
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

fn default_haiku_update_policy() -> String {
    "latest-gated".into()
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct BookMetadata {
    #[serde(default)]
    pub work_id: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub authors: Vec<String>,
    #[serde(default)]
    pub edition_label: Option<String>,
    #[serde(default)]
    pub edition_number: Option<u32>,
    #[serde(default)]
    pub publication_year: Option<u32>,
    #[serde(default)]
    pub isbn: Vec<String>,
    #[serde(default = "default_language")]
    pub language: String,
    #[serde(default)]
    pub curriculum: Option<String>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_document_status")]
    pub document_status: String,
    #[serde(default)]
    pub valid_from: Option<String>,
    #[serde(default)]
    pub valid_to: Option<String>,
    #[serde(default)]
    pub confirmed: bool,
}

fn default_language() -> String {
    "de".into()
}

fn default_document_status() -> String {
    "active".into()
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DocumentQuality {
    #[serde(default = "default_quality_score")]
    pub score: f64,
    #[serde(default)]
    pub pages_total: u32,
    #[serde(default)]
    pub native_text_pages: u32,
    #[serde(default)]
    pub ocr_pages: u32,
    #[serde(default)]
    pub chunks: u32,
    #[serde(default)]
    pub tables: u32,
    #[serde(default)]
    pub formulas: u32,
    #[serde(default)]
    pub pictures: u32,
    #[serde(default)]
    pub provenance_coverage: f64,
    #[serde(default)]
    pub substantive_coverage: f64,
    #[serde(default = "default_unknown")]
    pub structure_mode: String,
    #[serde(default)]
    pub structure_confidence: f64,
    #[serde(default)]
    pub toc_found: bool,
    #[serde(default)]
    pub index_found: bool,
    #[serde(default)]
    pub glossary_found: bool,
    #[serde(default)]
    pub fallback_used: bool,
    #[serde(default)]
    pub llm_fallback_used: bool,
    #[serde(default)]
    pub exact_duplicate_count: u32,
    #[serde(default)]
    pub issues: Vec<String>,
}

const fn default_quality_score() -> f64 {
    1.0
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
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
    #[serde(default)]
    pub fingerprint: Option<String>,
    #[serde(default)]
    pub generation_id: Option<String>,
    #[serde(default)]
    pub cache_status: Option<String>,
    #[serde(default)]
    pub pipeline_stats: BTreeMap<String, Value>,
    #[serde(default)]
    pub managed_source: Option<String>,
    #[serde(default)]
    pub book: Option<BookMetadata>,
    #[serde(default)]
    pub quality: Option<DocumentQuality>,
    #[serde(default = "default_pipeline_version")]
    pub pipeline_version: String,
    #[serde(default = "default_unknown")]
    pub structure_mode: String,
    #[serde(default)]
    pub structure_confidence: f64,
    #[serde(default)]
    pub toc_found: bool,
    #[serde(default)]
    pub index_found: bool,
    #[serde(default)]
    pub glossary_found: bool,
    #[serde(default)]
    pub fallback_used: bool,
    #[serde(default)]
    pub size_bytes: u64,
    #[serde(default = "default_archive_mode")]
    pub archive_mode: String,
}

fn default_archive_mode() -> String {
    "unknown".into()
}

fn default_docling_parser() -> String {
    "docling".into()
}

fn default_pipeline_version() -> String {
    "textbook-v1".into()
}

fn default_unknown() -> String {
    "unknown".into()
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QualityReport {
    pub workspace_id: WorkspaceId,
    pub status: String,
    pub document_count: usize,
    pub completed_imports: usize,
    pub failed_jobs: usize,
    #[serde(default)]
    pub issues: Vec<String>,
    #[serde(default)]
    pub latest_evaluation_id: Option<String>,
    #[serde(default)]
    pub retrieval_metrics: BTreeMap<String, f64>,
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
    #[serde(default)]
    pub pinned: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DocumentPurgePlan {
    pub plan_id: String,
    pub workspace_id: WorkspaceId,
    pub document_id: String,
    pub generation_id: String,
    pub fingerprint: String,
    #[serde(default)]
    pub segment_document_ids: Vec<String>,
    #[serde(default)]
    pub media_assets: u64,
    #[serde(default)]
    pub pinned_run_ids: Vec<String>,
    #[serde(default)]
    pub backup_ids: Vec<String>,
    #[serde(default)]
    pub pinned_backup_ids: Vec<String>,
    #[serde(default)]
    pub requires_backup_confirmation: bool,
    #[serde(default)]
    pub can_purge: bool,
    pub created_at: String,
    pub expires_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecuteDocumentPurgeRequest {
    pub plan_id: String,
    pub confirm: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backup_confirm: Option<String>,
}

impl ExecuteDocumentPurgeRequest {
    pub fn confirmed(plan_id: String, purge_backups: bool) -> Self {
        Self {
            plan_id,
            confirm: "PURGE_DOCUMENT".into(),
            backup_confirm: purge_backups.then(|| "PURGE_BACKUPS".into()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DocumentPurgeResult {
    pub workspace_id: WorkspaceId,
    pub document_id: String,
    pub generation_id: String,
    #[serde(default)]
    pub removed_segments: u64,
    #[serde(default)]
    pub removed_media_assets: u64,
    #[serde(default)]
    pub removed_backups: u64,
    #[serde(default)]
    pub original_removed: bool,
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
    #[serde(default)]
    pub progress_detail: Option<JobProgressDetail>,
    #[serde(default)]
    pub pinned: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct JobProgressDetail {
    pub current_document: Option<String>,
    pub page_start: Option<u32>,
    pub page_end: Option<u32>,
    pub total_pages: Option<u32>,
    #[serde(default)]
    pub cache_hits: u32,
    #[serde(default)]
    pub recovered_segments: u32,
    #[serde(default)]
    pub memory_state: String,
    #[serde(default)]
    pub eta_seconds_low: Option<f64>,
    #[serde(default)]
    pub eta_seconds_high: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SearchRequest {
    pub query: String,
    #[serde(default = "default_search_limit")]
    pub limit: u32,
    #[serde(default)]
    pub filters: BTreeMap<String, Value>,
    #[serde(default = "default_document_policy")]
    pub document_policy: String,
    #[serde(default)]
    pub options: SearchOptions,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchOptions {
    #[serde(default = "default_auto")]
    pub profile: String,
    #[serde(default)]
    pub max_sources: Option<u32>,
    #[serde(default)]
    pub deadline_ms: Option<u32>,
}

impl Default for SearchOptions {
    fn default() -> Self {
        Self {
            profile: default_auto(),
            max_sources: None,
            deadline_ms: None,
        }
    }
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
            document_policy: default_document_policy(),
            options: SearchOptions::default(),
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
    #[serde(default = "default_search_type")]
    pub search_type: String,
}

fn default_search_type() -> String {
    "hybrid".into()
}

fn default_document_policy() -> String {
    "current-only".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RetrievalTiming {
    pub search_ms: f64,
    pub total_ms: f64,
    #[serde(default)]
    pub routing_ms: f64,
    #[serde(default)]
    pub rerank_ms: f64,
    #[serde(default)]
    pub pack_ms: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RetrievalExplanation {
    pub query: String,
    #[serde(default)]
    pub candidates: Vec<SearchHit>,
    #[serde(default)]
    pub ranked: Vec<SearchHit>,
    pub timing: RetrievalTiming,
    #[serde(default)]
    pub provider_notes: Vec<String>,
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
    #[serde(default)]
    pub options: RunOptions,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VerifierMode {
    #[default]
    Auto,
    Off,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunOptions {
    #[serde(default = "default_auto")]
    pub profile: String,
    #[serde(default = "default_auto")]
    pub memory: String,
    #[serde(default)]
    pub max_sources: Option<u32>,
    #[serde(default)]
    pub max_answer_tokens: Option<u32>,
    #[serde(default)]
    pub deadline_ms: Option<u32>,
    #[serde(default)]
    pub verifier: VerifierMode,
}

impl Default for RunOptions {
    fn default() -> Self {
        Self {
            profile: default_auto(),
            memory: default_auto(),
            max_sources: None,
            max_answer_tokens: None,
            deadline_ms: None,
            verifier: VerifierMode::default(),
        }
    }
}

fn default_auto() -> String {
    "auto".into()
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
            options: RunOptions::default(),
        }
    }

    pub fn with_session_id(mut self, session_id: impl Into<String>) -> Self {
        self.session_id = Some(session_id.into());
        self
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

#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct MediaBoundingBox {
    #[serde(default, alias = "left")]
    pub x0: f64,
    #[serde(default, alias = "top")]
    pub y0: f64,
    #[serde(default, alias = "right")]
    pub x1: f64,
    #[serde(default, alias = "bottom")]
    pub y1: f64,
    /// Coordinate system used by the extractor, for example `pdf_points` or
    /// `normalized`. Consumers must not guess when this is absent.
    #[serde(default)]
    pub coordinate_space: Option<String>,
}

impl<'de> Deserialize<'de> for MediaBoundingBox {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum WireBoundingBox {
            Object {
                #[serde(default, alias = "left")]
                x0: f64,
                #[serde(default, alias = "top")]
                y0: f64,
                #[serde(default, alias = "right")]
                x1: f64,
                #[serde(default, alias = "bottom")]
                y1: f64,
                #[serde(default)]
                coordinate_space: Option<String>,
            },
            Coordinates([f64; 4]),
        }

        Ok(match WireBoundingBox::deserialize(deserializer)? {
            WireBoundingBox::Object {
                x0,
                y0,
                x1,
                y1,
                coordinate_space,
            } => Self {
                x0,
                y0,
                x1,
                y1,
                coordinate_space,
            },
            WireBoundingBox::Coordinates([x0, y0, x1, y1]) => Self {
                x0,
                y0,
                x1,
                y1,
                coordinate_space: None,
            },
        })
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct PageEvidence {
    #[serde(default, alias = "id")]
    pub page_id: String,
    #[serde(default)]
    pub citation_index: Option<usize>,
    #[serde(default, alias = "document")]
    pub document_id: Option<String>,
    #[serde(default)]
    pub document_title: Option<String>,
    #[serde(default, alias = "page_number")]
    pub page: u32,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub primary_anchors: Vec<CitationAnchor>,
    #[serde(default)]
    pub context_anchors: Vec<CitationAnchor>,
    #[serde(default)]
    pub preview_url: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct MediaEvidence {
    #[serde(default, alias = "id", alias = "asset_id")]
    pub media_id: String,
    #[serde(default, alias = "media_type", alias = "asset_type")]
    pub kind: String,
    #[serde(default, alias = "document")]
    pub document_id: Option<String>,
    #[serde(default)]
    pub document_title: Option<String>,
    #[serde(default, alias = "page_number")]
    pub page: Option<u32>,
    #[serde(default)]
    pub bbox: Option<MediaBoundingBox>,
    #[serde(default)]
    pub caption: Option<String>,
    #[serde(default)]
    pub caption_origin: Option<String>,
    #[serde(default)]
    pub score: Option<f64>,
    #[serde(default)]
    pub evidence_ids: Vec<String>,
    #[serde(default)]
    pub chunk_ids: Vec<String>,
    #[serde(default, alias = "thumbnail")]
    pub thumbnail_url: Option<String>,
    #[serde(default, alias = "image_url", alias = "url")]
    pub preview_url: Option<String>,
    #[serde(default)]
    pub width: Option<u32>,
    #[serde(default)]
    pub height: Option<u32>,
}

impl MediaEvidence {
    /// A page preview is evidence, but never an extracted figure. Keeping this
    /// gate in the shared domain prevents accidental page-as-image regressions
    /// in every frontend.
    pub fn is_individual_asset(&self) -> bool {
        let kind = self.kind.trim().to_ascii_lowercase().replace('-', "_");
        let Some(bbox) = &self.bbox else {
            return false;
        };
        let nearly_full_page = bbox.coordinate_space.as_deref() == Some("normalized")
            && bbox.x0 <= 0.02
            && bbox.y0 <= 0.02
            && bbox.x1 >= 0.98
            && bbox.y1 >= 0.98;
        !self.media_id.trim().is_empty()
            && matches!(kind.as_str(), "figure" | "diagram" | "table" | "formula")
            && bbox.x1 > bbox.x0
            && bbox.y1 > bbox.y0
            && !nearly_full_page
    }

    pub fn image_url(&self) -> Option<&str> {
        self.thumbnail_url
            .as_deref()
            .or(self.preview_url.as_deref())
            .filter(|url| !url.trim().is_empty())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VisualEvidenceSelection {
    #[serde(default = "default_max_media")]
    pub max_media: usize,
    #[serde(default)]
    pub cut_reason: Option<String>,
}

impl Default for VisualEvidenceSelection {
    fn default() -> Self {
        Self {
            max_media: default_max_media(),
            cut_reason: None,
        }
    }
}

const fn default_max_media() -> usize {
    4
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VisualEvidenceResponse {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    #[serde(default, alias = "page_evidence")]
    pub pages: Vec<PageEvidence>,
    #[serde(default, alias = "media_assets", alias = "figures")]
    pub media: Vec<MediaEvidence>,
    #[serde(default)]
    pub selection: VisualEvidenceSelection,
}

impl Default for VisualEvidenceResponse {
    fn default() -> Self {
        Self {
            schema_version: default_schema_version(),
            pages: Vec::new(),
            media: Vec::new(),
            selection: VisualEvidenceSelection::default(),
        }
    }
}

impl VisualEvidenceResponse {
    pub const MAX_MEDIA: usize = 4;

    /// Applies the frontend safety contract while preserving server ranking.
    pub fn normalized(mut self) -> Self {
        let limit = self.selection.max_media.min(Self::MAX_MEDIA);
        self.media.retain(MediaEvidence::is_individual_asset);
        self.media.truncate(limit);
        self.selection.max_media = limit;
        self
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Citation {
    #[serde(default)]
    pub evidence_id: Option<String>,
    #[serde(default)]
    pub prompt_evidence_id: Option<String>,
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
    #[serde(default)]
    pub excerpt_char_start: Option<u32>,
    #[serde(default)]
    pub excerpt_char_end: Option<u32>,
    #[serde(default)]
    pub chunk_content_hash: Option<String>,
    pub retrieval_rank: Option<u32>,
    pub rerank_score: Option<f64>,
    #[serde(default)]
    pub claim_ids: Vec<String>,
    #[serde(default)]
    pub retrieval_paths: Vec<String>,
    #[serde(default)]
    pub relevance_score: Option<f64>,
    #[serde(default)]
    pub book: Option<BookMetadata>,
    #[serde(default = "default_verification_status")]
    pub verification_status: String,
}

fn default_verification_status() -> String {
    "unverified".into()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AnswerCacheStatus {
    Hit,
    Miss,
    Bypass,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SourceCheck {
    Verified,
    Reviewed,
    Insufficient,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClaimStatus {
    Supported,
    Insufficient,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ClaimSupportKind {
    Literal,
    #[default]
    Semantic,
    Verifier,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClaimSupportSpan {
    pub evidence_id: String,
    pub char_start: u32,
    pub char_end: u32,
    #[serde(default)]
    pub content_hash: Option<String>,
    #[serde(default)]
    pub kind: ClaimSupportKind,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerClaim {
    pub id: String,
    pub text: String,
    #[serde(default)]
    pub evidence_ids: Vec<String>,
    #[serde(default)]
    pub facet_id: Option<String>,
    pub status: ClaimStatus,
    #[serde(default)]
    pub alignment_score: Option<f64>,
    #[serde(default = "default_protocol_checked")]
    pub verification_status: String,
    #[serde(default)]
    pub verification_score: Option<f64>,
    #[serde(default)]
    pub support_spans: Vec<ClaimSupportSpan>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunReceipt {
    pub session_id: String,
    pub turn: u32,
    pub cache_status: AnswerCacheStatus,
    pub total_ms: f64,
    pub source_count: u32,
    pub reused_source_count: u32,
    pub new_source_count: u32,
    pub source_check: SourceCheck,
    #[serde(default)]
    pub phase_timings_ms: BTreeMap<String, f64>,
    #[serde(default = "default_retrieval_mode")]
    pub retrieval_mode: String,
    #[serde(default = "default_rerank_status")]
    pub rerank_status: String,
    #[serde(default = "default_standard")]
    pub complexity: String,
    #[serde(default = "default_retrieval_mode")]
    pub route: String,
    #[serde(default)]
    pub facets: Vec<String>,
    #[serde(default)]
    pub budgets: BTreeMap<String, u32>,
    #[serde(default)]
    pub candidate_count: u32,
    #[serde(default)]
    pub selected_count: u32,
    #[serde(default = "default_legacy")]
    pub cut_reason: String,
    #[serde(default)]
    pub facet_coverage: BTreeMap<String, bool>,
    #[serde(default)]
    pub fallbacks: Vec<String>,
    #[serde(default)]
    pub model_digests: BTreeMap<String, String>,
    #[serde(default)]
    pub prompt_tokens: Option<u32>,
    #[serde(default)]
    pub output_tokens: Option<u32>,
    #[serde(default)]
    pub tokens_per_second: Option<f64>,
    #[serde(default)]
    pub time_to_first_token_ms: Option<f64>,
    #[serde(default = "default_none")]
    pub singleflight_status: String,
    #[serde(default = "default_none")]
    pub abstention: String,
    #[serde(default)]
    pub rejected_claims: u32,
    #[serde(default = "default_stop")]
    pub done_reason: String,
    #[serde(default)]
    pub retrieval_stages: Vec<String>,
    #[serde(default)]
    pub escalation_reasons: Vec<String>,
    #[serde(default)]
    pub calibrator_digest: Option<String>,
    #[serde(default = "default_unknown")]
    pub calibrator_status: String,
    #[serde(default)]
    pub verifier_digest: Option<String>,
    #[serde(default = "default_not_run")]
    pub verifier_status: String,
    #[serde(default = "default_unknown")]
    pub typed_evidence_status: String,
}

fn default_retrieval_mode() -> String {
    "hybrid".into()
}

fn default_rerank_status() -> String {
    "unknown".into()
}

fn default_standard() -> String {
    "standard".into()
}

fn default_legacy() -> String {
    "legacy".into()
}

fn default_none() -> String {
    "none".into()
}

fn default_stop() -> String {
    "stop".into()
}

fn default_protocol_checked() -> String {
    "protocol-checked".into()
}

fn default_not_run() -> String {
    "not-run".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunSnapshot {
    pub id: RunId,
    pub workspace_id: WorkspaceId,
    #[serde(default)]
    pub session_id: String,
    pub status: JobStatus,
    pub question: String,
    pub evidence_mode: EvidenceMode,
    pub answer: String,
    #[serde(default)]
    pub claims: Vec<AnswerClaim>,
    #[serde(default)]
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub receipt: Option<RunReceipt>,
    pub error: Option<Value>,
    pub created_at: String,
    pub updated_at: String,
    pub last_event_id: Option<EventId>,
    #[serde(default)]
    pub pinned: bool,
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
    #[serde(default)]
    pub fingerprint: Option<String>,
    #[serde(default)]
    pub candidate_id: Option<String>,
    #[serde(default)]
    pub metadata: Option<BookMetadata>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MetadataProposal {
    pub field: String,
    pub value: Value,
    pub source: String,
    pub confidence: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ImportCandidate {
    pub id: String,
    pub source: String,
    pub fingerprint: String,
    #[serde(default)]
    pub size_bytes: u64,
    #[serde(default)]
    pub mtime_ns: u64,
    pub metadata: BookMetadata,
    #[serde(default)]
    pub proposals: Vec<MetadataProposal>,
    #[serde(default)]
    pub issues: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ImportPreflightBatch {
    pub id: String,
    pub candidates: Vec<ImportCandidate>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PreflightImportRequest {
    pub sources: Vec<IngestSource>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommitImportRequest {
    pub preflight_id: String,
    pub sources: Vec<IngestSource>,
    pub processing_profile: String,
    pub duplicate_policy: String,
    pub validity_policy: String,
    #[serde(default)]
    pub indexing: IndexingOptions,
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
    #[serde(default)]
    pub indexing: IndexingOptions,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum VisualDenseMode {
    #[default]
    Off,
    On,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexingOptions {
    #[serde(default = "default_book_v3")]
    pub pipeline: String,
    #[serde(default = "default_captions")]
    pub enrichment: String,
    #[serde(default = "default_auto")]
    pub llm_fallback: String,
    #[serde(default)]
    pub visual_dense: VisualDenseMode,
}

impl Default for IndexingOptions {
    fn default() -> Self {
        Self {
            pipeline: default_book_v3(),
            enrichment: default_captions(),
            llm_fallback: default_auto(),
            visual_dense: VisualDenseMode::default(),
        }
    }
}

fn default_book_v3() -> String {
    "book-v3".into()
}

fn default_captions() -> String {
    "captions".into()
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
                    fingerprint: None,
                    candidate_id: None,
                    metadata: None,
                })
                .collect(),
            tags: Vec::new(),
            metadata: BTreeMap::new(),
            parser_id: default_parser_id(),
            processing_profile: "default".into(),
            duplicate_policy: "review".into(),
            validity_policy: "prefer-current".into(),
            indexing: IndexingOptions::default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReindexPreflightRequest {
    #[serde(default = "default_full")]
    pub mode: String,
    #[serde(default)]
    pub indexing: IndexingOptions,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReindexPreflight {
    pub id: String,
    pub workspace_id: WorkspaceId,
    pub mode: String,
    pub ready: bool,
    #[serde(default)]
    pub documents: u32,
    #[serde(default)]
    pub estimated_source_bytes: u64,
    #[serde(default)]
    pub available_bytes: u64,
    #[serde(default)]
    pub checks: BTreeMap<String, Value>,
    #[serde(default)]
    pub issues: Vec<String>,
    pub created_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReindexRequest {
    pub preflight_id: String,
    #[serde(default = "default_full")]
    pub mode: String,
    pub confirm: String,
    #[serde(default)]
    pub indexing: IndexingOptions,
}

fn default_full() -> String {
    "full".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct QueryReadiness {
    pub workspace_id: WorkspaceId,
    pub index_ready: bool,
    pub query_ready: bool,
    pub latency_status: String,
    #[serde(default = "default_required_loaded_models")]
    pub required_loaded_models: u32,
    #[serde(default)]
    pub loaded_models: Vec<Value>,
    #[serde(default)]
    pub model_digests: BTreeMap<String, String>,
    #[serde(default)]
    pub checks: BTreeMap<String, Value>,
}

const fn default_required_loaded_models() -> u32 {
    2
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
    #[error("Backend unavailable: {0}")]
    Transport(String),
    #[error("Backend error {status} ({code}): {message}")]
    Api {
        status: u16,
        code: String,
        message: String,
        retryable: bool,
    },
    #[error("Invalid backend response: {0}")]
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
        let mut request = RunRequest::question("Was ist Beton?", EvidenceMode::Strict)
            .with_session_id("conversation-1");
        request.options.profile = PerformanceProfile::Quality.api_label().into();
        let json = serde_json::to_value(request).unwrap();
        assert_eq!(json["evidence_mode"], "strict");
        assert_eq!(json["mode"], "rag");
        assert_eq!(json["session_id"], "conversation-1");
        assert_eq!(json["options"]["profile"], "quality");
        assert_eq!(json["options"]["verifier"], "auto");
    }

    #[test]
    fn v12_options_use_safe_defaults_and_preserve_legacy_values() {
        let options: RunOptions = serde_json::from_str("{}").unwrap();
        assert_eq!(options.verifier, VerifierMode::Auto);
        assert!(serde_json::from_str::<RunOptions>(r#"{"verifier":"always"}"#).is_err());

        let defaults: IndexingOptions = serde_json::from_str("{}").unwrap();
        assert_eq!(defaults.pipeline, "book-v3");
        assert_eq!(defaults.visual_dense, VisualDenseMode::Off);

        let legacy: IndexingOptions = serde_json::from_value(serde_json::json!({
            "pipeline": "book-v2",
            "enrichment": "captions",
            "llm_fallback": "off"
        }))
        .unwrap();
        assert_eq!(legacy.pipeline, "book-v2");
        assert_eq!(legacy.visual_dense, VisualDenseMode::Off);
    }

    #[test]
    fn v12_answer_evidence_fields_are_legacy_safe() {
        let claim: AnswerClaim = serde_json::from_value(serde_json::json!({
            "id": "C1",
            "text": "Die Betondeckung betraegt 35 mm.",
            "status": "supported"
        }))
        .unwrap();
        assert_eq!(claim.verification_status, "protocol-checked");
        assert_eq!(claim.verification_score, None);
        assert!(claim.support_spans.is_empty());

        let span: ClaimSupportSpan = serde_json::from_value(serde_json::json!({
            "evidence_id": "E1",
            "char_start": 4,
            "char_end": 9,
            "content_hash": "sha256:abc",
            "kind": "literal"
        }))
        .unwrap();
        assert_eq!(span.evidence_id, "E1");
        assert_eq!(span.kind, ClaimSupportKind::Literal);

        let receipt: RunReceipt = serde_json::from_value(serde_json::json!({
            "session_id": "conversation-1",
            "turn": 1,
            "cache_status": "miss",
            "total_ms": 12.0,
            "source_count": 1,
            "reused_source_count": 0,
            "new_source_count": 1,
            "source_check": "verified"
        }))
        .unwrap();
        assert!(receipt.retrieval_stages.is_empty());
        assert!(receipt.escalation_reasons.is_empty());
        assert_eq!(receipt.calibrator_status, "unknown");
        assert_eq!(receipt.verifier_status, "not-run");
        assert_eq!(receipt.typed_evidence_status, "unknown");
    }

    #[test]
    fn visual_evidence_is_legacy_safe_and_never_promotes_pages_to_media() {
        let legacy: VisualEvidenceResponse = serde_json::from_str("{}").unwrap();
        assert!(legacy.pages.is_empty());
        assert!(legacy.media.is_empty());
        assert_eq!(legacy.selection.max_media, 4);

        let response: VisualEvidenceResponse = serde_json::from_value(serde_json::json!({
            "pages": [{"id": "p-7", "page": 7}],
            "media_assets": [
                {"id": "whole-page", "kind": "page_preview", "bbox": [0.1, 0.1, 0.8, 0.8], "preview_url": "/page.png"},
                {"id": "figure-1", "kind": "figure", "bbox": [0.1, 0.1, 0.8, 0.8], "preview_url": "/figure.png"},
                {"id": "figure-2", "kind": "table", "bbox": [0.1, 0.1, 0.8, 0.8], "preview_url": "/table.png"},
                {"id": "figure-3", "kind": "formula", "bbox": [0.1, 0.1, 0.8, 0.8], "preview_url": "/formula.png"},
                {"id": "figure-4", "kind": "image", "bbox": [0.1, 0.1, 0.8, 0.8], "preview_url": "/image.png"},
                {"id": "figure-5", "kind": "diagram", "bbox": [0.1, 0.1, 0.8, 0.8], "preview_url": "/diagram.png"}
            ],
            "selection": {"max_media": 9}
        }))
        .unwrap();
        let normalized = response.normalized();
        assert_eq!(normalized.pages[0].page_id, "p-7");
        assert_eq!(normalized.media.len(), 4);
        assert!(
            normalized
                .media
                .iter()
                .all(MediaEvidence::is_individual_asset)
        );
        assert_eq!(normalized.selection.max_media, 4);
    }

    #[test]
    fn hardware_profile_accepts_old_names_but_rejects_invalid_tiers() {
        let profile: HardwareProfileResponse = serde_json::from_value(serde_json::json!({
            "tier": "6",
            "bottleneck": "VRAM",
            "model_catalog_version": "2026.08",
            "subprofile": "balanced",
            "models": [{"role": "chat", "model_id": "example", "memory_bytes": 42}]
        }))
        .unwrap();
        assert_eq!(profile.tier.level(), 6);
        assert_eq!(profile.profile, PerformanceProfile::Normal);
        assert_eq!(profile.recommendations[0].model, "example");
        assert_eq!(profile.recommendations[0].required_bytes, Some(42));
        assert!(serde_json::from_str::<HardwareProfileResponse>(r#"{"tier":11}"#).is_err());

        let crop: MediaEvidence = serde_json::from_value(serde_json::json!({
            "asset_id": "figure",
            "asset_type": "diagram",
            "page_number": 9,
            "bbox": [1.0, 2.0, 30.0, 40.0],
            "image_url": "/v1/media/figure"
        }))
        .unwrap();
        assert_eq!(crop.bbox.as_ref().unwrap().x1, 30.0);
        assert_eq!(crop.image_url(), Some("/v1/media/figure"));
    }

    #[test]
    fn offline_tiers_are_conservative_and_span_the_catalog() {
        const GIB: u64 = 1_073_741_824;
        assert_eq!(HardwareTier::for_capacity(8 * GIB, 0).level(), 1);
        assert_eq!(HardwareTier::for_capacity(16 * GIB, 4 * GIB).level(), 4);
        assert_eq!(HardwareTier::for_capacity(32 * GIB, 0).level(), 6);
        assert_eq!(HardwareTier::for_capacity(64 * GIB, 16 * GIB).level(), 9);
        assert_eq!(HardwareTier::for_capacity(64 * GIB, 24 * GIB).level(), 10);
    }

    #[test]
    fn recommendation_preflight_is_selection_only() {
        let value = serde_json::to_value(HardwareRecommendationRequest {
            performance_profile: PerformanceProfile::Quality,
            workspace_id: None,
        })
        .unwrap();
        assert_eq!(value, serde_json::json!({"performance_profile": "quality"}));
        assert!(value.get("apply").is_none());
        assert!(value.get("download").is_none());
    }

    #[test]
    fn model_profile_preflight_decodes_the_compact_apply_contract() {
        let preflight: ModelProfilePreflight = serde_json::from_value(serde_json::json!({
            "recommendation": {
                "recommendation_id": "rec-1",
                "catalog_id": "oma-rag-model-catalog",
                "catalog_release": "2026.08",
                "catalog_as_of": "2026-08-01",
                "catalog_stale": false,
                "profile": "normal",
                "classification": {"tier": 5},
                "stack_tier": 5,
                "assignments": [{
                    "role": "chat",
                    "artifact_id": "chat-1",
                    "provider": "ollama",
                    "model": "example:latest",
                    "revision": "r1",
                    "digest": "sha256:abc",
                    "install_state": "not-installed",
                    "download_bytes": 4_294_967_296_u64
                }],
                "context_tokens": 8192,
                "residency_slots": 1,
                "retrieval_budgets": {},
                "estimated_peak_memory": 1,
                "total_download_bytes": 4_294_967_296_u64,
                "ready_now": false,
                "fallback_tiers": [],
                "warnings": []
            },
            "changes": {"chat": "example:latest"},
            "downloads": [{
                "role": "chat",
                "artifact_id": "chat-1",
                "provider": "ollama",
                "model": "example:latest",
                "revision": "r1",
                "digest": "sha256:abc",
                "install_state": "not-installed",
                "download_bytes": 4_294_967_296_u64
            }],
            "requires_reindex": false,
            "requires_visual_reindex": false,
            "can_apply": true,
            "warnings": []
        }))
        .unwrap();

        assert_eq!(preflight.recommendation.stack_tier.level(), 5);
        assert_eq!(preflight.downloads[0].role, CatalogRole::Chat);
        assert_eq!(preflight.recommendation.total_download_bytes, 4_294_967_296);
        assert_eq!(preflight.changes["chat"], "example:latest");
    }

    #[test]
    fn model_profile_apply_consent_is_explicit_and_has_no_reindex_escape_hatch() {
        let without_downloads =
            serde_json::to_value(ModelProfileApplyRequest::new("rec-1", false)).unwrap();
        assert_eq!(without_downloads["confirm"], "APPLY");
        assert!(without_downloads.get("download_consent").is_none());
        assert!(without_downloads.get("reindex_consent").is_none());

        let with_downloads =
            serde_json::to_value(ModelProfileApplyRequest::new("rec-1", true)).unwrap();
        assert_eq!(with_downloads["download_consent"], "DOWNLOAD_MODELS");
        assert!(with_downloads.get("reindex_consent").is_none());
    }
}
