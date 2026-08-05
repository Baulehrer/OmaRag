use omarag_domain::{
    BackendMeta, BackupSummary, BookMetadata, Citation, ConfigDocument, DocumentSummary,
    DomainEvent, EvidenceMode, JobId, JobSnapshot, QualityReport, RetrievalExplanation, RunId,
    RunReceipt, SearchHit, SourceDefinition, WorkspaceId, WorkspaceSummary,
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const THEME_COUNT: usize = 15;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum InteractionLevel {
    #[default]
    Simple,
    Workshop,
}

impl InteractionLevel {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Simple => "Simple",
            Self::Workshop => "Advanced",
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum InputMode {
    #[default]
    Nav,
    Text,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PrimarySection {
    #[default]
    Chat,
    Library,
    Foundry,
    Settings,
}

impl PrimarySection {
    pub const CORE: [Self; 4] = [Self::Chat, Self::Library, Self::Foundry, Self::Settings];

    pub const fn label(self) -> &'static str {
        match self {
            Self::Chat => "Chat",
            Self::Library => "Library",
            Self::Foundry => "Models",
            Self::Settings => "Settings",
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum View {
    #[default]
    Conversation,
    History,
    Retrieval,
    Books,
    Indexing,
    Sources,
    Quality,
    Backups,
    FoundryOverview,
    Models,
    System,
    Activity,
    Settings,
    Themes,
}

impl View {
    pub const ALL: [Self; 13] = [
        Self::Conversation,
        Self::History,
        Self::Retrieval,
        Self::Books,
        Self::Indexing,
        Self::Sources,
        Self::Quality,
        Self::Backups,
        Self::FoundryOverview,
        Self::Models,
        Self::Settings,
        Self::Themes,
        Self::System,
    ];

    pub const fn label(self) -> &'static str {
        match self {
            Self::Conversation => "Conversation",
            Self::History => "History",
            Self::Retrieval => "Retrieval",
            Self::Books => "Books",
            Self::Indexing => "Indexing",
            Self::Sources => "Sources",
            Self::Quality => "Quality",
            Self::Backups => "Backups",
            Self::FoundryOverview => "Presets",
            Self::Models => "Catalog",
            Self::System => "Runtime",
            Self::Activity => "Activity",
            Self::Settings => "General",
            Self::Themes => "Themes",
        }
    }

    pub const fn section(self) -> PrimarySection {
        match self {
            Self::Conversation | Self::History | Self::Retrieval => PrimarySection::Chat,
            Self::Books | Self::Indexing | Self::Sources | Self::Quality | Self::Backups => {
                PrimarySection::Library
            }
            Self::FoundryOverview | Self::Models => PrimarySection::Foundry,
            Self::Activity => PrimarySection::Library,
            Self::Settings | Self::Themes | Self::System => PrimarySection::Settings,
        }
    }

    pub const fn advanced(self) -> bool {
        matches!(
            self,
            Self::Retrieval | Self::Sources | Self::Quality | Self::Backups | Self::System
        )
    }

    pub const fn route(self) -> Route {
        match self {
            Self::Conversation | Self::History => Route::Chat,
            Self::Retrieval => Route::Search,
            Self::Books | Self::Indexing => Route::Library,
            Self::Sources => Route::Sources,
            Self::Quality => Route::Quality,
            Self::Backups => Route::Backups,
            Self::FoundryOverview | Self::Models | Self::System | Self::Themes => Route::System,
            Self::Activity => Route::Jobs,
            Self::Settings => Route::Settings,
        }
    }

    pub const fn from_legacy(route: Route) -> Self {
        match route {
            Route::Chat => Self::Conversation,
            Route::Library => Self::Books,
            Route::Sources => Self::Sources,
            Route::Jobs => Self::Activity,
            Route::Search => Self::Retrieval,
            Route::Quality => Self::Quality,
            Route::Backups => Self::Backups,
            Route::Settings => Self::Settings,
            Route::System => Self::FoundryOverview,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FocusPane {
    Sidebar,
    #[default]
    Workspace,
    Inspector,
}

impl FocusPane {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Sidebar => "Sidebar",
            Self::Workspace => "Workspace",
            Self::Inspector => "Inspector",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Sidebar => Self::Workspace,
            Self::Workspace => Self::Inspector,
            Self::Inspector => Self::Sidebar,
        }
    }

    pub const fn previous(self) -> Self {
        match self {
            Self::Sidebar => Self::Inspector,
            Self::Workspace => Self::Sidebar,
            Self::Inspector => Self::Workspace,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Route {
    #[default]
    Chat,
    Library,
    Sources,
    Jobs,
    Search,
    Quality,
    Backups,
    Settings,
    System,
}

impl Route {
    pub const ALL: [Self; 9] = [
        Self::Chat,
        Self::Library,
        Self::Sources,
        Self::Jobs,
        Self::Search,
        Self::Quality,
        Self::Backups,
        Self::Settings,
        Self::System,
    ];

    pub const fn label(self) -> &'static str {
        match self {
            Self::Chat => "Chat",
            Self::Library => "Library",
            Self::Sources => "Sources",
            Self::Jobs => "Activity",
            Self::Search => "Search",
            Self::Quality => "Quality",
            Self::Backups => "Backups",
            Self::Settings => "Settings",
            Self::System => "System",
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FocusPanel {
    Navigation,
    #[default]
    Chat,
    Import,
    Sources,
    Hardware,
    Models,
    Activity,
}

impl FocusPanel {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Navigation => "Navigation",
            Self::Chat => "Chat",
            Self::Import => "Import",
            Self::Sources => "Library",
            Self::Hardware | Self::Models => "Compute",
            Self::Activity => "Activity",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Navigation => Self::Chat,
            Self::Chat => Self::Import,
            Self::Import => Self::Sources,
            Self::Sources => Self::Models,
            Self::Hardware => Self::Models,
            Self::Models => Self::Activity,
            Self::Activity => Self::Chat,
        }
    }

    pub const fn previous(self) -> Self {
        match self {
            Self::Navigation => Self::Activity,
            Self::Chat => Self::Activity,
            Self::Import => Self::Chat,
            Self::Sources => Self::Import,
            Self::Hardware => Self::Sources,
            Self::Models => Self::Sources,
            Self::Activity => Self::Models,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overlay {
    ConfirmQuit,
    Help,
    Palette,
    Workspaces,
    ConfirmModelDelete,
    FileBrowser,
    ConfirmImport,
    DocumentDetails,
    ConfirmDocumentDelete,
    ConfirmLibraryDelete,
    WorkspaceProfile,
    CustomProfileEditor,
    ChatHistory,
    DocumentTags,
    CustomModel,
    BookScope,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ChatTextSelection {
    pub anchor: usize,
    pub focus: usize,
    pub moved: bool,
}

impl ChatTextSelection {
    pub const fn bounds(self) -> (usize, usize) {
        if self.anchor <= self.focus {
            (self.anchor, self.focus)
        } else {
            (self.focus, self.anchor)
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LibraryFilter {
    #[default]
    All,
    Ready,
    Indexing,
    Failed,
    Duplicates,
}

impl LibraryFilter {
    pub const fn label(self) -> &'static str {
        match self {
            Self::All => "All",
            Self::Ready => "Ready",
            Self::Indexing => "Indexing",
            Self::Failed => "Failed",
            Self::Duplicates => "Duplicates",
        }
    }
    pub const fn next(self) -> Self {
        match self {
            Self::All => Self::Ready,
            Self::Ready => Self::Indexing,
            Self::Indexing => Self::Failed,
            Self::Failed => Self::Duplicates,
            Self::Duplicates => Self::All,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LibrarySort {
    #[default]
    Newest,
    Title,
    Size,
}

impl LibrarySort {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Newest => "Newest",
            Self::Title => "Title",
            Self::Size => "Size",
        }
    }
    pub const fn next(self) -> Self {
        match self {
            Self::Newest => Self::Title,
            Self::Title => Self::Size,
            Self::Size => Self::Newest,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkspaceProfile {
    #[default]
    Technical,
    General,
    ImageHeavy,
    LowMemory,
    Fast,
    Quality,
}

impl WorkspaceProfile {
    pub const ALL: [Self; 6] = [
        Self::Technical,
        Self::General,
        Self::ImageHeavy,
        Self::LowMemory,
        Self::Fast,
        Self::Quality,
    ];
    pub const fn label(self) -> &'static str {
        match self {
            Self::Technical => "Technical",
            Self::General => "General knowledge",
            Self::ImageHeavy => "Image-heavy",
            Self::LowMemory => "Low-memory",
            Self::Fast => "Fast indexing",
            Self::Quality => "High quality",
        }
    }
    pub const fn processing_profile(self) -> &'static str {
        match self {
            Self::LowMemory => "low-memory",
            Self::Fast => "fast",
            Self::Quality => "quality",
            Self::ImageHeavy => "image-heavy",
            Self::Technical => "technical",
            Self::General => "default",
        }
    }

    pub const fn duplicate_policy(self) -> &'static str {
        match self {
            Self::LowMemory | Self::Fast => "skip",
            Self::Quality => "replace",
            _ => "review",
        }
    }

    pub const fn validity_policy(self) -> &'static str {
        match self {
            Self::Quality => "strict",
            _ => "prefer-current",
        }
    }

    pub fn settings(self) -> CustomLibraryProfile {
        CustomLibraryProfile {
            id: format!("builtin-{}", self.processing_profile()),
            name: self.label().into(),
            processing_profile: self.processing_profile().into(),
            duplicate_policy: self.duplicate_policy().into(),
            validity_policy: self.validity_policy().into(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CustomLibraryProfile {
    pub id: String,
    pub name: String,
    pub processing_profile: String,
    pub duplicate_policy: String,
    pub validity_policy: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportPreflight {
    pub busy: bool,
    pub selected: Vec<String>,
    pub pdfs: Vec<String>,
    pub total_bytes: u64,
    pub estimated_index_bytes: u64,
    pub estimated_seconds: u64,
    pub duplicates: Vec<String>,
    pub unreadable: Vec<String>,
    pub encrypted: Vec<String>,
    pub server_preflight_id: Option<String>,
    pub books: Vec<PendingBookReview>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct PendingBookReview {
    pub candidate_id: String,
    pub source: String,
    pub fingerprint: String,
    pub metadata: BookMetadata,
    pub issues: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DocumentInsight {
    pub size_bytes: u64,
    pub pages: Option<u32>,
    pub sha256: Option<String>,
    pub chunks: Option<u64>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ChatSession {
    pub workspace_id: WorkspaceId,
    #[serde(default)]
    pub session_id: String,
    pub question: String,
    pub answer: String,
    pub citations: Vec<Citation>,
    #[serde(default)]
    pub receipt: Option<RunReceipt>,
    #[serde(default)]
    pub scope_document_id: Option<String>,
    #[serde(default = "default_all_books_label")]
    pub scope_title: String,
    pub created_at: String,
}

fn default_all_books_label() -> String {
    "All books".into()
}

#[derive(Debug, Clone, PartialEq)]
pub enum UndoAction {
    RemovedDocument(Box<DocumentSummary>),
    HiddenJob(JobSnapshot),
    CancelledJob(JobSnapshot),
    ProfileChanged {
        workspace: WorkspaceId,
        previous: WorkspaceProfile,
        previous_custom: Option<String>,
    },
    WorkspaceChanged(WorkspaceId),
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ModelSource {
    #[default]
    Installed,
    Ollama,
    HuggingFace,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ModelCategory {
    #[default]
    Chat,
    Vl,
    Embedding,
    Rerank,
}

impl ModelCategory {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Chat => "Chat",
            Self::Vl => "VL",
            Self::Embedding => "Embedding",
            Self::Rerank => "Rerank",
        }
    }

    pub const fn api_label(self) -> &'static str {
        match self {
            Self::Chat => "chat",
            Self::Vl => "vl",
            Self::Embedding => "embedding",
            Self::Rerank => "rerank",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Chat => Self::Vl,
            Self::Vl => Self::Embedding,
            Self::Embedding => Self::Rerank,
            Self::Rerank => Self::Chat,
        }
    }

    pub const fn previous(self) -> Self {
        match self {
            Self::Chat => Self::Rerank,
            Self::Vl => Self::Chat,
            Self::Embedding => Self::Vl,
            Self::Rerank => Self::Embedding,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ModelFit {
    #[default]
    Comfortable,
    Tight,
}

impl ModelFit {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Comfortable => "comfortable",
            Self::Tight => "tight",
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum HardwareProfile {
    Eco,
    #[default]
    Laptop,
    Quality,
}

impl HardwareProfile {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Eco => "Fast · Q3 · 4K",
            Self::Laptop => "Balanced · Q4 · 8K",
            Self::Quality => "Quality · Q5 · 8K",
        }
    }

    pub const fn api_label(self) -> &'static str {
        match self {
            Self::Eco => "eco",
            Self::Laptop => "laptop",
            Self::Quality => "quality",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Eco => Self::Laptop,
            Self::Laptop => Self::Quality,
            Self::Quality => Self::Eco,
        }
    }
}

impl ModelSource {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Installed => "Installed",
            Self::Ollama => "Ollama",
            Self::HuggingFace => "Hugging Face",
        }
    }

    pub const fn api_label(self) -> &'static str {
        match self {
            Self::Installed => "installed",
            Self::Ollama => "ollama",
            Self::HuggingFace => "hugging-face",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Installed => Self::Ollama,
            Self::Ollama => Self::HuggingFace,
            Self::HuggingFace => Self::Installed,
        }
    }

    pub const fn previous(self) -> Self {
        match self {
            Self::Installed => Self::HuggingFace,
            Self::Ollama => Self::Installed,
            Self::HuggingFace => Self::Ollama,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ModelQuantization {
    Q3Km,
    #[default]
    Q4Km,
    Q5Km,
    Q6K,
    Q8,
}

impl ModelQuantization {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Q3Km => "Q3_K_M",
            Self::Q4Km => "Q4_K_M",
            Self::Q5Km => "Q5_K_M",
            Self::Q6K => "Q6_K",
            Self::Q8 => "Q8_0",
        }
    }

    pub const fn ollama_label(self) -> &'static str {
        match self {
            Self::Q3Km => "q3_K_M",
            Self::Q4Km => "q4_K_M",
            Self::Q5Km => "q5_K_M",
            Self::Q6K => "q6_K",
            Self::Q8 => "q8_0",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Q3Km => Self::Q4Km,
            Self::Q4Km => Self::Q5Km,
            Self::Q5Km => Self::Q6K,
            Self::Q6K => Self::Q8,
            Self::Q8 => Self::Q3Km,
        }
    }

    pub const fn previous(self) -> Self {
        match self {
            Self::Q3Km => Self::Q8,
            Self::Q4Km => Self::Q3Km,
            Self::Q5Km => Self::Q4Km,
            Self::Q6K => Self::Q5Km,
            Self::Q8 => Self::Q6K,
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ModelMemoryPolicy {
    Saver,
    #[default]
    Balanced,
    Manual,
}

impl ModelMemoryPolicy {
    pub const fn label(self) -> &'static str {
        match self {
            Self::Saver => "Saver · 30s",
            Self::Balanced => "Balanced · 5m",
            Self::Manual => "Manual · resident",
        }
    }

    pub const fn keep_alive(self) -> &'static str {
        match self {
            Self::Saver => "30s",
            Self::Balanced => "5m",
            Self::Manual => "-1",
        }
    }

    pub const fn next(self) -> Self {
        match self {
            Self::Saver => Self::Balanced,
            Self::Balanced => Self::Manual,
            Self::Manual => Self::Saver,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelCatalogEntry {
    pub id: String,
    pub source: ModelSource,
    pub category: ModelCategory,
    pub description: String,
    pub likes: Option<u64>,
    pub downloads: Option<u64>,
    pub parameter_count: Option<u64>,
    pub estimated_size: Option<u64>,
    pub estimated_memory: u64,
    pub installed: bool,
    pub quantization: Option<String>,
    pub fit: ModelFit,
    pub recommended_rank: Option<u8>,
    #[serde(default)]
    pub capabilities: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelPackageItem {
    pub role: ModelCategory,
    pub model: String,
    pub download_name: String,
    pub source: ModelSource,
    pub installed: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelPackage {
    pub id: String,
    pub name: String,
    pub summary: String,
    pub synergy: String,
    pub recommended_rank: u8,
    pub total_estimated_memory: u64,
    pub fit: ModelFit,
    pub models: Vec<ModelPackageItem>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelCatalogResponse {
    pub entries: Vec<ModelCatalogEntry>,
    #[serde(default)]
    pub packages: Vec<ModelPackage>,
    pub scanned: usize,
    pub compatible: usize,
    pub truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelManagerState {
    pub source: ModelSource,
    pub category: ModelCategory,
    pub profile: HardwareProfile,
    pub query: EditorState,
    pub searching: bool,
    pub entries: Vec<ModelCatalogEntry>,
    pub cursor: usize,
    pub packages: Vec<ModelPackage>,
    pub package_cursor: usize,
    pub center_control_cursor: usize,
    pub center_controls_active: bool,
    pub inspector_cursor: usize,
    pub quantization: ModelQuantization,
    pub context_tokens: u32,
    pub memory_policy: ModelMemoryPolicy,
    pub busy: bool,
    pub transfer_status: String,
    pub transfer_completed: u64,
    pub transfer_total: u64,
    pub scanned: usize,
    pub compatible: usize,
    pub truncated: bool,
    pub delete_candidate: Option<String>,
}

impl Default for ModelManagerState {
    fn default() -> Self {
        Self {
            source: ModelSource::Installed,
            category: ModelCategory::Chat,
            profile: HardwareProfile::Laptop,
            query: EditorState::default(),
            searching: false,
            entries: Vec::new(),
            cursor: 0,
            packages: Vec::new(),
            package_cursor: 0,
            center_control_cursor: 0,
            center_controls_active: false,
            inspector_cursor: 0,
            quantization: ModelQuantization::default(),
            context_tokens: 8_192,
            memory_policy: ModelMemoryPolicy::default(),
            busy: false,
            transfer_status: String::new(),
            transfer_completed: 0,
            transfer_total: 0,
            scanned: 0,
            compatible: 0,
            truncated: false,
            delete_candidate: None,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum ConnectionState {
    #[default]
    Connecting,
    Connected,
    Reconnecting {
        attempt: u32,
    },
    Disconnected {
        reason: String,
    },
}

impl ConnectionState {
    pub const fn label(&self) -> &'static str {
        match self {
            Self::Connecting => "CONNECTING",
            Self::Connected => "CONNECTED",
            Self::Reconnecting { .. } => "RECONNECTING",
            Self::Disconnected { .. } => "OFFLINE",
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct EditorState {
    pub value: String,
    /// Byte offset which is always kept on a UTF-8 character boundary.
    pub cursor: usize,
}

impl EditorState {
    pub fn set(&mut self, value: impl Into<String>) {
        self.value = value.into();
        self.cursor = self.value.len();
    }

    pub fn insert_char(&mut self, character: char) {
        self.value.insert(self.cursor, character);
        self.cursor += character.len_utf8();
    }

    pub fn insert_str(&mut self, text: &str) {
        self.value.insert_str(self.cursor, text);
        self.cursor += text.len();
    }

    pub fn move_left(&mut self) {
        if self.cursor > 0 {
            self.cursor = self.value[..self.cursor]
                .char_indices()
                .next_back()
                .map_or(0, |(index, _)| index);
        }
    }

    pub fn move_right(&mut self) {
        if self.cursor < self.value.len() {
            let width = self.value[self.cursor..]
                .chars()
                .next()
                .map_or(0, char::len_utf8);
            self.cursor += width;
        }
    }

    pub fn home(&mut self) {
        self.cursor = 0;
    }

    pub fn end(&mut self) {
        self.cursor = self.value.len();
    }

    pub fn line_home(&mut self) {
        self.cursor = self.value[..self.cursor]
            .rfind('\n')
            .map_or(0, |index| index + 1);
    }

    pub fn line_end(&mut self) {
        self.cursor = self.value[self.cursor..]
            .find('\n')
            .map_or(self.value.len(), |index| self.cursor + index);
    }

    pub fn move_up(&mut self) {
        let line_start = self.value[..self.cursor]
            .rfind('\n')
            .map_or(0, |index| index + 1);
        if line_start == 0 {
            return;
        }
        let column = self.value[line_start..self.cursor].chars().count();
        let previous_end = line_start - 1;
        let previous_start = self.value[..previous_end]
            .rfind('\n')
            .map_or(0, |index| index + 1);
        self.cursor = byte_at_column(&self.value, previous_start, previous_end, column);
    }

    pub fn move_down(&mut self) {
        let line_start = self.value[..self.cursor]
            .rfind('\n')
            .map_or(0, |index| index + 1);
        let Some(relative_end) = self.value[self.cursor..].find('\n') else {
            return;
        };
        let column = self.value[line_start..self.cursor].chars().count();
        let next_start = self.cursor + relative_end + 1;
        let next_end = self.value[next_start..]
            .find('\n')
            .map_or(self.value.len(), |index| next_start + index);
        self.cursor = byte_at_column(&self.value, next_start, next_end, column);
    }

    pub fn backspace(&mut self) {
        let old = self.cursor;
        self.move_left();
        if self.cursor < old {
            self.value.drain(self.cursor..old);
        }
    }

    pub fn delete(&mut self) {
        if self.cursor < self.value.len() {
            let end = self.cursor
                + self.value[self.cursor..]
                    .chars()
                    .next()
                    .map_or(0, char::len_utf8);
            self.value.drain(self.cursor..end);
        }
    }

    pub fn delete_word(&mut self) {
        while self.cursor > 0
            && self.value[..self.cursor]
                .chars()
                .next_back()
                .is_some_and(char::is_whitespace)
        {
            self.backspace();
        }
        while self.cursor > 0
            && self.value[..self.cursor]
                .chars()
                .next_back()
                .is_some_and(|character| !character.is_whitespace())
        {
            self.backspace();
        }
    }

    pub fn clear_before(&mut self) {
        self.value.drain(..self.cursor);
        self.cursor = 0;
    }

    pub fn clear_after(&mut self) {
        self.value.truncate(self.cursor);
    }
}

fn byte_at_column(value: &str, start: usize, end: usize, column: usize) -> usize {
    value[start..end]
        .char_indices()
        .nth(column)
        .map_or(end, |(offset, _)| start + offset)
}

#[derive(Debug, Clone, PartialEq)]
pub struct ChatState {
    pub question: EditorState,
    /// The question associated with the answer currently shown in the chat.
    /// Kept separately so the composer can be cleared immediately after send.
    pub submitted_question: String,
    pub answer: String,
    pub active_run: Option<RunId>,
    pub last_run: Option<RunId>,
    pub request_pending: bool,
    pub evidence_mode: EvidenceMode,
    pub citations: Vec<Citation>,
    pub receipt: Option<RunReceipt>,
    pub selection: Option<ChatTextSelection>,
    pub error: Option<String>,
    pub phase: String,
    pub phase_label: String,
    pub phase_elapsed_ms: f64,
    pub scope_document_id: Option<String>,
    pub scope_title: String,
    pub scope_cursor: usize,
}

impl Default for ChatState {
    fn default() -> Self {
        Self {
            question: EditorState::default(),
            submitted_question: String::new(),
            answer: String::new(),
            active_run: None,
            last_run: None,
            request_pending: false,
            evidence_mode: EvidenceMode::default(),
            citations: Vec::new(),
            receipt: None,
            selection: None,
            error: None,
            phase: "idle".into(),
            phase_label: String::new(),
            phase_elapsed_ms: 0.0,
            scope_document_id: None,
            scope_title: default_all_books_label(),
            scope_cursor: 0,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SearchState {
    pub query: EditorState,
    pub results: Vec<SearchHit>,
    pub cursor: usize,
    pub loading: bool,
    pub error: Option<String>,
    pub explanation: Option<RetrievalExplanation>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LibraryState {
    pub import_path: EditorState,
    pub path_suggestions: Vec<String>,
    pub path_suggestion_cursor: usize,
    pub import_pending: bool,
    pub last_job_id: Option<JobId>,
    pub error: Option<String>,
    pub query: EditorState,
    pub filtering: bool,
    pub filter: LibraryFilter,
    pub sort: LibrarySort,
    pub preflight: ImportPreflight,
    pub details: BTreeMap<String, DocumentInsight>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FileBrowserEntry {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FileBrowserState {
    pub current_dir: String,
    pub entries: Vec<FileBrowserEntry>,
    pub cursor: usize,
    pub selected: Vec<String>,
    pub error: Option<String>,
    pub history: Vec<String>,
    pub favorites: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PaletteState {
    pub query: EditorState,
    pub cursor: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Notification {
    pub level: NotificationLevel,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotificationLevel {
    Info,
    Warning,
    Error,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OperationState {
    pub label: String,
    pub active: bool,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AppState {
    pub view: View,
    pub focus_pane: FocusPane,
    pub inspector_scroll: u16,
    pub route: Route,
    pub interaction_level: InteractionLevel,
    pub input_mode: InputMode,
    pub focus: FocusPanel,
    pub overlay: Option<Overlay>,
    pub connection: ConnectionState,
    pub backend: Option<BackendMeta>,
    pub workspaces: Vec<WorkspaceSummary>,
    pub active_workspace: Option<WorkspaceId>,
    pub workspace_cursor: usize,
    pub workspace_name: EditorState,
    pub creating_workspace: bool,
    pub route_cursor: usize,
    pub nav_cursor: usize,
    pub chat_scroll: u16,
    pub asset_cursor: usize,
    pub hardware_cursor: usize,
    pub model_cursor: usize,
    pub model_manager: ModelManagerState,
    pub custom_model_input: EditorState,
    pub custom_model_file: bool,
    pub theme_index: usize,
    pub theme_cursor: usize,
    pub theme_preview_origin: Option<usize>,
    pub jobs: BTreeMap<JobId, JobSnapshot>,
    pub documents: Vec<DocumentSummary>,
    pub document_cursor: usize,
    pub sources: Vec<SourceDefinition>,
    pub source_cursor: usize,
    pub source_location: EditorState,
    pub quality: Option<QualityReport>,
    pub backups: Vec<BackupSummary>,
    pub backup_cursor: usize,
    pub config: Option<ConfigDocument>,
    pub config_editor: EditorState,
    pub config_dirty: bool,
    pub job_cursor: usize,
    pub citation_cursor: usize,
    pub citation_page_cursor: usize,
    pub gallery_cursor: usize,
    pub chat: ChatState,
    pub search: SearchState,
    pub library: LibraryState,
    pub file_browser: FileBrowserState,
    pub palette: PaletteState,
    pub workspace_profiles: BTreeMap<WorkspaceId, WorkspaceProfile>,
    pub workspace_custom_profiles: BTreeMap<WorkspaceId, String>,
    pub custom_profiles: Vec<CustomLibraryProfile>,
    pub profile_cursor: usize,
    pub custom_profile_draft: CustomLibraryProfile,
    pub custom_profile_name: EditorState,
    pub custom_profile_field: usize,
    pub editing_custom_profile: Option<usize>,
    pub chat_sessions: BTreeMap<WorkspaceId, Vec<ChatSession>>,
    pub conversation_ids: BTreeMap<WorkspaceId, String>,
    pub bold_term_explanations_disabled: bool,
    pub document_tags: BTreeMap<String, Vec<String>>,
    pub tag_editor: EditorState,
    pub history_cursor: usize,
    pub hidden_jobs: BTreeSet<JobId>,
    pub undo: Option<UndoAction>,
    pub operation: OperationState,
    pub notifications: Vec<Notification>,
    pub last_event_id: Option<u64>,
    pub quit_requested: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct UiPreferences {
    pub version: u8,
    pub view: View,
    pub focus_pane: FocusPane,
    pub theme_index: usize,
    pub active_workspace: Option<WorkspaceId>,
    pub focus: FocusPanel,
    pub route: Route,
    pub interaction_level: InteractionLevel,
    pub last_directory: String,
    pub import_history: Vec<String>,
    pub favorite_directories: Vec<String>,
    pub library_filter: LibraryFilter,
    pub library_sort: LibrarySort,
    pub library_query: String,
    pub workspace_profiles: BTreeMap<WorkspaceId, WorkspaceProfile>,
    pub workspace_custom_profiles: BTreeMap<WorkspaceId, String>,
    pub custom_profiles: Vec<CustomLibraryProfile>,
    pub chat_sessions: BTreeMap<WorkspaceId, Vec<ChatSession>>,
    pub conversation_ids: BTreeMap<WorkspaceId, String>,
    pub bold_term_explanations_disabled: bool,
    pub document_tags: BTreeMap<String, Vec<String>>,
}

impl AppState {
    pub fn apply_preferences(&mut self, preferences: UiPreferences) {
        self.theme_index = preferences.theme_index % THEME_COUNT;
        self.theme_cursor = self.theme_index;
        self.active_workspace = preferences.active_workspace;
        if preferences.version >= 2 {
            self.view = match preferences.view {
                View::Activity => View::Indexing,
                view => view,
            };
            self.focus_pane = preferences.focus_pane;
            if self.focus_pane == FocusPane::Inspector
                && !matches!(self.view, View::Conversation | View::Retrieval)
            {
                self.focus_pane = FocusPane::Workspace;
            }
            self.sync_legacy_navigation();
        } else {
            self.route = preferences.route;
            self.focus = preferences.focus;
            self.view = View::from_legacy(preferences.route);
            self.focus_pane = if preferences.focus == FocusPanel::Navigation {
                FocusPane::Sidebar
            } else {
                FocusPane::Workspace
            };
        }
        self.interaction_level = preferences.interaction_level;
        self.file_browser.current_dir = preferences.last_directory;
        self.file_browser.history = preferences.import_history;
        self.file_browser.favorites = preferences.favorite_directories;
        self.library.filter = preferences.library_filter;
        self.library.sort = preferences.library_sort;
        self.library.query.set(preferences.library_query);
        self.workspace_profiles = preferences.workspace_profiles;
        self.workspace_custom_profiles = preferences.workspace_custom_profiles;
        self.custom_profiles = preferences.custom_profiles;
        self.chat_sessions = preferences.chat_sessions;
        self.conversation_ids = preferences.conversation_ids;
        self.bold_term_explanations_disabled = preferences.bold_term_explanations_disabled;
        self.document_tags = preferences.document_tags;
    }

    pub fn preferences(&self) -> UiPreferences {
        UiPreferences {
            version: 5,
            view: self.view,
            focus_pane: self.focus_pane,
            theme_index: self.theme_index,
            active_workspace: self.active_workspace.clone(),
            focus: self.focus,
            route: self.route,
            interaction_level: self.interaction_level,
            last_directory: self.file_browser.current_dir.clone(),
            import_history: self.file_browser.history.clone(),
            favorite_directories: self.file_browser.favorites.clone(),
            library_filter: self.library.filter,
            library_sort: self.library.sort,
            library_query: self.library.query.value.clone(),
            workspace_profiles: self.workspace_profiles.clone(),
            workspace_custom_profiles: self.workspace_custom_profiles.clone(),
            custom_profiles: self.custom_profiles.clone(),
            chat_sessions: self.chat_sessions.clone(),
            conversation_ids: self.conversation_ids.clone(),
            bold_term_explanations_disabled: self.bold_term_explanations_disabled,
            document_tags: self.document_tags.clone(),
        }
    }

    pub fn navigate_view(&mut self, view: View) {
        if self.view == View::Themes && view != View::Themes {
            if let Some(origin) = self.theme_preview_origin.take() {
                self.theme_index = origin;
                self.theme_cursor = origin;
            }
        } else if self.view != View::Themes && view == View::Themes {
            self.theme_cursor = self.theme_index;
            self.theme_preview_origin = Some(self.theme_index);
        }
        self.view = view;
        if self.focus_pane == FocusPane::Inspector
            && !matches!(view, View::Conversation | View::Retrieval)
        {
            self.focus_pane = FocusPane::Workspace;
        }
        self.route = view.route();
        self.route_cursor = View::ALL
            .iter()
            .position(|item| *item == view)
            .unwrap_or_default();
        self.sync_legacy_navigation();
        self.input_mode = InputMode::Nav;
        self.inspector_scroll = 0;
    }

    pub fn set_focus_pane(&mut self, pane: FocusPane) {
        self.focus_pane = pane;
        self.sync_legacy_navigation();
    }

    fn sync_legacy_navigation(&mut self) {
        self.route = self.view.route();
        self.focus = match self.focus_pane {
            FocusPane::Sidebar => FocusPanel::Navigation,
            FocusPane::Workspace | FocusPane::Inspector => match self.view {
                View::Conversation | View::History => FocusPanel::Chat,
                View::Books | View::Indexing | View::Sources | View::Retrieval => {
                    FocusPanel::Sources
                }
                View::Activity | View::Backups => FocusPanel::Activity,
                View::FoundryOverview
                | View::Models
                | View::System
                | View::Quality
                | View::Settings
                | View::Themes => FocusPanel::Models,
            },
        };
    }

    pub fn active_profile(&self) -> WorkspaceProfile {
        self.active_workspace
            .as_ref()
            .and_then(|workspace| self.workspace_profiles.get(workspace).copied())
            .unwrap_or_default()
    }

    pub fn profile_count(&self) -> usize {
        WorkspaceProfile::ALL.len() + self.custom_profiles.len()
    }

    pub fn profile_settings_at(&self, index: usize) -> CustomLibraryProfile {
        if let Some(profile) = WorkspaceProfile::ALL.get(index) {
            return profile.settings();
        }
        self.custom_profiles
            .get(index.saturating_sub(WorkspaceProfile::ALL.len()))
            .cloned()
            .unwrap_or_else(|| WorkspaceProfile::Technical.settings())
    }

    pub fn active_profile_settings(&self) -> CustomLibraryProfile {
        if let Some(custom_id) = self
            .active_workspace
            .as_ref()
            .and_then(|library| self.workspace_custom_profiles.get(library))
            && let Some(profile) = self
                .custom_profiles
                .iter()
                .find(|item| &item.id == custom_id)
        {
            return profile.clone();
        }
        self.active_profile().settings()
    }

    pub fn active_profile_index(&self) -> usize {
        if let Some(custom_id) = self
            .active_workspace
            .as_ref()
            .and_then(|library| self.workspace_custom_profiles.get(library))
            && let Some(index) = self
                .custom_profiles
                .iter()
                .position(|profile| &profile.id == custom_id)
        {
            return WorkspaceProfile::ALL.len() + index;
        }
        WorkspaceProfile::ALL
            .iter()
            .position(|profile| *profile == self.active_profile())
            .unwrap_or_default()
    }

    pub fn assign_profile_at(&mut self, library: WorkspaceId, index: usize) {
        if let Some(profile) = WorkspaceProfile::ALL.get(index).copied() {
            self.workspace_profiles.insert(library.clone(), profile);
            self.workspace_custom_profiles.remove(&library);
        } else if let Some(profile) = self
            .custom_profiles
            .get(index.saturating_sub(WorkspaceProfile::ALL.len()))
        {
            self.workspace_custom_profiles
                .insert(library, profile.id.clone());
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    QuitRequested,
    Navigate(Route),
    NavigateView(View),
    NavigateNext,
    NavigatePrevious,
    SetInteractionLevel(InteractionLevel),
    ToggleInteractionLevel,
    CycleTheme,
    SetInputMode(InputMode),
    SetFocus(FocusPanel),
    SetFocusPane(FocusPane),
    FocusPaneNext,
    FocusPanePrevious,
    FocusNext,
    FocusPrevious,
    OpenOverlay(Overlay),
    CloseOverlay,
    BackendConnected(BackendMeta),
    BackendDisconnected(String),
    WorkspacesLoaded(Vec<WorkspaceSummary>),
    SelectNextWorkspace,
    SelectPreviousWorkspace,
    WorkspaceOpenStarted,
    WorkspaceOpened(WorkspaceId),
    JobsLoaded(Vec<JobSnapshot>),
    WorkspaceFeaturesLoaded {
        documents: Vec<DocumentSummary>,
        sources: Vec<SourceDefinition>,
        quality: QualityReport,
        backups: Vec<BackupSummary>,
        config: ConfigDocument,
    },
    SourceCreated(SourceDefinition),
    ConfigSaved(ConfigDocument),
    SelectNextJob,
    SelectPreviousJob,
    SelectNextMainItem,
    SelectPreviousMainItem,
    SelectNextInspectorItem,
    SelectPreviousInspectorItem,
    RunRequestStarted,
    RunStarted(RunId),
    RunCancelled,
    RunFailed(String),
    SearchStarted,
    SearchCompleted(Vec<SearchHit>),
    SearchFailed(String),
    SelectNextSearchHit,
    SelectPreviousSearchHit,
    ImportStarted,
    ImportAccepted(JobId),
    ImportFailed(String),
    OperationStarted(String),
    OperationFinished,
    EventReceived(DomainEvent),
    Notify(Notification),
    NotificationDismissed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    LoadWorkspaces,
    OpenWorkspace(WorkspaceId),
    LoadJobs(Option<WorkspaceId>),
    StartRun {
        workspace: WorkspaceId,
        question: String,
        evidence_mode: EvidenceMode,
    },
    SubscribeEvents {
        workspace: Option<WorkspaceId>,
        last_event_id: Option<u64>,
    },
}

pub fn update(state: &mut AppState, action: Action) -> Vec<Effect> {
    match action {
        Action::QuitRequested => state.quit_requested = true,
        Action::Navigate(route) => {
            state.navigate_view(View::from_legacy(route));
        }
        Action::NavigateView(view) => state.navigate_view(view),
        Action::NavigateNext => {
            let views = View::ALL
                .iter()
                .copied()
                .filter(|view| {
                    state.interaction_level == InteractionLevel::Workshop || !view.advanced()
                })
                .collect::<Vec<_>>();
            let index = views
                .iter()
                .position(|view| *view == state.view)
                .unwrap_or(0);
            update(
                state,
                Action::NavigateView(views[(index + 1) % views.len()]),
            );
        }
        Action::NavigatePrevious => {
            let views = View::ALL
                .iter()
                .copied()
                .filter(|view| {
                    state.interaction_level == InteractionLevel::Workshop || !view.advanced()
                })
                .collect::<Vec<_>>();
            let index = views
                .iter()
                .position(|view| *view == state.view)
                .unwrap_or(0);
            update(
                state,
                Action::NavigateView(views[(index + views.len() - 1) % views.len()]),
            );
        }
        Action::SetInteractionLevel(level) => state.interaction_level = level,
        Action::ToggleInteractionLevel => {
            state.interaction_level = match state.interaction_level {
                InteractionLevel::Simple => InteractionLevel::Workshop,
                InteractionLevel::Workshop => InteractionLevel::Simple,
            }
        }
        Action::CycleTheme => {
            state.theme_index = (state.theme_index + 1) % THEME_COUNT;
            state.theme_cursor = state.theme_index;
        }
        Action::SetInputMode(mode) => state.input_mode = mode,
        Action::SetFocus(focus) => {
            state.focus = focus;
            state.focus_pane = if focus == FocusPanel::Navigation {
                FocusPane::Sidebar
            } else {
                FocusPane::Workspace
            };
        }
        Action::SetFocusPane(pane) => state.set_focus_pane(pane),
        Action::FocusPaneNext => {
            state.set_focus_pane(next_focus_pane(state.view, state.focus_pane, false))
        }
        Action::FocusPanePrevious => {
            state.set_focus_pane(next_focus_pane(state.view, state.focus_pane, true))
        }
        Action::FocusNext => {
            state.focus = state.focus.next();
            state.focus_pane = if state.focus == FocusPanel::Navigation {
                FocusPane::Sidebar
            } else {
                FocusPane::Workspace
            };
        }
        Action::FocusPrevious => {
            state.focus = state.focus.previous();
            state.focus_pane = if state.focus == FocusPanel::Navigation {
                FocusPane::Sidebar
            } else {
                FocusPane::Workspace
            };
        }
        Action::OpenOverlay(overlay) => state.overlay = Some(overlay),
        Action::CloseOverlay => {
            state.overlay = None;
            state.palette.query = EditorState::default();
            state.palette.cursor = 0;
        }
        Action::BackendConnected(meta) => {
            state.backend = Some(meta);
            state.connection = ConnectionState::Connected;
            return vec![Effect::LoadWorkspaces];
        }
        Action::BackendDisconnected(reason) => {
            state.connection = ConnectionState::Disconnected { reason };
            state.operation.active = false;
        }
        Action::WorkspacesLoaded(workspaces) => {
            state.workspaces = workspaces;
            if state.active_workspace.as_ref().is_some_and(|active| {
                !state
                    .workspaces
                    .iter()
                    .any(|workspace| &workspace.id == active)
            }) {
                state.active_workspace = None;
            }
            state.workspace_cursor = state
                .workspace_cursor
                .min(state.workspaces.len().saturating_sub(1));
            if state.active_workspace.is_none()
                && let Some(first) = state.workspaces.first()
            {
                state.active_workspace = Some(first.id.clone());
                return vec![
                    Effect::OpenWorkspace(first.id.clone()),
                    Effect::LoadJobs(Some(first.id.clone())),
                    Effect::SubscribeEvents {
                        workspace: Some(first.id.clone()),
                        last_event_id: state.last_event_id,
                    },
                ];
            }
        }
        Action::SelectNextWorkspace => {
            if !state.workspaces.is_empty() {
                state.workspace_cursor = (state.workspace_cursor + 1) % state.workspaces.len();
            }
        }
        Action::SelectPreviousWorkspace => {
            if !state.workspaces.is_empty() {
                state.workspace_cursor =
                    (state.workspace_cursor + state.workspaces.len() - 1) % state.workspaces.len();
            }
        }
        Action::WorkspaceOpenStarted => {
            state.operation = OperationState {
                label: "Opening library".into(),
                active: true,
            };
        }
        Action::WorkspaceOpened(id) => {
            state.active_workspace = Some(id.clone());
            state.operation.active = false;
            state.overlay = None;
            state.chat = ChatState::default();
            state.search = SearchState::default();
            state.documents.clear();
            state.document_cursor = 0;
            state.sources.clear();
            state.source_cursor = 0;
            state.source_location = EditorState::default();
            state.quality = None;
            state.backups.clear();
            state.backup_cursor = 0;
            state.config = None;
            state.config_editor = EditorState::default();
            state.config_dirty = false;
            state.citation_cursor = 0;
            state.creating_workspace = false;
            state.workspace_name = EditorState::default();
            return vec![
                Effect::LoadJobs(Some(id.clone())),
                Effect::SubscribeEvents {
                    workspace: Some(id),
                    last_event_id: state.last_event_id,
                },
            ];
        }
        Action::JobsLoaded(jobs) => {
            state.jobs = jobs.into_iter().map(|job| (job.id.clone(), job)).collect();
            state.job_cursor = state.job_cursor.min(state.jobs.len().saturating_sub(1));
        }
        Action::WorkspaceFeaturesLoaded {
            documents,
            sources,
            quality,
            backups,
            config,
        } => {
            state.documents = documents;
            state.document_cursor = state
                .document_cursor
                .min(state.documents.len().saturating_sub(1));
            state.sources = sources;
            state.source_cursor = state
                .source_cursor
                .min(state.sources.len().saturating_sub(1));
            state.quality = Some(quality);
            state.backups = backups;
            state.backup_cursor = state
                .backup_cursor
                .min(state.backups.len().saturating_sub(1));
            if !state.config_dirty {
                state.config_editor.set(config.content.clone());
                state.config = Some(config);
            }
        }
        Action::SourceCreated(source) => {
            state.sources.push(source);
            state.source_location = EditorState::default();
            state.operation.active = false;
            state.input_mode = InputMode::Nav;
        }
        Action::ConfigSaved(config) => {
            state.config_editor.set(config.content.clone());
            state.config = Some(config);
            state.config_dirty = false;
            state.operation.active = false;
            state.input_mode = InputMode::Nav;
        }
        Action::SelectNextJob => {
            if !state.jobs.is_empty() {
                state.job_cursor = (state.job_cursor + 1) % state.jobs.len();
            }
        }
        Action::SelectPreviousJob => {
            if !state.jobs.is_empty() {
                state.job_cursor = (state.job_cursor + state.jobs.len() - 1) % state.jobs.len();
            }
        }
        Action::SelectNextMainItem => select_main_item(state, true),
        Action::SelectPreviousMainItem => select_main_item(state, false),
        Action::SelectNextInspectorItem => select_inspector_item(state, true),
        Action::SelectPreviousInspectorItem => select_inspector_item(state, false),
        Action::RunRequestStarted => {
            state.chat.request_pending = true;
            state.chat.error = None;
            state.chat.answer.clear();
            state.chat.citations.clear();
            state.chat.receipt = None;
            state.chat.selection = None;
            state.chat.last_run = None;
            state.chat.phase = "waiting".into();
            state.chat.phase_label = "Waiting".into();
            state.chat.phase_elapsed_ms = 0.0;
            state.citation_cursor = 0;
            state.operation = OperationState {
                label: "Preparing answer".into(),
                active: true,
            };
        }
        Action::RunStarted(run_id) => {
            state.chat.request_pending = false;
            state.chat.active_run = Some(run_id.clone());
            state.chat.last_run = Some(run_id);
            state.input_mode = InputMode::Nav;
            state.operation = OperationState {
                label: "Searching library".into(),
                active: true,
            };
        }
        Action::RunCancelled => {
            state.chat.active_run = None;
            state.chat.request_pending = false;
            state.operation.active = false;
        }
        Action::RunFailed(message) => {
            state.chat.active_run = None;
            state.chat.request_pending = false;
            state.chat.error = Some(message);
            state.operation.active = false;
        }
        Action::SearchStarted => {
            state.search.loading = true;
            state.search.error = None;
            state.search.results.clear();
            state.search.explanation = None;
            state.operation = OperationState {
                label: "Searching".into(),
                active: true,
            };
        }
        Action::SearchCompleted(results) => {
            state.search.results = results;
            state.search.cursor = 0;
            state.search.loading = false;
            state.operation.active = false;
            state.input_mode = InputMode::Nav;
        }
        Action::SearchFailed(message) => {
            state.search.loading = false;
            state.search.error = Some(message);
            state.operation.active = false;
        }
        Action::SelectNextSearchHit => {
            if !state.search.results.is_empty() {
                state.search.cursor = (state.search.cursor + 1) % state.search.results.len();
            }
        }
        Action::SelectPreviousSearchHit => {
            if !state.search.results.is_empty() {
                state.search.cursor = (state.search.cursor + state.search.results.len() - 1)
                    % state.search.results.len();
            }
        }
        Action::ImportStarted => {
            state.library.import_pending = true;
            state.library.error = None;
            state.operation = OperationState {
                label: "Queuing import".into(),
                active: true,
            };
        }
        Action::ImportAccepted(job_id) => {
            state.library.import_pending = false;
            state.library.last_job_id = Some(job_id);
            state.library.import_path = EditorState::default();
            state.file_browser.selected.clear();
            state.operation.active = false;
            state.input_mode = InputMode::Nav;
        }
        Action::ImportFailed(message) => {
            state.library.import_pending = false;
            state.library.error = Some(message);
            state.operation.active = false;
        }
        Action::OperationStarted(label) => {
            state.operation = OperationState {
                label,
                active: true,
            };
        }
        Action::OperationFinished => state.operation.active = false,
        Action::EventReceived(event) => apply_event(state, event),
        Action::Notify(notification) => state.notifications.push(notification),
        Action::NotificationDismissed => {
            if !state.notifications.is_empty() {
                state.notifications.remove(0);
            }
        }
    }
    Vec::new()
}

fn next_focus_pane(view: View, current: FocusPane, reverse: bool) -> FocusPane {
    let panes: &[FocusPane] = if matches!(view, View::Conversation | View::Retrieval) {
        &[
            FocusPane::Sidebar,
            FocusPane::Workspace,
            FocusPane::Inspector,
        ]
    } else {
        &[FocusPane::Sidebar, FocusPane::Workspace]
    };
    let index = panes.iter().position(|pane| *pane == current).unwrap_or(0);
    if reverse {
        panes[(index + panes.len() - 1) % panes.len()]
    } else {
        panes[(index + 1) % panes.len()]
    }
}

fn select_main_item(state: &mut AppState, next: bool) {
    match state.route {
        Route::Library => move_cursor(&mut state.document_cursor, state.documents.len(), next),
        Route::Sources => move_cursor(&mut state.source_cursor, state.sources.len(), next),
        Route::Jobs => move_cursor(&mut state.job_cursor, state.jobs.len(), next),
        Route::Search => move_cursor(&mut state.search.cursor, state.search.results.len(), next),
        Route::Backups => move_cursor(&mut state.backup_cursor, state.backups.len(), next),
        _ => {}
    }
}

fn select_inspector_item(state: &mut AppState, next: bool) {
    match state.route {
        Route::Chat => move_cursor(&mut state.citation_cursor, state.chat.citations.len(), next),
        Route::Search => move_cursor(&mut state.search.cursor, state.search.results.len(), next),
        _ => {}
    }
}

fn move_cursor(cursor: &mut usize, len: usize, next: bool) {
    if len == 0 {
        *cursor = 0;
    } else if next {
        *cursor = (*cursor + 1) % len;
    } else {
        *cursor = (*cursor + len - 1) % len;
    }
}

fn apply_event(state: &mut AppState, event: DomainEvent) {
    if state
        .last_event_id
        .is_some_and(|last| event.event_id <= last)
    {
        return;
    }
    state.last_event_id = Some(event.event_id);
    let is_run_event = matches!(
        event.event_type.as_str(),
        "assistant.started"
            | "run.phase"
            | "assistant.delta"
            | "citation.added"
            | "run.completed"
            | "run.cancelled"
            | "run.failed"
    );
    if is_run_event && event.run_id.as_ref() != state.chat.active_run.as_ref() {
        return;
    }
    match event.event_type.as_str() {
        "run.phase" => {
            state.chat.phase = event
                .payload
                .get("phase")
                .and_then(|value| value.as_str())
                .unwrap_or("working")
                .to_owned();
            state.chat.phase_label = event
                .payload
                .get("label")
                .and_then(|value| value.as_str())
                .unwrap_or("Working")
                .to_owned();
            state.chat.phase_elapsed_ms = event
                .payload
                .get("elapsed_ms")
                .and_then(serde_json::Value::as_f64)
                .unwrap_or_default();
            state.operation.label.clone_from(&state.chat.phase_label);
        }
        "assistant.delta" => {
            if let Some(delta) = event.payload.get("delta").and_then(|value| value.as_str()) {
                state.chat.answer.push_str(delta);
            }
        }
        "citation.added" => {
            if let Ok(citation) = serde_json::from_value::<Citation>(event.payload.clone()) {
                state.chat.citations.push(citation);
            }
        }
        "run.completed" => {
            state.chat.receipt = event
                .payload
                .get("receipt")
                .cloned()
                .and_then(|value| serde_json::from_value(value).ok());
            state.chat.active_run = None;
            state.chat.phase = "completed".into();
            state.operation.active = false;
        }
        "run.cancelled" => {
            state.chat.active_run = None;
            state.operation.active = false;
        }
        "run.failed" => {
            state.chat.active_run = None;
            state.operation.active = false;
            let message = event
                .payload
                .get("error")
                .and_then(|value| value.get("message"))
                .and_then(|value| value.as_str())
                .unwrap_or("The answer could not be created.");
            state.chat.error = Some(message.to_owned());
        }
        _ => {}
    }
}

pub fn submit_question(state: &AppState) -> Option<Effect> {
    let workspace = state.active_workspace.clone()?;
    let question = state.chat.question.value.trim();
    if question.is_empty() || state.chat.active_run.is_some() || state.chat.request_pending {
        return None;
    }
    Some(Effect::StartRun {
        workspace,
        question: question.to_owned(),
        evidence_mode: state.chat.evidence_mode,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use omarag_domain::CapabilitySet;
    use serde_json::json;

    fn backend() -> BackendMeta {
        BackendMeta {
            api_version: "1.0".into(),
            min_client_version: "1.0".into(),
            max_client_version: "1.x".into(),
            omarag_version: "1.0.0".into(),
            haiku_version: None,
            adapter: None,
            backend_id: "local".into(),
            capabilities: CapabilitySet::default(),
            deprecations: Vec::new(),
        }
    }

    #[test]
    fn editor_handles_unicode_at_the_cursor() {
        let mut editor = EditorState::default();
        editor.insert_str("Hllo");
        editor.home();
        editor.move_right();
        editor.insert_char('ä');
        assert_eq!(editor.value, "Hällo");
        editor.backspace();
        assert_eq!(editor.value, "Hllo");
        editor.delete();
        assert_eq!(editor.value, "Hlo");
    }

    #[test]
    fn editor_moves_vertically_across_utf8_lines() {
        let mut editor = EditorState::default();
        editor.set("äbc\nx\n12345");
        editor.move_up();
        assert_eq!(&editor.value[..editor.cursor], "äbc\nx");
        editor.move_up();
        assert_eq!(&editor.value[..editor.cursor], "ä");
        editor.move_down();
        assert_eq!(&editor.value[..editor.cursor], "äbc\nx");
        editor.move_down();
        assert_eq!(&editor.value[..editor.cursor], "äbc\nx\n1");
    }

    #[test]
    fn reducer_emits_load_after_connect() {
        let mut state = AppState::default();
        let effects = update(&mut state, Action::BackendConnected(backend()));
        assert_eq!(state.connection, ConnectionState::Connected);
        assert_eq!(effects, vec![Effect::LoadWorkspaces]);
    }

    #[test]
    fn duplicate_events_do_not_duplicate_answer() {
        let mut state = AppState::default();
        state.chat.active_run = Some("run-1".into());
        let event = DomainEvent {
            event_id: 9,
            sequence: 1,
            timestamp: "now".into(),
            event_type: "assistant.delta".into(),
            workspace_id: None,
            job_id: None,
            run_id: Some("run-1".into()),
            correlation_id: "c".into(),
            schema_version: 1,
            payload: json!({"delta": "Hallo"}),
        };
        update(&mut state, Action::EventReceived(event.clone()));
        update(&mut state, Action::EventReceived(event));
        assert_eq!(state.chat.answer, "Hallo");
    }

    #[test]
    fn completed_run_keeps_the_plain_language_receipt() {
        let mut state = AppState::default();
        state.chat.active_run = Some("run-1".into());
        update(
            &mut state,
            Action::EventReceived(DomainEvent {
                event_id: 10,
                sequence: 1,
                timestamp: "now".into(),
                event_type: "run.completed".into(),
                workspace_id: Some("workspace-1".into()),
                job_id: None,
                run_id: Some("run-1".into()),
                correlation_id: "run-1".into(),
                schema_version: 1,
                payload: json!({
                    "receipt": {
                        "session_id": "conversation-1",
                        "turn": 2,
                        "cache_status": "hit",
                        "total_ms": 12.0,
                        "source_count": 1,
                        "reused_source_count": 1,
                        "new_source_count": 0,
                        "source_check": "verified"
                    }
                }),
            }),
        );
        let receipt = state.chat.receipt.as_ref().unwrap();
        assert_eq!(receipt.turn, 2);
        assert_eq!(receipt.cache_status, omarag_domain::AnswerCacheStatus::Hit);
    }

    #[test]
    fn focus_cycles_without_changing_interaction_level() {
        let mut state = AppState::default();
        update(&mut state, Action::FocusNext);
        assert_eq!(state.focus, FocusPanel::Import);
        assert_eq!(state.interaction_level, InteractionLevel::Simple);
    }

    #[test]
    fn legacy_preferences_migrate_to_the_new_view_model() {
        let preferences: UiPreferences =
            serde_json::from_str(r#"{"route":"quality","focus":"models"}"#).unwrap();
        let mut state = AppState::default();
        state.apply_preferences(preferences);
        assert_eq!(state.view, View::Quality);
        assert_eq!(state.focus_pane, FocusPane::Workspace);
        assert_eq!(state.route, Route::Quality);
    }

    #[test]
    fn current_preferences_restore_view_and_enforce_read_only_inspector_focus() {
        let mut state = AppState::default();
        state.navigate_view(View::Models);
        state.set_focus_pane(FocusPane::Inspector);
        state.bold_term_explanations_disabled = true;
        let encoded = serde_json::to_string(&state.preferences()).unwrap();
        let mut restored = AppState::default();
        restored.apply_preferences(serde_json::from_str(&encoded).unwrap());
        assert_eq!(restored.view, View::Models);
        assert_eq!(restored.focus_pane, FocusPane::Workspace);
        assert_eq!(restored.route, Route::System);
        assert!(restored.bold_term_explanations_disabled);
    }

    #[test]
    fn version_two_activity_and_out_of_range_theme_preferences_migrate() {
        let preferences: UiPreferences = serde_json::from_str(
            r#"{"version":2,"view":"activity","focus_pane":"inspector","theme_index":31}"#,
        )
        .unwrap();
        let mut state = AppState::default();
        state.apply_preferences(preferences);
        assert_eq!(state.view, View::Indexing);
        assert_eq!(state.focus_pane, FocusPane::Workspace);
        assert_eq!(state.theme_index, 1);
    }

    #[test]
    fn conceptual_focus_cycles_sidebar_workspace_inspector() {
        let mut state = AppState::default();
        update(&mut state, Action::FocusPanePrevious);
        assert_eq!(state.focus_pane, FocusPane::Sidebar);
        update(&mut state, Action::FocusPaneNext);
        assert_eq!(state.focus_pane, FocusPane::Workspace);
        update(&mut state, Action::FocusPaneNext);
        assert_eq!(state.focus_pane, FocusPane::Inspector);
    }
}
